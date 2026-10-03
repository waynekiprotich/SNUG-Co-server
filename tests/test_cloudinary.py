"""Cloudinary storage and delivery. The SDK is replaced by a fake: nothing reaches a real account."""

import io
import json
from pathlib import Path

import cloudinary.exceptions
import cloudinary.uploader
import pytest
from PIL import Image

from app.extensions import db
from app.images import cloudinary_template, cloudinary_url
from app.models import Category, ProductImage

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, HEADERS, NEW_PRODUCT, PROD_SECRET, photo

CLOUD = {"CLOUDINARY_CLOUD_NAME": "snug-test", "CLOUDINARY_API_KEY": "key-123", "CLOUDINARY_API_SECRET": "secret-456"}


class FakeCloudinary:
    def __init__(self):
        self.assets = {}
        self.uploads = []
        self.destroyed = []
        self.fail_on_upload = None

    def upload(self, file, **options):
        assert options["api_secret"] == "secret-456" and options["cloud_name"] == "snug-test"
        self.uploads.append({"file": file if isinstance(file, str) else file.read(), **options})
        if self.fail_on_upload == len(self.uploads):
            raise cloudinary.exceptions.Error("Server error")
        public_id = f"{options['folder']}/{options['public_id']}"
        if public_id in self.assets and not options["overwrite"]:
            return {"public_id": public_id, "version": self.assets[public_id], "existing": True}
        self.assets[public_id] = 1727700000 + len(self.assets)
        return {"public_id": public_id, "version": self.assets[public_id]}

    def destroy(self, public_id, **options):
        self.destroyed.append((public_id, options))
        return {"result": "ok" if self.assets.pop(public_id, None) else "not found"}


@pytest.fixture()
def fake_cloud(monkeypatch):
    fake = FakeCloudinary()
    monkeypatch.setattr(cloudinary.uploader, "upload", fake.upload)
    monkeypatch.setattr(cloudinary.uploader, "destroy", fake.destroy)
    return fake


@pytest.fixture()
def cloud_admin(app, admin, fake_cloud):
    app.config.update(CLOUD)
    return admin


def new_product(client):
    return client.post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS).get_json()["product"]["id"]


def exif_rotated_photo(width, height):
    """A JPEG stored sideways with an EXIF tag that says to rotate it upright."""
    img = Image.new("RGB", (width, height), (90, 60, 40))
    exif = img.getexif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    buf.seek(0)
    return buf


# ---- URL generation --------------------------------------------------------


def test_delivery_template_crops_resizes_and_auto_optimises():
    template = cloudinary_template("snug-test", "snug-co/products/u1", 1727700001, (400, 0, 1200, 1500))
    assert template == (
        "https://res.cloudinary.com/snug-test/image/private/"
        "c_crop,h_1500,w_1200,x_400,y_0/f_auto,q_auto,c_limit,w_{w}/v1727700001/snug-co/products/u1"
    )
    plain = cloudinary_url("snug-test", "snug-co/products/u1", 7, 480)
    assert plain == "https://res.cloudinary.com/snug-test/image/private/f_auto,q_auto,c_limit,w_480/v7/snug-co/products/u1"


# ---- Uploads ---------------------------------------------------------------


def test_upload_sends_original_to_cloudinary_and_returns_delivery_urls(cloud_admin, fake_cloud):
    pid = new_product(cloud_admin)
    wide, tall = photo(2000, 1500), photo(900, 1800, fmt="PNG")
    wide_bytes = wide.getvalue()
    res = cloud_admin.post(
        f"/api/admin/products/{pid}/images",
        data={"files": [(wide, "wide.jpg"), (tall, "tall.png")], "focus": "0.4"},
        headers=HEADERS,
    )
    assert res.status_code == 201
    body = res.get_json()
    first, second = body["product"]["images"]

    # The untouched original is stored, privately, without overwriting anything.
    upload = fake_cloud.uploads[0]
    assert upload["file"] == wide_bytes
    assert upload["folder"] == "snug-co/products" and upload["public_id"] == first["id"]
    assert upload["type"] == "private" and upload["overwrite"] is False
    assert "transformation" not in upload and "format" not in upload

    meta = body["images"][first["id"]]
    assert (meta["width"], meta["height"]) == (1200, 1500)
    assert meta["src"].startswith("https://res.cloudinary.com/snug-test/image/private/c_crop,h_1500,w_1200,x_400,y_0/")
    assert "/f_auto,q_auto,c_limit,w_{w}/v" in meta["src"] and meta["src"].endswith(f"/snug-co/products/{first['id']}")
    assert "secret-456" not in res.get_data(as_text=True) and "key-123" not in res.get_data(as_text=True)

    tall_meta = body["images"][second["id"]]
    assert (tall_meta["width"], tall_meta["height"]) == (900, 1125)
    assert "c_crop,h_1125,w_900,x_0,y_270/" in tall_meta["src"]  # (1800 - 1125) * 0.4

    with cloud_admin.application.app_context():
        row = db.session.get(ProductImage, first["id"])
        assert row.cloudinary_public_id == f"snug-co/products/{first['id']}" and row.cloudinary_version
        assert row.widths is None and row.is_upload

    public = cloud_admin.get("/api/catalog").get_json()["images"]
    assert public[first["id"]]["src"] == meta["src"]
    assert not list(Path(cloud_admin.application.config["UPLOAD_DIR"]).glob("*.webp"))


