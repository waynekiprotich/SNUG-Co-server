"""Wishlist and cart for shoppers, kept in a signed cookie. No account is needed.

The cookie holds product ids and choices only. Every read checks them against the
catalog, so a product that is hidden or deleted drops out on its own.
"""

import re
import secrets

from flask import Blueprint, current_app, jsonify, request
from itsdangerous import BadSignature, URLSafeSerializer

from ..errors import ApiError, ValidationError
from ..models import Product
from ..serializers import order_check

bp = Blueprint("shopper", __name__, url_prefix="/api/shopper")

COOKIE = "snug_shopper"
COOKIE_DAYS = 90
MAX_WISHLIST = 60
MAX_LINES = 20
MAX_QUANTITY = 10
MAX_TEXT = 60
# Browsers drop cookies over 4 KB.
MAX_COOKIE_BYTES = 3800
UNORDERABLE = {"sold-out", "coming-soon"}
# A public product id: "p" and an ASCII number small enough for any database integer.
PUBLIC_ID = re.compile(r"p([1-9][0-9]{0,8})", re.ASCII)


def parse_public_id(value):
    """The database id in a public id ("p12" -> 12), or None for anything else."""
    match = PUBLIC_ID.fullmatch(value) if isinstance(value, str) else None
    return int(match.group(1)) if match else None


def _serializer():
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="snug-shopper")


def load_state():
    raw = request.cookies.get(COOKIE)
    try:
        data = _serializer().loads(raw) if raw else {}
    except BadSignature:
        data = {}
    if not isinstance(data, dict):
        data = {}
    wishlist = [i for i in data.get("w") or [] if isinstance(i, int)]
    cart = [
        line
        for line in data.get("c") or []
        if isinstance(line, dict) and isinstance(line.get("p"), int) and isinstance(line.get("q"), int) and line.get("k")
    ]
    return {"w": wishlist, "c": cart}


def published(ids):
    if not ids:
        return {}
    rows = Product.query.filter(Product.id.in_(set(ids)), Product.published.is_(True)).all()
    return {p.id: p for p in rows}


