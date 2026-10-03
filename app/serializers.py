"""Database rows to client JSON."""

from flask import current_app
from sqlalchemy import or_

from .images import cloudinary_template
from .models import ProductImage


def image_meta(image):
    """How the client loads a photo, or None to use the client's bundled copy.

    Cloudinary photos get {width, height, src}, where src has {w} for the width.
    Older photos get {widths, width, height, base} for files named base-<width>.webp.
    """
    cfg = current_app.config
    legacy = None
    if image.is_upload and image.widths:
        legacy = {
            "widths": image.widths,
            "width": image.width,
            "height": image.height,
            "base": f"{cfg['UPLOAD_URL_BASE']}/{image.id}",
        }
    if cfg["IMAGE_DELIVERY"] == "legacy" and (legacy or not image.is_upload):
        return legacy
    if image.cloudinary_public_id and cfg["CLOUDINARY_CLOUD_NAME"]:
        return {
            "width": image.width,
            "height": image.height,
            "src": cloudinary_template(
                cfg["CLOUDINARY_CLOUD_NAME"], image.cloudinary_public_id, image.cloudinary_version, image.crop
            ),
        }
    return legacy


def image_registry(image_ids, loaded=()):
    """Metadata for stored images. The client resolves bundled ones.

    loaded: image rows the caller already has, so only the others are queried. The database is
    a network hop away, so each query saved is a round trip saved.
    """
    ids = {i for i in image_ids if i}
    if not ids:
        return {}
    rows = [row for row in loaded if row.id in ids]
    missing = ids - {row.id for row in rows}
    if missing:
        rows += ProductImage.query.filter(
            ProductImage.id.in_(missing), or_(ProductImage.is_upload.is_(True), ProductImage.cloudinary_public_id.isnot(None))
        ).all()
    registry = {row.id: image_meta(row) for row in rows if row.is_upload or row.cloudinary_public_id}
    return {key: meta for key, meta in registry.items() if meta}


def product_json(p, admin=False):
    data = {
        "id": p.id if admin else f"p{p.id}",
        "slug": p.slug,
        "name": p.name,
        "nameConfirmed": p.name_confirmed,
        "category": p.category.slug,
        "collections": [c.slug for c in p.collections],
        "description": p.description,
        "priceKES": p.price_kes,
        "compareAtPriceKES": p.compare_at_price_kes,
        "images": [{"id": i.id, "alt": i.alt} for i in p.images],
        "colors": p.colors or None,
        "sizes": p.sizes or None,
        "sizesNote": p.sizes_note,
        "options": p.options or [],
        "details": p.details or [],
        "material": p.material,
        "care": p.care,
        "tags": p.tags or [],
        "availability": p.availability,
        "madeToOrder": p.made_to_order,
        "badge": p.badge,
        "featured": p.featured,
        "newArrival": p.new_arrival,
        "recency": p.recency,
        "sortOrder": p.sort_order,
        "sourcePost": p.source_post,
    }
    if admin:
        data["published"] = p.published
        data["updatedAt"] = p.updated_at.isoformat() if p.updated_at else None
    return data


def product_summary(p):
    """What a product card, search and the bag need: no ordering details, first two photos only."""
    return {
        "id": f"p{p.id}",
        "slug": p.slug,
        "name": p.name,
        "category": p.category.slug,
        "collections": [c.slug for c in p.collections],
        "description": p.description,
        "priceKES": p.price_kes,
        "images": [{"id": i.id, "alt": i.alt} for i in p.images[:2]],
        "sizes": p.sizes or None,
        "tags": p.tags or [],
        "availability": p.availability,
        "badge": p.badge,
        "featured": p.featured,
        "newArrival": p.new_arrival,
        "recency": p.recency,
        "sortOrder": p.sort_order,
    }


def order_check(p):
    """What an order is checked against right before WhatsApp opens: never cached."""
    return {
        "id": f"p{p.id}",
        "slug": p.slug,
        "name": p.name,
        "priceKES": p.price_kes,
        "availability": p.availability,
        "madeToOrder": p.made_to_order,
    }


SETTINGS_IMAGES =("hero_image", "feature_image", "feature_image_small")


def settings_json(row):
    if row is None:
        return {"announcementText": None, "announcementHref": None, "heroImage": None, "heroAlt": None,
                "featureImage": None, "featureImageSmall": None, "images": {}}
    return {
        "announcementText": row.announcement_text,
        "announcementHref": row.announcement_href,
        "heroImage": row.hero_image,
        "heroAlt": row.hero_alt,
        "featureImage": row.feature_image,
        "featureImageSmall": row.feature_image_small,
        "images": image_registry(getattr(row, f) for f in SETTINGS_IMAGES),
    }


def taxonomy_json(item, admin=False):
    data = {
        "id": item.id if admin else f"{'c' if item.__tablename__ == 'categories' else 'k'}{item.id}",
        "name": item.name,
        "slug": item.slug,
        "description": item.description or "",
        "image": item.image_id,
        "sortOrder": item.sort_order,
    }
    if admin:
        data["productCount"] = len(item.products)
    return data