def test_crop_uses_the_upright_full_size_photo(cloud_admin, fake_cloud):
    pid = new_product(cloud_admin)
    # Stored 1600x1200 but shown upright as 1200x1600.
    res = cloud_admin.post(
        f"/api/admin/products/{pid}/images", data={"files": (exif_rotated_photo(1600, 1200), "phone.jpg")}, headers=HEADERS
    )
    meta = next(iter(res.get_json()["images"].values()))
    assert "c_crop,h_1500,w_1200,x_0,y_40/" in meta["src"]

    # Big JPEGs are decoded at reduced size for checking, but the crop is for the full original.
    res = cloud_admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(4000, 6000), "big.jpg")}, headers=HEADERS)
    big = res.get_json()["images"][res.get_json()["product"]["images"][-1]["id"]]
    assert (big["width"], big["height"]) == (4000, 5000)


def test_upload_rejects_bad_files_before_reaching_cloudinary(cloud_admin, fake_cloud, app):
    pid = new_product(cloud_admin)
    url = f"/api/admin/products/{pid}/images"

    fake = cloud_admin.post(url, data={"files": (io.BytesIO(b"<script>alert(1)</script>"), "x.jpg")}, headers=HEADERS)
    assert fake.status_code == 422
    html = cloud_admin.post(url, data={"files": (photo(), "page.html", "text/html")}, headers=HEADERS)
    assert html.status_code == 415
    tiny = cloud_admin.post(url, data={"files": (photo(300, 400), "tiny.jpg")}, headers=HEADERS)
    assert tiny.status_code == 422 and "too small" in tiny.get_json()["error"]
    gif = io.BytesIO()
    Image.new("RGB", (800, 1000)).save(gif, "GIF")
    gif.seek(0)
    assert cloud_admin.post(url, data={"files": (gif, "a.gif", "image/jpeg")}, headers=HEADERS).status_code == 422

    app.config["MAX_UPLOAD_BYTES"] = 1000
    big = cloud_admin.post(url, data={"files": (photo(), "big.jpg")}, headers=HEADERS)
    assert big.status_code == 413

    assert fake_cloud.uploads == []
    assert cloud_admin.get(f"/api/admin/products/{pid}").get_json()["product"]["images"] == []


def test_upload_requires_an_admin_session(app, client, fake_cloud):
    app.config.update(CLOUD)
    with app.app_context():
        product_id = db.session.execute(db.text("select min(id) from products")).scalar()
    url = f"/api/admin/products/{product_id}/images"
    assert client.post(url, data={"files": (photo(), "a.jpg")}, headers=HEADERS).status_code == 401
    client.post("/api/admin/logout", headers=HEADERS)
    assert client.post(url, data={"files": (photo(), "a.jpg")}, headers=HEADERS).status_code == 401
    # Signed in, but without the admin page's header.
    client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS)
    assert client.post(url, data={"files": (photo(), "a.jpg")}).status_code == 403
    assert fake_cloud.uploads == []


