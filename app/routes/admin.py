from collections import namedtuple

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func

from ..errors import ApiError, ValidationError
from ..extensions import db
from ..images import PRODUCT_FOLDER, cloudinary_storage, crop_box, local_storage, new_image_id, open_image, process_image
from ..models import Category, Collection, Product, ProductImage, SiteSettings, with_relations
from ..security import login_required
from ..serializers import SETTINGS_IMAGES, image_registry, product_json, settings_json, taxonomy_json
from ..validation import clean_product, clean_settings, clean_taxonomy, slugify

bp = Blueprint("admin", __name__, url_prefix="/api/admin")
bp.before_request(login_required(lambda: None))

# The browser's label for the file. A missing label is allowed: the content itself is
# always checked when the photo is decoded.
UPLOAD_TYPES = {"image/jpeg", "image/png", "image/webp", "application/octet-stream", ""}

PRODUCT_FIELDS = {
    "name": "name",
    "nameConfirmed": "name_confirmed",
    "description": "description",
    "priceKES": "price_kes",
    "compareAtPriceKES": "compare_at_price_kes",
    "colors": "colors",
    "sizes": "sizes",
    "sizesNote": "sizes_note",
    "options": "options",
    "details": "details",
    "material": "material",
    "care": "care",
    "tags": "tags",
    "availability": "availability",
    "madeToOrder": "made_to_order",
    "badge": "badge",
    "featured": "featured",
    "newArrival": "new_arrival",
    "published": "published",
    "sourcePost": "source_post",
}


def get_or_404(model, item_id, label):
    item = db.session.get(model, item_id)
    if item is None:
        raise ApiError(404, f"That {label} doesn’t exist.")
    return item


def product_payload(product):
    """Product plus its photo metadata."""
    return {
        "product": product_json(product, admin=True),
        "images": image_registry(i.id for i in product.images),
    }


def unique_slug(model, wanted, exclude_id=None):
    base = wanted or "item"
    slug, n = base, 2
    while True:
        query = model.query.filter(model.slug == slug)
        if exclude_id:
            query = query.filter(model.id != exclude_id)
        if query.first() is None:
            return slug
        slug = f"{base[:74]}-{n}"
        n += 1


def resolve_taxonomy(category_slug, collection_slugs):
    category = Category.query.filter_by(slug=category_slug).first() if category_slug else None
    if category_slug and category is None:
        raise ValidationError({"category": "Choose one of the existing categories."})
    collections = []
    if collection_slugs:
        found = {c.slug: c for c in Collection.query.filter(Collection.slug.in_(collection_slugs)).all()}
        missing = [s for s in collection_slugs if s not in found]
        if missing:
            raise ValidationError({"collections": "Unknown collection: " + ", ".join(missing)})
        collections = [found[s] for s in collection_slugs]
    return category, collections


def apply_product(product, cleaned):
    for key, column in PRODUCT_FIELDS.items():
        if key in cleaned:
            setattr(product, column, cleaned[key])


def move_in_order(model, item, direction):
    items = model.query.order_by(model.sort_order, model.id).all()
    index = items.index(item)
    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(items):
        items[index], items[target] = items[target], items[index]
    for position, entry in enumerate(items):
        entry.sort_order = position


StoredPhoto = namedtuple("StoredPhoto", "id widths is_upload public_id")


def stored_photos(images):
    """What to remove from storage once these photos' rows are deleted."""
    return [StoredPhoto(i.id, i.widths, i.is_upload, i.cloudinary_public_id) for i in images]


def remove_stored_files(photos):
    """Run after the delete is committed, so a failure here never leaves a broken row.

    Cloudinary assets are deleted unless another photo still uses them. While Cloudinary is
    on, photos stored before it are left alone: they're the rollback copy.
    """
    cloud = cloudinary_storage(current_app)
    public_ids = {p.public_id for p in photos if p.public_id}
    if public_ids:
        in_use = {
            row[0]
            for row in db.session.query(ProductImage.cloudinary_public_id).filter(
                ProductImage.cloudinary_public_id.in_(public_ids)
            )
        }
        for public_id in sorted(public_ids - in_use):
            if cloud is None:
                current_app.logger.warning("Cloudinary isn’t configured; %s was not deleted.", public_id)
            else:
                cloud.destroy(public_id)
    storage = local_storage(current_app) if cloud is None else None
    if storage:
        for photo in photos:
            if photo.is_upload and photo.widths:
                storage.delete(photo.id, photo.widths)


# ---- Products -------------------------------------------------------------


