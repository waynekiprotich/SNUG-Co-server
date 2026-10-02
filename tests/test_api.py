from pathlib import Path

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, HEADERS, NEW_PRODUCT, photo


# ---- Public catalog -------------------------------------------------------


def test_catalog_returns_seeded_products_in_client_shape(client):
    data = client.get("/api/catalog").get_json()
    assert len(data["products"]) == 23
    p = next(p for p in data["products"] if p["slug"] == "the-black-tracksuit")
    assert p["category"] == "tracksuits"
    assert p["images"][0]["id"] == "the-black-tracksuit-1"
    assert {"priceKES", "newArrival", "madeToOrder", "sizesNote", "sortOrder"} <= set(p)
    assert "published" not in p  # admin-only field
    assert client.get("/api/catalog").headers["Cache-Control"].startswith("public, max-age=60, s-maxage=60")


def test_hidden_products_are_not_public(admin):
    products = admin.get("/api/admin/products").get_json()["products"]
    pid = next(p["id"] for p in products if p["slug"] == "zebra-print-set")
    assert admin.patch(f"/api/admin/products/{pid}", json={"published": False}, headers=HEADERS).status_code == 200
    slugs = [p["slug"] for p in admin.get("/api/catalog").get_json()["products"]]
    assert "zebra-print-set" not in slugs and len(slugs) == 22


# ---- Auth and request guards ---------------------------------------------


def test_admin_routes_require_sign_in(client):
    assert client.get("/api/admin/products").status_code == 401
    assert client.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS).status_code == 401
    assert client.get("/api/admin/me").get_json() == {"email": None}


def test_login_rejects_bad_credentials_with_one_message(client):
    wrong_pw = client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": "nope"}, headers=HEADERS)
    wrong_email = client.post("/api/admin/login", json={"email": "who@example.com", "password": "nope"}, headers=HEADERS)
    assert wrong_pw.status_code == wrong_email.status_code == 401
    assert wrong_pw.get_json() == wrong_email.get_json()


def test_login_is_throttled_after_repeated_failures(client):
    for _ in range(5):
        client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": "bad"}, headers=HEADERS)
    res = client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS)
    assert res.status_code == 429


def test_state_changes_need_the_csrf_header(admin):
    res = admin.post("/api/admin/products", json=NEW_PRODUCT)  # no header
    assert res.status_code == 403


def test_foreign_origin_is_blocked(admin):
    res = admin.post("/api/admin/products", json=NEW_PRODUCT, headers={**HEADERS, "Origin": "https://evil.example"})
    assert res.status_code == 403


def test_logout_ends_the_session(admin):
    assert admin.post("/api/admin/logout", headers=HEADERS).status_code == 200
    assert admin.get("/api/admin/me").get_json() == {"email": None}


def test_change_password(admin, client):
    bad = admin.post("/api/admin/password", json={"currentPassword": "x", "newPassword": "a-long-new-password"}, headers=HEADERS)
    assert bad.status_code == 422 and "currentPassword" in bad.get_json()["fields"]
    short = admin.post("/api/admin/password", json={"currentPassword": ADMIN_PASSWORD, "newPassword": "short"}, headers=HEADERS)
    assert short.status_code == 422
    ok = admin.post("/api/admin/password", json={"currentPassword": ADMIN_PASSWORD, "newPassword": "a-long-new-password"}, headers=HEADERS)
    assert ok.status_code == 200
    fresh = client.application.test_client()
    assert fresh.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": "a-long-new-password"}, headers=HEADERS).status_code == 200


# ---- Product CRUD ---------------------------------------------------------


def test_create_product_appears_first_and_gets_a_slug(admin):
    res = admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS)
    assert res.status_code == 201
    product = res.get_json()["product"]
    assert product["slug"] == "test-lounge-set"
    assert product["published"] is True and product["newArrival"] is True
    public = admin.get("/api/catalog").get_json()["products"]
    assert public[0]["slug"] == "test-lounge-set"
    assert min(p["recency"] for p in public) == public[0]["recency"]


