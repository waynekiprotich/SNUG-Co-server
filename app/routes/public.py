from flask import Blueprint, jsonify

from ..errors import ApiError
from ..extensions import db
from ..models import Category, Collection, Product, SiteSettings, with_relations
from ..serializers import image_registry, product_json, product_summary, settings_json, taxonomy_json

bp = Blueprint("public", __name__, url_prefix="/api")

# Browsers keep a copy for a minute. The CDN (Vercel honours CDN-Cache-Control) also serves stale
# copies while it refreshes, so a sleeping server never blocks visitors. Browsers don't get the
# stale allowance: a returning visitor never sees prices or pieces older than a minute.
PUBLIC_CACHE = "public, max-age=60, s-maxage=60"
CDN_CACHE = "public, max-age=60, stale-while-revalidate=604800, stale-if-error=604800"

HOME_NEW_ARRIVALS = 4
RELATED_LIMIT = 4


def _public(payload):
    response = jsonify(payload)
    response.headers["Cache-Control"] = PUBLIC_CACHE
    response.headers["CDN-Cache-Control"] = CDN_CACHE
    return response


def _published():
    return with_relations(Product.query.filter_by(published=True)).order_by(Product.sort_order, Product.id).all()


def _summary_images(products):
    return [i.id for p in products for i in p.images[:2]]


def _loaded_images(products):
    """Photo rows already fetched with the products, so the registry needn't query them again."""
    return [i for p in products for i in p.images]


@bp.get("/health")
def health():
    return jsonify({"status": "ok"})


@bp.get("/settings")
def settings():
    return _public(settings_json(db.session.get(SiteSettings, 1)))


@bp.get("/catalog")
def catalog():
    """Full published catalog. The shop now loads /home, /products and /products/<slug>; this stays
    for pages still open from an older build."""
    products = _published()
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    collections = Collection.query.order_by(Collection.sort_order, Collection.id).all()

    image_ids = [i.id for p in products for i in p.images]
    image_ids += [c.image_id for c in categories] + [c.image_id for c in collections]

    return _public(
        {
            "products": [product_json(p) for p in products],
            "categories": [taxonomy_json(c) for c in categories],
            "collections": [taxonomy_json(c) for c in collections],
            "images": image_registry(image_ids, loaded=_loaded_images(products)),
        }
    )


@bp.get("/home")
def home():
    """Only what the home page shows: new arrivals, category tiles and the His & Hers pair."""
    products = _published()
    counts = {}
    for p in products:
        counts[p.category_id] = counts.get(p.category_id, 0) + 1
    categories = [c for c in Category.query.order_by(Category.sort_order, Category.id).all() if counts.get(c.id)]
    new_arrivals = [p for p in products if p.new_arrival][:HOME_NEW_ARRIVALS]
    pair = [p for p in products if p.images and any(c.slug == "his-and-hers" for c in p.collections)][:2]

    return _public(
        {
            "newArrivals": [product_summary(p) for p in new_arrivals],
            "hisAndHers": [product_summary(p) for p in pair],
            "categories": [{**taxonomy_json(c), "productCount": counts[c.id]} for c in categories],
            "images": image_registry(
                _summary_images(new_arrivals + pair) + [c.image_id for c in categories], loaded=_loaded_images(products)
            ),
        }
    )


@bp.get("/products")
def products():
    """Card-sized data for every published product: the shop grid, search and the bag."""
    items = _published()
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    collections = Collection.query.order_by(Collection.sort_order, Collection.id).all()
    return _public(
        {
            "products": [product_summary(p) for p in items],
            "categories": [taxonomy_json(c) for c in categories],
            "collections": [taxonomy_json(c) for c in collections],
            "images": image_registry(_summary_images(items), loaded=_loaded_images(items)),
        }
    )


def related_to(product, candidates, limit=RELATED_LIMIT):
    """Sharing a collection scores 2, the same category 1. Ties keep shop order."""
    mine = {c.id for c in product.collections}

    def score(p):
        return (2 if mine & {c.id for c in p.collections} else 0) + (1 if p.category_id == product.category_id else 0)

    scored = [(score(p), p) for p in candidates if p.id != product.id]
    scored = sorted((s for s in scored if s[0] > 0), key=lambda s: -s[0])
    return [p for _, p in scored[:limit]]


@bp.get("/products/<slug>")
def product_detail(slug):
    """One product with everything its page needs, plus a few related pieces."""
    items = _published()
    product = next((p for p in items if p.slug == slug), None)
    if product is None:
        raise ApiError(404, "That piece isn't available.")
    related = related_to(product, items)
    return _public(
        {
            "product": product_json(product),
            "category": taxonomy_json(product.category),
            "related": [product_summary(p) for p in related],
            "images": image_registry(
                [i.id for i in product.images] + _summary_images(related), loaded=_loaded_images([product, *related])
            ),
        }
    )