@bp.get("/products")
def list_products():
    products = with_relations(Product.query).order_by(Product.sort_order, Product.id).all()
    return jsonify(
        {
            "products": [product_json(p, admin=True) for p in products],
            "images": image_registry(i.id for p in products for i in p.images),
        }
    )


@bp.post("/products")
def create_product():
    cleaned = clean_product(request.get_json(silent=True))
    category, collections = resolve_taxonomy(cleaned["category"], cleaned.get("collections"))

    wanted = cleaned.get("slug") or slugify(cleaned["name"])
    if not wanted:
        raise ValidationError({"slug": "Add letters or numbers to the name so a web address can be made."})
    if Product.query.filter_by(slug=wanted).first():
        raise ValidationError({"slug": "Another product already uses this web address."})

    sort_order = (db.session.query(func.min(Product.sort_order)).scalar() or 0) - 1
    recency = (db.session.query(func.min(Product.recency)).scalar() or 0) - 1
    product = Product(slug=wanted, category=category, collections=collections, sort_order=sort_order, recency=recency)
    apply_product(product, cleaned)
    db.session.add(product)
    db.session.commit()
    return jsonify(product_payload(product)), 201


@bp.get("/products/<int:product_id>")
def get_product(product_id):
    return jsonify(product_payload(get_or_404(Product, product_id, "product")))


@bp.patch("/products/<int:product_id>")
def update_product(product_id):
    product = get_or_404(Product, product_id, "product")
    cleaned = clean_product(request.get_json(silent=True), partial=True)

    if "category" in cleaned or "collections" in cleaned:
        category, collections = resolve_taxonomy(
            cleaned.get("category"), cleaned.get("collections") if "collections" in cleaned else None
        )
        if category:
            product.category = category
        if "collections" in cleaned:
            product.collections = collections
    if "slug" in cleaned:
        wanted = cleaned["slug"] or slugify(product.name)
        if Product.query.filter(Product.slug == wanted, Product.id != product.id).first():
            raise ValidationError({"slug": "Another product already uses this web address."})
        product.slug = wanted

    apply_product(product, cleaned)
    if product.compare_at_price_kes is not None and (
        product.price_kes is None or product.compare_at_price_kes <= product.price_kes
    ):
        raise ValidationError({"compareAtPriceKES": "The compare-at price must be higher than the price."})
    db.session.commit()
    return jsonify(product_payload(product))


@bp.delete("/products/<int:product_id>")
def delete_product(product_id):
    product = get_or_404(Product, product_id, "product")
    photos = stored_photos(product.images)
    image_ids = [i.id for i in product.images]
    Category.query.filter(Category.image_id.in_(image_ids)).update({"image_id": None}, synchronize_session=False)
    Collection.query.filter(Collection.image_id.in_(image_ids)).update({"image_id": None}, synchronize_session=False)
    for column in SETTINGS_IMAGES:
        SiteSettings.query.filter(getattr(SiteSettings, column).in_(image_ids)).update({column: None}, synchronize_session=False)
    db.session.delete(product)
    db.session.commit()
    remove_stored_files(photos)
    return jsonify({"ok": True})


@bp.post("/products/<int:product_id>/move")
def move_product(product_id):
    product = get_or_404(Product, product_id, "product")
    direction = (request.get_json(silent=True) or {}).get("direction")
    if direction not in ("up", "down"):
        raise ApiError(422, "Direction must be up or down.")
    move_in_order(Product, product, direction)
    db.session.commit()
    return jsonify({"ok": True})


# ---- Product photos -------------------------------------------------------