def test_create_validation_reports_each_field(admin):
    res = admin.post(
        "/api/admin/products",
        json={"name": "", "category": "nope", "priceKES": -5, "availability": "maybe", "colors": [{"name": "Red", "swatch": ["red"]}]},
        headers=HEADERS,
    )
    fields = res.get_json()["fields"]
    assert res.status_code == 422
    assert {"name", "description", "priceKES", "availability", "colors"} <= set(fields)


def test_unknown_category_and_collection_are_rejected(admin):
    res = admin.post("/api/admin/products", json={**NEW_PRODUCT, "category": "capes"}, headers=HEADERS)
    assert res.status_code == 422 and "category" in res.get_json()["fields"]
    res = admin.post("/api/admin/products", json={**NEW_PRODUCT, "collections": ["ghost"]}, headers=HEADERS)
    assert res.status_code == 422 and "collections" in res.get_json()["fields"]


def test_duplicate_slug_is_rejected(admin):
    admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS)
    res = admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS)
    assert res.status_code == 422 and "slug" in res.get_json()["fields"]


def test_update_variants_and_flags(admin):
    pid = admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS).get_json()["product"]["id"]
    res = admin.patch(
        f"/api/admin/products/{pid}",
        json={
            "sizes": ["S", "M", "M", "L"],
            "colors": [{"name": "Bottle green", "swatch": ["#1F4A35"]}],
            "options": [{"name": "Style", "values": ["Pants", "Shorts"]}, {"name": "Club", "type": "text", "placeholder": "Arsenal"}],
            "tags": ["Green", " sage ", "green"],
            "availability": "sold-out",
            "badge": "limited",
            "collections": ["kenya", "his-and-hers"],
            "priceKES": None,
        },
        headers=HEADERS,
    )
    p = res.get_json()["product"]
    assert res.status_code == 200
    assert p["sizes"] == ["S", "M", "L"]
    assert p["colors"] == [{"name": "Bottle green", "swatch": ["#1f4a35"]}]
    assert p["options"][0] == {"name": "Style", "values": ["Pants", "Shorts"], "required": True}
    assert p["options"][1]["type"] == "text"
    assert p["tags"] == ["green", "sage"]
    assert p["collections"] == ["kenya", "his-and-hers"]
    assert p["priceKES"] is None and p["availability"] == "sold-out"


def test_compare_at_must_exceed_price(admin):
    res = admin.post("/api/admin/products", json={**NEW_PRODUCT, "compareAtPriceKES": 4000}, headers=HEADERS)
    assert res.status_code == 422 and "compareAtPriceKES" in res.get_json()["fields"]
    ok = admin.post("/api/admin/products", json={**NEW_PRODUCT, "compareAtPriceKES": 6000}, headers=HEADERS)
    assert ok.status_code == 201


def test_move_product_reorders(admin):
    products = admin.get("/api/admin/products").get_json()["products"]
    first, second = products[0], products[1]
    assert admin.post(f"/api/admin/products/{second['id']}/move", json={"direction": "up"}, headers=HEADERS).status_code == 200
    after = admin.get("/api/admin/products").get_json()["products"]
    assert [after[0]["id"], after[1]["id"]] == [second["id"], first["id"]]
    # Moving the top item up is a no-op.
    admin.post(f"/api/admin/products/{after[0]['id']}/move", json={"direction": "up"}, headers=HEADERS)
    assert admin.get("/api/admin/products").get_json()["products"][0]["id"] == second["id"]