def respond(state, changed=False):
    """The shopper's state as JSON. Rewrites the cookie when it changed or had stale items."""
    products = published(state["w"] + [line["p"] for line in state["c"]])
    wishlist = [i for i in state["w"] if i in products]
    cart = [line for line in state["c"] if line["p"] in products]
    changed = changed or len(wishlist) != len(state["w"]) or len(cart) != len(state["c"])
    state = {"w": wishlist, "c": cart}

    response = jsonify(
        {
            "wishlist": [f"p{i}" for i in wishlist],
            "cart": [
                {
                    "key": line["k"],
                    "productId": f"p{line['p']}",
                    "color": line.get("color"),
                    "size": line.get("size"),
                    "options": line.get("o") or {},
                    "quantity": line["q"],
                }
                for line in cart
            ],
            # Current name, price and availability of every saved piece, straight from the
            # database: the bag shows these, and checks them again before an order is sent.
            "products": {f"p{i}": order_check(products[i]) for i in dict.fromkeys(wishlist + [line["p"] for line in cart])},
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    if changed:
        value = _serializer().dumps(state)
        if len(value) > MAX_COOKIE_BYTES:
            raise ApiError(422, "Your bag is full. Order or remove some pieces first.")
        cfg = current_app.config
        response.set_cookie(
            COOKIE,
            value,
            max_age=COOKIE_DAYS * 24 * 3600,
            httponly=True,
            secure=cfg["SESSION_COOKIE_SECURE"],
            samesite=cfg["SESSION_COOKIE_SAMESITE"],
            path="/api/shopper",
        )
    return response


def find_product(product_id):
    """A published product from its public id ("p12")."""
    pk = parse_public_id(product_id)
    product = published([pk]).get(pk) if pk else None
    if product is None:
        raise ApiError(404, "That piece isn’t available.")
    return product


def clean_quantity(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_QUANTITY:
        raise ValidationError({"quantity": f"Choose a quantity from 1 to {MAX_QUANTITY}."})
    return value


def clean_choices(product, body):
    """The colour, size and options for a cart line, checked against the product."""
    errors = {}
    colors = [c["name"] for c in product.colors or []]
    color = body.get("color")
    if colors and color not in colors:
        errors["color"] = "Choose a colour to continue."
    size = body.get("size")
    if product.sizes and size not in product.sizes:
        errors["size"] = "Choose a size to continue."

    sent = body.get("options") if isinstance(body.get("options"), dict) else {}
    options = {}
    for option in product.options or []:
        name = option["name"]
        value = sent.get(name)
        value = value.strip() if isinstance(value, str) else ""
        if option.get("type") == "text":
            if len(value) > MAX_TEXT:
                errors[f"option:{name}"] = f"Use up to {MAX_TEXT} characters."
        elif value and value not in option.get("values", []):
            errors[f"option:{name}"] = f"Choose a {name.lower()}."
        if option.get("required") and not value:
            errors[f"option:{name}"] = f"Choose a {name.lower()}." if option.get("type") != "text" else f"Tell us the {name.lower()}."
        if value:
            options[name] = value
    if errors:
        raise ValidationError(errors)
    return (color if colors else None), (size if product.sizes else None), options


def same_choices(line, color, size, options):
    return line.get("color") == color and line.get("size") == size and (line.get("o") or {}) == options


@bp.get("")
def get_state():
    return respond(load_state())


@bp.get("/check")
def check_products():
    """Current price and availability of up to MAX_LINES pieces ("?ids=p1,p2"). Never cached, so an
    order is never sent on a price the CDN or this browser kept. Hidden or deleted pieces are left out."""
    raw = [i for i in request.args.get("ids", "").split(",") if i]
    if len(raw) > MAX_LINES:
        raise ApiError(422, f"Check up to {MAX_LINES} pieces at a time.")
    ids = [pk for pk in map(parse_public_id, raw) if pk]
    found = published(ids)
    response = jsonify({"products": {f"p{i}": order_check(p) for i, p in found.items()}})
    response.headers["Cache-Control"] = "private, no-store"
    return response


# ---- Wishlist ------------------------------------------------------------


@bp.put("/wishlist/<product_id>")
def save_to_wishlist(product_id):
    product = find_product(product_id)
    state = load_state()
    if product.id in state["w"]:
        return respond(state)
    if len(state["w"]) >= MAX_WISHLIST:
        raise ApiError(422, f"Your wishlist holds up to {MAX_WISHLIST} pieces. Remove some first.")
    state["w"].insert(0, product.id)
    return respond(state, changed=True)


@bp.delete("/wishlist/<product_id>")
def remove_from_wishlist(product_id):
    state = load_state()
    before = len(state["w"])
    state["w"] = [i for i in state["w"] if f"p{i}" != product_id]
    return respond(state, changed=len(state["w"]) != before)


# ---- Cart ----------------------------------------------------------------


@bp.post("/cart")
def add_to_cart():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "Send the piece and your choices.")
    product = find_product(body.get("productId"))
    if product.availability in UNORDERABLE:
        raise ApiError(409, "That piece can’t be ordered right now.")
    color, size, options = clean_choices(product, body)
    quantity = clean_quantity(body.get("quantity", 1))

    state = load_state()
    for line in state["c"]:
        if line["p"] == product.id and same_choices(line, color, size, options):
            line["q"] = min(MAX_QUANTITY, line["q"] + quantity)
            return respond(state, changed=True)
    if len(state["c"]) >= MAX_LINES:
        raise ApiError(422, f"Your bag holds up to {MAX_LINES} pieces. Order or remove some first.")
    line = {"k": secrets.token_hex(4), "p": product.id, "q": quantity}
    if color:
        line["color"] = color
    if size:
        line["size"] = size
    if options:
        line["o"] = options
    state["c"].append(line)
    return respond(state, changed=True)


def find_line(state, key):
    for line in state["c"]:
        if line["k"] == key:
            return line
    raise ApiError(404, "That piece is no longer in your bag.")


@bp.patch("/cart/<key>")
def update_cart_line(key):
    state = load_state()
    line = find_line(state, key)
    line["q"] = clean_quantity((request.get_json(silent=True) or {}).get("quantity"))
    return respond(state, changed=True)


@bp.delete("/cart/<key>")
def remove_cart_line(key):
    state = load_state()
    find_line(state, key)
    state["c"] = [line for line in state["c"] if line["k"] != key]
    return respond(state, changed=True)


@bp.delete("/cart")
def clear_cart():
    state = load_state()
    state["c"] = []
    return respond(state, changed=True)