@bp.post("/products/<int:product_id>/images")
def upload_images(product_id):
    request.max_content_length = current_app.config["MAX_UPLOAD_REQUEST_BYTES"]
    product = get_or_404(Product, product_id, "product")
    files = request.files.getlist("files")
    if not files:
        raise ApiError(422, "Choose at least one photo.")
    limit = current_app.config["MAX_IMAGES_PER_PRODUCT"]
    if len(product.images) + len(files) > limit:
        raise ApiError(422, f"A product can have up to {limit} photos. Remove some first.")
    try:
        focus = float(request.form.get("focus", 0.4))
    except ValueError:
        focus = 0.4

    cloud = cloudinary_storage(current_app)
    storage = None if cloud else local_storage(current_app)
    if not cloud and not storage:
        current_app.logger.error("Photo upload refused: CLOUDINARY_* is not set.")
        raise ApiError(503, "Photo storage isn’t set up yet. Ask the site owner to connect Cloudinary.")
    max_bytes = current_app.config["MAX_UPLOAD_BYTES"]
    prepared = []
    # Check every file before storing any, so a bad one in a batch saves nothing.
    for upload in files:
        if upload.mimetype not in UPLOAD_TYPES:
            raise ApiError(415, f"“{upload.filename}” isn’t a JPEG, PNG or WebP photo.")
        data = upload.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ApiError(413, f"“{upload.filename}” is over {max_bytes // (1024 * 1024)} MB.")
        try:
            if cloud:
                _, size = open_image(data)
                prepared.append((data, crop_box(*size, focus)))
            else:
                prepared.append(process_image(data, focus))
        except ApiError as err:
            raise ApiError(err.status, f"“{upload.filename}”: {err.message}")

    position = max((i.position for i in product.images), default=-1) + 1
    if cloud:
        save_to_cloudinary(cloud, product, prepared, position)
    else:
        for widths, blobs in prepared:
            image_id = new_image_id()
            storage.save(image_id, blobs)
            width = max(widths)
            db.session.add(
                ProductImage(
                    id=image_id,
                    product=product,
                    position=position,
                    alt=product.name,
                    is_upload=True,
                    widths=widths,
                    width=width,
                    height=round(width * 5 / 4),
                )
            )
            position += 1
        db.session.commit()
    return jsonify(product_payload(product)), 201


def save_to_cloudinary(cloud, product, prepared, position):
    """Upload originals, then save their rows. On any failure, nothing is kept."""
    uploaded = []
    try:
        for data, (x, y, w, h) in prepared:
            image_id = new_image_id()
            asset = cloud.upload(data, PRODUCT_FOLDER, image_id)
            uploaded.append(asset["public_id"])
            db.session.add(
                ProductImage(
                    id=image_id,
                    product=product,
                    position=position,
                    alt=product.name,
                    is_upload=True,
                    width=w,
                    height=h,
                    crop_x=x,
                    crop_y=y,
                    cloudinary_public_id=asset["public_id"],
                    cloudinary_version=asset["version"],
                )
            )
            position += 1
        db.session.commit()
    except Exception as err:
        db.session.rollback()
        for public_id in uploaded:
            cloud.destroy(public_id)
        if isinstance(err, ApiError):
            raise
        current_app.logger.exception("Saving uploaded photos failed")
        raise ApiError(500, "The photos couldn’t be saved. Try again.")


@bp.patch("/products/<int:product_id>/images/<image_id>")
def update_image(product_id, image_id):
    image = ProductImage.query.filter_by(id=image_id, product_id=product_id).first()
    if image is None:
        raise ApiError(404, "That photo doesn’t exist.")
    alt = (request.get_json(silent=True) or {}).get("alt")
    if not isinstance(alt, str) or not alt.strip() or len(alt.strip()) > 300:
        raise ValidationError({"alt": "Describe the photo in up to 300 characters."})
    image.alt = alt.strip()
    db.session.commit()
    return jsonify(product_payload(image.product))


@bp.post("/products/<int:product_id>/images/order")
def order_images(product_id):
    product = get_or_404(Product, product_id, "product")
    ids = (request.get_json(silent=True) or {}).get("ids")
    current = {i.id: i for i in product.images}
    if not isinstance(ids, list) or sorted(ids) != sorted(current):
        raise ApiError(422, "Send every photo of this product exactly once.")
    for position, image_id in enumerate(ids):
        current[image_id].position = position
    db.session.commit()
    return jsonify(product_payload(product))


@bp.delete("/products/<int:product_id>/images/<image_id>")
def delete_image(product_id, image_id):
    image = ProductImage.query.filter_by(id=image_id, product_id=product_id).first()
    if image is None:
        raise ApiError(404, "That photo doesn’t exist.")
    product = image.product
    photos = stored_photos([image])
    Category.query.filter_by(image_id=image_id).update({"image_id": None})
    Collection.query.filter_by(image_id=image_id).update({"image_id": None})
    for column in SETTINGS_IMAGES:
        SiteSettings.query.filter(getattr(SiteSettings, column) == image_id).update({column: None})
    db.session.delete(image)
    db.session.flush()
    db.session.expire(product, ["images"])
    for position, remaining in enumerate(product.images):
        remaining.position = position
    db.session.commit()
    remove_stored_files(photos)
    return jsonify(product_payload(product))


@bp.get("/images")
def list_images():
    """All photos, for cover pickers."""
    rows = ProductImage.query.order_by(ProductImage.product_id, ProductImage.position).all()
    return jsonify(
        {
            "images": [{"id": r.id, "alt": r.alt, "product": r.product.name} for r in rows],
            "registry": image_registry(r.id for r in rows),
        }
    )