def test_delete_product_removes_its_uploaded_files(admin, app):
    pid = admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS).get_json()["product"]["id"]
    admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(), "a.jpg")}, headers=HEADERS)
    uploads = Path(app.config["UPLOAD_DIR"])
    assert list(uploads.glob("*.webp"))
    assert admin.delete(f"/api/admin/products/{pid}", headers=HEADERS).status_code == 200
    assert not list(uploads.glob("*.webp"))
    assert admin.get(f"/api/admin/products/{pid}").status_code == 404


# ---- Photos ---------------------------------------------------------------


def test_upload_makes_cropped_webp_sizes_and_registers_them(admin, app):
    from PIL import Image

    pid = admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS).get_json()["product"]["id"]
    res = admin.post(
        f"/api/admin/products/{pid}/images",
        data={"files": [(photo(2000, 1500), "wide.jpg"), (photo(900, 1800, fmt="PNG"), "tall.png")]},
        headers=HEADERS,
    )
    body = res.get_json()
    assert res.status_code == 201
    images = body["product"]["images"]
    assert len(images) == 2 and images[0]["alt"] == "Test Lounge Set"
    meta = body["images"][images[0]["id"]]
    # Cropped to 1200x1500; the full width is kept so large screens stay sharp.
    assert meta["widths"] == [480, 800, 1080, 1200] and meta["height"] == 1500
    assert meta["base"] == f"/uploads/{images[0]['id']}"

    largest = Path(app.config["UPLOAD_DIR"]) / f"{images[0]['id']}-1080.webp"
    with Image.open(largest) as im:
        assert im.format == "WEBP" and im.size == (1080, 1350)
    served = admin.get(f"/uploads/{images[0]['id']}-1080.webp")
    assert served.status_code == 200 and served.mimetype == "image/webp"
    assert "immutable" in served.headers["Cache-Control"]

    public = admin.get("/api/catalog").get_json()
    assert images[0]["id"] in public["images"]


def test_upload_rejects_non_images_and_tiny_photos(admin):
    pid = admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS).get_json()["product"]["id"]
    import io

    fake = admin.post(f"/api/admin/products/{pid}/images", data={"files": (io.BytesIO(b"<script>alert(1)</script>"), "x.jpg")}, headers=HEADERS)
    assert fake.status_code == 422
    tiny = admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(300, 400), "tiny.jpg")}, headers=HEADERS)
    assert tiny.status_code == 422 and "too small" in tiny.get_json()["error"]
    assert admin.get(f"/api/admin/products/{pid}").get_json()["product"]["images"] == []


def test_bad_photo_in_batch_saves_nothing(admin, app):
    import io

    pid = admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS).get_json()["product"]["id"]
    res = admin.post(
        f"/api/admin/products/{pid}/images",
        data={"files": [(photo(), "ok.jpg"), (io.BytesIO(b"nope"), "bad.jpg")]},
        headers=HEADERS,
    )
    assert res.status_code == 422
    assert not list(Path(app.config["UPLOAD_DIR"]).glob("*.webp"))


def test_reorder_edit_and_delete_photos(admin, app):
    pid = admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS).get_json()["product"]["id"]
    res = admin.post(
        f"/api/admin/products/{pid}/images", data={"files": [(photo(), "1.jpg"), (photo(color=(1, 2, 3)), "2.jpg")]}, headers=HEADERS
    )
    a, b = [i["id"] for i in res.get_json()["product"]["images"]]

    bad = admin.post(f"/api/admin/products/{pid}/images/order", json={"ids": [a]}, headers=HEADERS)
    assert bad.status_code == 422
    ordered = admin.post(f"/api/admin/products/{pid}/images/order", json={"ids": [b, a]}, headers=HEADERS)
    assert [i["id"] for i in ordered.get_json()["product"]["images"]] == [b, a]

    alt = admin.patch(f"/api/admin/products/{pid}/images/{a}", json={"alt": "Front view"}, headers=HEADERS)
    assert alt.get_json()["product"]["images"][1]["alt"] == "Front view"

    gone = admin.delete(f"/api/admin/products/{pid}/images/{b}", headers=HEADERS)
    assert [i["id"] for i in gone.get_json()["product"]["images"]] == [a]
    assert not list(Path(app.config["UPLOAD_DIR"]).glob(f"{b}-*.webp"))