def test_cloudinary_failure_keeps_nothing(cloud_admin, fake_cloud):
    pid = new_product(cloud_admin)
    fake_cloud.fail_on_upload = 2
    res = cloud_admin.post(
        f"/api/admin/products/{pid}/images",
        data={"files": [(photo(), "1.jpg"), (photo(), "2.jpg")]},
        headers=HEADERS,
    )
    assert res.status_code == 502 and "couldn’t be uploaded" in res.get_json()["error"]
    assert fake_cloud.assets == {}  # the first upload was removed again
    assert [d[0] for d in fake_cloud.destroyed] == [f"snug-co/products/{fake_cloud.uploads[0]['public_id']}"]
    assert cloud_admin.get(f"/api/admin/products/{pid}").get_json()["product"]["images"] == []

    fake_cloud.fail_on_upload = None
    ok = cloud_admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(), "1.jpg")}, headers=HEADERS)
    assert ok.status_code == 201


def test_database_failure_after_upload_removes_the_asset(app, cloud_admin, fake_cloud, monkeypatch):
    from sqlalchemy.exc import OperationalError

    pid = new_product(cloud_admin)

    def broken_commit():
        raise OperationalError("commit", {}, Exception("connection lost"))

    monkeypatch.setattr(db.session, "commit", broken_commit)
    res = cloud_admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(), "1.jpg")}, headers=HEADERS)
    monkeypatch.undo()
    assert res.status_code == 500 and "couldn’t be saved" in res.get_json()["error"]
    assert fake_cloud.assets == {} and len(fake_cloud.destroyed) == 1
    with app.app_context():
        assert ProductImage.query.filter(ProductImage.cloudinary_public_id.isnot(None)).count() == 0


# ---- Deletes ---------------------------------------------------------------


def test_deleting_a_photo_deletes_the_asset_and_purges_the_cdn(cloud_admin, fake_cloud):
    pid = new_product(cloud_admin)
    res = cloud_admin.post(
        f"/api/admin/products/{pid}/images", data={"files": [(photo(), "1.jpg"), (photo(), "2.jpg")]}, headers=HEADERS
    )
    a, b = [i["id"] for i in res.get_json()["product"]["images"]]
    gone = cloud_admin.delete(f"/api/admin/products/{pid}/images/{a}", headers=HEADERS)
    assert [i["id"] for i in gone.get_json()["product"]["images"]] == [b]
    public_id, options = fake_cloud.destroyed[0]
    assert public_id == f"snug-co/products/{a}"
    assert options["invalidate"] is True and options["type"] == "private"
    assert list(fake_cloud.assets) == [f"snug-co/products/{b}"]

    assert cloud_admin.delete(f"/api/admin/products/{pid}", headers=HEADERS).status_code == 200
    assert fake_cloud.assets == {}


def test_asset_shared_by_another_photo_is_kept(app, cloud_admin, fake_cloud):
    pid = new_product(cloud_admin)
    res = cloud_admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(), "1.jpg")}, headers=HEADERS)
    image_id = res.get_json()["product"]["images"][0]["id"]
    with app.app_context():
        row = db.session.get(ProductImage, image_id)
        public_id = row.cloudinary_public_id
        twin = db.session.get(ProductImage, "green-tracksuit-1")
        twin.cloudinary_public_id, twin.cloudinary_version = public_id, row.cloudinary_version
        db.session.commit()

    cloud_admin.delete(f"/api/admin/products/{pid}/images/{image_id}", headers=HEADERS)
    assert fake_cloud.destroyed == []
    assert public_id in fake_cloud.assets


def test_supabase_copies_are_left_alone_while_cloudinary_is_on(admin, app, fake_cloud):
    pid = new_product(admin)
    # Uploaded before Cloudinary: stored as sized WebP files.
    res = admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(), "old.jpg")}, headers=HEADERS)
    image_id = res.get_json()["product"]["images"][0]["id"]
    uploads = Path(app.config["UPLOAD_DIR"])
    assert list(uploads.glob(f"{image_id}-*.webp"))

    app.config.update(CLOUD)
    admin.delete(f"/api/admin/products/{pid}/images/{image_id}", headers=HEADERS)
    assert list(uploads.glob(f"{image_id}-*.webp")), "rollback copy must not be deleted"
    assert fake_cloud.destroyed == []


# ---- Serialisation and rollback -------------------------------------------


def migrate_one(app, image_id, version=5):
    with app.app_context():
        row = db.session.get(ProductImage, image_id)
        row.cloudinary_public_id, row.cloudinary_version = f"snug-co/products/{image_id}", version
        db.session.commit()