# ---- Categories and collections ------------------------------------------


def register_taxonomy(path, model, label):
    def resolve_image(image_id):
        if image_id and db.session.get(ProductImage, image_id) is None:
            raise ValidationError({"image": "Choose a photo that exists."})

    def list_items():
        items = model.query.order_by(model.sort_order, model.id).all()
        return jsonify({"items": [taxonomy_json(i, admin=True) for i in items]})

    def create_item():
        cleaned = clean_taxonomy(request.get_json(silent=True))
        wanted = cleaned.get("slug") or slugify(cleaned["name"])
        if not wanted:
            raise ValidationError({"slug": "Add letters or numbers to the name so a web address can be made."})
        if model.query.filter_by(slug=wanted).first():
            raise ValidationError({"slug": f"Another {label} already uses this web address."})
        resolve_image(cleaned.get("image"))
        item = model(
            name=cleaned["name"],
            slug=wanted,
            description=cleaned.get("description"),
            image_id=cleaned.get("image"),
            sort_order=(db.session.query(func.max(model.sort_order)).scalar() or 0) + 1,
        )
        db.session.add(item)
        db.session.commit()
        return jsonify({"item": taxonomy_json(item, admin=True)}), 201

    def update_item(item_id):
        item = get_or_404(model, item_id, label)
        cleaned = clean_taxonomy(request.get_json(silent=True), partial=True)
        if "slug" in cleaned:
            wanted = cleaned["slug"] or slugify(item.name)
            if model.query.filter(model.slug == wanted, model.id != item.id).first():
                raise ValidationError({"slug": f"Another {label} already uses this web address."})
            item.slug = wanted
        if "name" in cleaned:
            item.name = cleaned["name"]
        if "description" in cleaned:
            item.description = cleaned["description"]
        if "image" in cleaned:
            resolve_image(cleaned["image"])
            item.image_id = cleaned["image"]
        db.session.commit()
        return jsonify({"item": taxonomy_json(item, admin=True)})

    def delete_item(item_id):
        item = get_or_404(model, item_id, label)
        if model is Category and item.products:
            raise ApiError(409, f"{len(item.products)} product(s) are in this category. Move them to another category first.")
        db.session.delete(item)
        db.session.commit()
        return jsonify({"ok": True})

    def move_item(item_id):
        item = get_or_404(model, item_id, label)
        direction = (request.get_json(silent=True) or {}).get("direction")
        if direction not in ("up", "down"):
            raise ApiError(422, "Direction must be up or down.")
        move_in_order(model, item, direction)
        db.session.commit()
        return jsonify({"ok": True})

    bp.add_url_rule(f"/{path}", f"list_{path}", list_items, methods=["GET"])
    bp.add_url_rule(f"/{path}", f"create_{path}", create_item, methods=["POST"])
    bp.add_url_rule(f"/{path}/<int:item_id>", f"update_{path}", update_item, methods=["PATCH"])
    bp.add_url_rule(f"/{path}/<int:item_id>", f"delete_{path}", delete_item, methods=["DELETE"])
    bp.add_url_rule(f"/{path}/<int:item_id>/move", f"move_{path}", move_item, methods=["POST"])


register_taxonomy("categories", Category, "category")
register_taxonomy("collections", Collection, "collection")


# ---- Site settings ---------------------------------------------------------


def get_settings_row():
    row = db.session.get(SiteSettings, 1)
    if row is None:
        row = SiteSettings(id=1)
        db.session.add(row)
        db.session.commit()
    return row


@bp.get("/settings")
def get_settings():
    return jsonify({"settings": settings_json(get_settings_row())})


@bp.patch("/settings")
def update_settings():
    cleaned = clean_settings(request.get_json(silent=True))
    row = get_settings_row()
    fields = {
        "announcementText": "announcement_text",
        "announcementHref": "announcement_href",
        "heroAlt": "hero_alt",
        "heroImage": "hero_image",
        "featureImage": "feature_image",
        "featureImageSmall": "feature_image_small",
    }
    for key, column in fields.items():
        if key not in cleaned:
            continue
        if column in SETTINGS_IMAGES and cleaned[key] and db.session.get(ProductImage, cleaned[key]) is None:
            raise ValidationError({key: "Choose a photo that exists."})
        setattr(row, column, cleaned[key])
    db.session.commit()
    return jsonify({"settings": settings_json(row)})