def test_photo_limit_per_product(admin):
    pid = admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS).get_json()["product"]["id"]
    files = [(photo(700, 900), f"{i}.jpg") for i in range(13)]
    res = admin.post(f"/api/admin/products/{pid}/images", data={"files": files}, headers=HEADERS)
    assert res.status_code == 422


def test_uploads_route_only_serves_webp(client):
    assert client.get("/uploads/../.env").status_code == 404
    assert client.get("/uploads/secret.txt").status_code == 404


# ---- Categories and collections ------------------------------------------


def test_category_in_use_cannot_be_deleted(admin):
    cats = admin.get("/api/admin/categories").get_json()["items"]
    tracksuits = next(c for c in cats if c["slug"] == "tracksuits")
    assert tracksuits["productCount"] == 2
    res = admin.delete(f"/api/admin/categories/{tracksuits['id']}", headers=HEADERS)
    assert res.status_code == 409


def test_create_edit_reorder_and_delete_category(admin):
    made = admin.post("/api/admin/categories", json={"name": "Caps & hats", "description": "Headwear"}, headers=HEADERS)
    assert made.status_code == 201
    item = made.get_json()["item"]
    assert item["slug"] == "caps-and-hats"
    cid = item["id"]
    admin.patch(f"/api/admin/categories/{cid}", json={"name": "Caps", "slug": "caps"}, headers=HEADERS)
    admin.post(f"/api/admin/categories/{cid}/move", json={"direction": "up"}, headers=HEADERS)
    slugs = [c["slug"] for c in admin.get("/api/catalog").get_json()["categories"]]
    assert slugs[-2:] == ["caps", "sweatshirts"]
    assert admin.delete(f"/api/admin/categories/{cid}", headers=HEADERS).status_code == 200


def test_category_cover_must_be_a_real_photo(admin):
    res = admin.post("/api/admin/categories", json={"name": "Hats", "image": "missing-photo"}, headers=HEADERS)
    assert res.status_code == 422 and "image" in res.get_json()["fields"]
    ok = admin.post("/api/admin/categories", json={"name": "Hats", "image": "green-tracksuit-1"}, headers=HEADERS)
    assert ok.status_code == 201


def test_deleting_a_photo_clears_category_cover(admin):
    cats = admin.get("/api/admin/categories").get_json()["items"]
    tracksuits = next(c for c in cats if c["slug"] == "tracksuits")
    assert tracksuits["image"] == "green-tracksuit-1"
    products = admin.get("/api/admin/products").get_json()["products"]
    pid = next(p["id"] for p in products if p["slug"] == "green-tracksuit")
    admin.delete(f"/api/admin/products/{pid}/images/green-tracksuit-1", headers=HEADERS)
    cats = admin.get("/api/admin/categories").get_json()["items"]
    assert next(c for c in cats if c["slug"] == "tracksuits")["image"] is None


def test_deleting_a_collection_detaches_products(admin):
    cols = admin.get("/api/admin/collections").get_json()["items"]
    kenya = next(c for c in cols if c["slug"] == "kenya")
    assert admin.delete(f"/api/admin/collections/{kenya['id']}", headers=HEADERS).status_code == 200
    products = admin.get("/api/catalog").get_json()["products"]
    assert all("kenya" not in p["collections"] for p in products)


def test_site_served_with_spa_fallback(tmp_path):
    from app import create_app

    (tmp_path / "index.html").write_text("<html>app</html>")
    app = create_app({"SECRET_KEY": "x", "SQLALCHEMY_DATABASE_URI": "sqlite://", "CLIENT_DIST": str(tmp_path),
                      "UPLOAD_DIR": str(tmp_path / "up")})
    c = app.test_client()
    assert b"app" in c.get("/shop/some-page").data
    assert c.get("/api/nope").status_code == 404
    assert c.get("/uploads/x.png").status_code == 404