def test_catalog_serves_cloudinary_photos_and_the_rollback_switch(app, admin, client):
    pid = new_product(admin)
    admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(), "old.jpg")}, headers=HEADERS)
    app.config.update(CLOUD)
    with app.app_context():
        legacy_id = ProductImage.query.filter_by(product_id=pid).one().id
    migrate_one(app, legacy_id)  # an older upload that the migration copied
    migrate_one(app, "green-tracksuit-1")  # a bundled photo that the migration copied

    images = client.get("/api/catalog").get_json()["images"]
    assert images["green-tracksuit-1"]["src"].endswith("/v5/snug-co/products/green-tracksuit-1")
    assert "f_auto,q_auto,c_limit,w_{w}" in images[legacy_id]["src"]
    assert "c_crop" not in images[legacy_id]["src"]  # already 4:5
    assert "the-black-tracksuit-1" not in images  # not migrated: the client's bundled copy is used
    # The page-sized routes build the same entries from the photo rows they already loaded.
    for path in ("/api/products", "/api/home", "/api/products/green-tracksuit"):
        registry = client.get(path).get_json()["images"]
        assert registry["green-tracksuit-1"] == images["green-tracksuit-1"], path
        assert "the-black-tracksuit-1" not in registry, path

    app.config["IMAGE_DELIVERY"] = "legacy"
    images = client.get("/api/catalog").get_json()["images"]
    assert images[legacy_id]["base"].endswith(f"/{legacy_id}") and "src" not in images[legacy_id]
    assert "green-tracksuit-1" not in images

    # Without a cloud name, migrated rows fall back to their old copies.
    app.config.update(IMAGE_DELIVERY="cloudinary", CLOUDINARY_CLOUD_NAME="")
    assert "base" in client.get("/api/catalog").get_json()["images"][legacy_id]


def test_category_cover_and_homepage_photos_use_cloudinary(app, admin, client):
    app.config.update(CLOUD)
    migrate_one(app, "green-tracksuit-1")
    admin.patch("/api/admin/settings", json={"heroImage": "green-tracksuit-1"}, headers=HEADERS)
    settings = client.get("/api/settings").get_json()
    assert settings["images"]["green-tracksuit-1"]["src"].startswith("https://res.cloudinary.com/snug-test/")
    with app.app_context():
        assert Category.query.filter_by(image_id="green-tracksuit-1").count() == 1
    assert "green-tracksuit-1" in client.get("/api/catalog").get_json()["images"]


# ---- Migration -------------------------------------------------------------


@pytest.fixture()
def client_dir(tmp_path):
    """A client repo with two bundled photos."""
    images = tmp_path / "client" / "public" / "images"
    images.mkdir(parents=True)
    manifest = {}
    for image_id in ("green-tracksuit-1", "the-black-tracksuit-1"):
        buf = io.BytesIO()
        Image.new("RGB", (800, 1000)).save(buf, "WEBP")
        (images / f"{image_id}-800.webp").write_bytes(buf.getvalue())
        manifest[image_id] = {"widths": [480, 800], "width": 800, "height": 1000}
    (tmp_path / "client" / "src" / "data").mkdir(parents=True)
    (tmp_path / "client" / "src" / "data" / "image-manifest.json").write_text(json.dumps(manifest))
    return tmp_path / "client"


def run_migration(app, client_dir, *args):
    return app.test_cli_runner().invoke(args=["migrate-images-to-cloudinary", "--client-dir", str(client_dir), *args])