def test_sitemap_and_robots(client):
    xml = client.get("/sitemap.xml").get_data(as_text=True)
    assert "/product/kenya-bomber-jacket" in xml and "/shop" in xml
    assert "Sitemap:" in client.get("/robots.txt").get_data(as_text=True)


def test_supabase_storage_verifies_ssl_and_sends_expected_request(monkeypatch):
    import ssl

    from app.legacy import SupabaseStorage

    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None, context=None: calls.append((req, context)))
    SupabaseStorage("https://x.supabase.co/", "secret-key", "product-photos").save("kenya-1", {480: b"a", 800: b"b"})

    assert len(calls) == 2
    req, context = calls[0]
    assert req.full_url == "https://x.supabase.co/storage/v1/object/product-photos/kenya-1-480.webp"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer secret-key"
    assert req.get_header("X-upsert") == "true"
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_import_bundled_photos_resumes_without_duplicates(app, tmp_path, monkeypatch):
    import json

    from app.extensions import db
    from app.legacy import SupabaseStorage
    from app.models import ProductImage

    with app.app_context():
        ids = [i.id for i in ProductImage.query.order_by(ProductImage.id).limit(2)]
        # Pretend the first photo was migrated by an earlier, interrupted run.
        first = db.session.get(ProductImage, ids[0])
        first.is_upload, first.widths, first.width, first.height = True, [480], 480, 600
        db.session.commit()

    client = tmp_path / "client"
    (client / "public" / "images").mkdir(parents=True)
    (client / "src" / "data").mkdir(parents=True)
    (client / "src" / "data" / "image-manifest.json").write_text(
        json.dumps({i: {"widths": [480], "width": 480, "height": 600} for i in ids})
    )
    for i in ids:
        (client / "public" / "images" / f"{i}-480.webp").write_bytes(b"x")

    saved = []

    class FakeStorage(SupabaseStorage):
        def __init__(self):
            pass

        def save(self, image_id, files):
            saved.append(image_id)

    monkeypatch.setattr("app.legacy.supabase_storage", lambda: FakeStorage())
    runner = app.test_cli_runner()
    for _ in range(2):
        result = runner.invoke(args=["import-bundled-photos", "--client-dir", str(client)])
        assert result.exit_code == 0, result.output

    assert saved == [ids[1]]  # the migrated one is skipped; the second run does nothing
    with app.app_context():
        assert db.session.get(ProductImage, ids[1]).is_upload is True


def test_homepage_images_set_in_settings(admin):
    client = admin
    image_id = client.get("/api/admin/images").get_json()["images"][0]["id"]
    res = client.patch("/api/admin/settings", json={"heroImage": image_id, "heroAlt": "Two models"}, headers=HEADERS)
    assert res.status_code == 200
    public = client.get("/api/settings").get_json()
    assert public["heroImage"] == image_id and public["heroAlt"] == "Two models"
    bad = client.patch("/api/admin/settings", json={"featureImage": "nope"}, headers=HEADERS)
    assert bad.status_code == 422


def test_cross_origin_admin_gets_cors_and_cross_site_cookie(tmp_path, monkeypatch):
    from app import create_app
    from app.extensions import db
    from app.models import AdminUser
    from conftest import ADMIN_EMAIL, ADMIN_PASSWORD
    from werkzeug.security import generate_password_hash

    site = "https://shop.example.com"
    monkeypatch.setenv("ALLOWED_ORIGINS", site)
    monkeypatch.setenv("SESSION_COOKIE_SAMESITE", "None")
    app = create_app({"SECRET_KEY": "x", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'c.db'}",
                      "UPLOAD_DIR": str(tmp_path / "up")})
    with app.app_context():
        db.create_all()
        db.session.add(AdminUser(email=ADMIN_EMAIL, password_hash=generate_password_hash(ADMIN_PASSWORD)))
        db.session.commit()
    c = app.test_client()

    pre = c.options("/api/admin/login", headers={"Origin": site, "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 204
    assert pre.headers["Access-Control-Allow-Origin"] == site
    assert pre.headers["Access-Control-Allow-Credentials"] == "true"
    assert "X-Requested-With" in pre.headers["Access-Control-Allow-Headers"]

    evil = c.options("/api/admin/login", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in evil.headers

    res = c.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                 headers={**HEADERS, "Origin": site})
    assert res.status_code == 200
    cookie = next(c for c in res.headers.getlist("Set-Cookie") if c.startswith("snug_admin="))
    assert "SameSite=None" in cookie and "Secure" in cookie and "HttpOnly" in cookie

    blocked = c.post("/api/admin/logout", headers={**HEADERS, "Origin": "https://evil.example"})
    assert blocked.status_code == 403



def test_default_session_cookie_is_lax(client):
    from conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    res = client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS)
    cookie = next(c for c in res.headers.getlist("Set-Cookie") if c.startswith("snug_admin="))
    assert "SameSite=Lax" in cookie and "HttpOnly" in cookie


def test_attacker_on_shared_address_cannot_lock_out_known_browser(app):
    from conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    owner = app.test_client()
    good = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    assert owner.post("/api/admin/login", json=good, headers=HEADERS).status_code == 200  # gets device cookie
    owner.post("/api/admin/logout", headers=HEADERS)

    attacker = app.test_client()  # same address (test clients share 127.0.0.1), no device cookie
    for _ in range(25):
        attacker.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": "wrong-password!"}, headers=HEADERS)
    assert attacker.post("/api/admin/login", json=good, headers=HEADERS).status_code == 429
    assert owner.post("/api/admin/login", json=good, headers=HEADERS).status_code == 200


def test_password_change_signs_out_other_browsers(app):
    from conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    a, b = app.test_client(), app.test_client()
    for c in (a, b):
        c.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS)
    res = a.post("/api/admin/password", json={"currentPassword": ADMIN_PASSWORD, "newPassword": "another-long-password"}, headers=HEADERS)
    assert res.status_code == 200
    assert a.get("/api/admin/me").get_json()["email"] == ADMIN_EMAIL
    assert b.get("/api/admin/me").get_json() == {"email": None}


def test_oversized_json_body_is_rejected(client):
    body = b'{"email": "' + b"a" * 100_000 + b'"}'
    res = client.post("/api/admin/login", data=body, headers={**HEADERS, "Content-Type": "application/json"})
    assert res.status_code == 413


def test_protocol_relative_announcement_link_rejected(admin):
    for bad in ("//evil.com", "/\\evil.com"):
        res = admin.patch("/api/admin/settings", json={"announcementHref": bad}, headers=HEADERS)
        assert res.status_code == 422, bad
    assert admin.patch("/api/admin/settings", json={"announcementHref": "/shop"}, headers=HEADERS).status_code == 200


def test_huge_png_rejected_before_decoding(admin):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", (7000, 6500)).save(buf, "PNG")  # 45.5 MP, tiny file
    pid = admin.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS).get_json()["product"]["id"]
    res = admin.post(f"/api/admin/products/{pid}/images", data={"files": [(io.BytesIO(buf.getvalue()), "big.png")]}, headers=HEADERS)
    assert res.status_code == 422
    assert "too large" in res.get_json()["error"]


def test_catalog_uses_a_fixed_number_of_queries(app, client):
    from sqlalchemy import event

    from app.extensions import db

    with app.app_context():
        engine = db.engine
    count = []
    listener = lambda *a, **k: count.append(1)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        client.get("/api/catalog")
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert len(count) <= 8, f"{len(count)} queries; each is a slow round trip in production"