def test_migration_copies_photos_once_and_can_be_rerun(app, admin, fake_cloud, client_dir, monkeypatch):
    checked = []
    monkeypatch.setattr("app.images.check_delivery", lambda url: checked.append(url) or True)
    pid = new_product(admin)
    admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(), "old.jpg")}, headers=HEADERS)
    with app.app_context():
        legacy = ProductImage.query.filter_by(product_id=pid).one()
        legacy_id, legacy_widths = legacy.id, legacy.widths
    app.config.update(CLOUD)

    dry = run_migration(app, client_dir, "--dry-run")
    assert dry.exit_code == 0 and "would copy green-tracksuit-1" in dry.output
    assert fake_cloud.uploads == []
    with app.app_context():
        assert ProductImage.query.filter(ProductImage.cloudinary_public_id.isnot(None)).count() == 0

    result = run_migration(app, client_dir)
    assert result.exit_code == 0, result.output
    assert "Copied 3, failed 0" in result.output
    assert {u["public_id"] for u in fake_cloud.uploads} == {"green-tracksuit-1", "the-black-tracksuit-1", legacy_id}
    assert all(u["folder"] == "snug-co/products" and u["overwrite"] is False for u in fake_cloud.uploads)
    assert all("/f_auto,q_auto,c_limit,w_320/" in url for url in checked)

    with app.app_context():
        bundled = db.session.get(ProductImage, "green-tracksuit-1")
        assert bundled.cloudinary_public_id == "snug-co/products/green-tracksuit-1"
        assert (bundled.width, bundled.height) == (800, 1000)
        migrated = db.session.get(ProductImage, legacy_id)
        assert migrated.widths == legacy_widths and migrated.is_upload  # rollback data kept
    assert list(Path(app.config["UPLOAD_DIR"]).glob(f"{legacy_id}-*.webp"))  # nothing deleted

    again = run_migration(app, client_dir)
    assert again.exit_code == 0 and "Copied 0" in again.output
    assert len(fake_cloud.uploads) == 3


def test_interrupted_migration_resumes_without_duplicates(app, fake_cloud, client_dir, monkeypatch):
    app.config.update(CLOUD)
    # The upload works but the delivery check fails, so the row isn't updated.
    monkeypatch.setattr("app.images.check_delivery", lambda url: False)
    failed = run_migration(app, client_dir)
    assert failed.exit_code != 0 and "FAILED" in failed.output
    with app.app_context():
        assert ProductImage.query.filter(ProductImage.cloudinary_public_id.isnot(None)).count() == 0

    monkeypatch.setattr("app.images.check_delivery", lambda url: True)
    resumed = run_migration(app, client_dir)
    assert resumed.exit_code == 0, resumed.output
    # The retried photos reused their assets.
    assert len(fake_cloud.uploads) == 4 and len(fake_cloud.assets) == 2
    assert [u["public_id"] for u in fake_cloud.uploads[2:]] == [u["public_id"] for u in fake_cloud.uploads[:2]]


def test_migration_needs_credentials(app, client_dir):
    result = run_migration(app, client_dir)
    assert result.exit_code != 0 and "CLOUDINARY_API_SECRET" in result.output


# ---- Session cookie ---------------------------------------------------------


def test_production_session_cookie_is_secure(tmp_path):
    from app import create_app

    prod = create_app(
        {"SECRET_KEY": PROD_SECRET, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'p.db'}", "UPLOAD_DIR": str(tmp_path / "up"),
         "DEBUG": False, "CLOUDINARY_CLOUD_NAME": "", "CLOUDINARY_API_KEY": "", "CLOUDINARY_API_SECRET": ""}
    )
    assert prod.config["SESSION_COOKIE_SECURE"] is True
    with prod.app_context():
        db.create_all()
        from werkzeug.security import generate_password_hash

        from app.models import AdminUser

        db.session.add(AdminUser(email=ADMIN_EMAIL, password_hash=generate_password_hash(ADMIN_PASSWORD)))
        db.session.commit()
    res = prod.test_client().post(
        "/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS,
        base_url="https://example.com",
    )
    cookie = next(c for c in res.headers.getlist("Set-Cookie") if c.startswith("snug_admin="))
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Expires=" in cookie  # 8-hour session, not a forever cookie


def test_development_never_deletes_live_photos(app, cloud_admin, fake_cloud):
    # A live photo, then the same database used by a development server with the live keys
    # (a restored backup, say).
    pid = new_product(cloud_admin)
    res = cloud_admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(), "1.jpg")}, headers=HEADERS)
    live = res.get_json()["product"]["images"][0]["id"]
    app.config["CLOUDINARY_FOLDER"] = "snug-co-dev"

    cloud_admin.delete(f"/api/admin/products/{pid}/images/{live}", headers=HEADERS)
    assert fake_cloud.destroyed == []
    assert f"snug-co/products/{live}" in fake_cloud.assets

    res = cloud_admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(), "2.jpg")}, headers=HEADERS)
    dev = res.get_json()["product"]["images"][0]["id"]
    assert fake_cloud.uploads[-1]["folder"] == "snug-co-dev/products"
    cloud_admin.delete(f"/api/admin/products/{pid}/images/{dev}", headers=HEADERS)
    assert [d[0] for d in fake_cloud.destroyed] == [f"snug-co-dev/products/{dev}"]
