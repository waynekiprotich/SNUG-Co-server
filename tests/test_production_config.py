"""A fresh production deployment: empty database, Cloudinary for photos, no Supabase Storage."""

import cloudinary.uploader
import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import AdminUser, Product, ProductImage

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, HEADERS, photo
from test_cloudinary import CLOUD, FakeCloudinary

PRODUCTION_ENV = ("FLASK_DEBUG", "UPLOAD_DIR", "UPLOAD_URL_BASE", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_BUCKET")


def production_app(tmp_path, monkeypatch, **env):
    """The app as Render builds it: config from the environment, a brand-new empty database."""
    for name in PRODUCTION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IMAGE_DELIVERY", "cloudinary")
    for name in CLOUD:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'prod.db'}",
            "UPLOAD_DIR": str(tmp_path / "uploads"),
            "SESSION_COOKIE_SECURE": False,
        }
    )
    with app.app_context():
        db.create_all()
        db.session.add(AdminUser(email=ADMIN_EMAIL, password_hash=generate_password_hash(ADMIN_PASSWORD)))
        db.session.commit()
    return app


def sign_in(app):
    client = app.test_client()
    res = client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS)
    assert res.status_code == 200
    return client


@pytest.fixture()
def fake_cloud(monkeypatch):
    fake = FakeCloudinary()
    monkeypatch.setattr(cloudinary.uploader, "upload", fake.upload)
    monkeypatch.setattr(cloudinary.uploader, "destroy", fake.destroy)
    return fake


def test_config_reads_cloudinary_from_the_environment_and_needs_no_supabase(tmp_path, monkeypatch):
    app = production_app(tmp_path, monkeypatch, **{**CLOUD, "CLOUDINARY_CLOUD_NAME": "snug-prod"})
    assert app.config["CLOUDINARY_CLOUD_NAME"] == "snug-prod"
    assert app.config["CLOUDINARY_API_KEY"] == "key-123" and app.config["CLOUDINARY_API_SECRET"] == "secret-456"
    assert app.config["IMAGE_DELIVERY"] == "cloudinary"
    assert app.config["LOCAL_UPLOADS"] is False
    assert not any(key.startswith("SUPABASE") for key in app.config)
    assert not (tmp_path / "uploads").exists(), "production must not create a photo folder on disk"


def test_fresh_database_starts_empty(tmp_path, monkeypatch):
    client = production_app(tmp_path, monkeypatch, **CLOUD).test_client()
    catalog = client.get("/api/catalog").get_json()
    assert catalog == {"products": [], "categories": [], "collections": [], "images": {}}
    assert client.get("/api/settings").get_json()["heroImage"] is None
    assert client.get("/api/shopper").status_code == 200


def test_admin_workflow_on_a_fresh_database_uses_only_cloudinary(tmp_path, monkeypatch, fake_cloud):
    app = production_app(tmp_path, monkeypatch, **CLOUD)
    admin = sign_in(app)

    cat = admin.post("/api/admin/categories", json={"name": "Lounge sets"}, headers=HEADERS)
    assert cat.status_code == 201
    col = admin.post("/api/admin/collections", json={"name": "Kenya"}, headers=HEADERS)
    assert col.status_code == 201

    created = admin.post(
        "/api/admin/products",
        json={"name": "First Set", "category": "lounge-sets", "collections": ["kenya"], "description": "Soft.", "priceKES": 4500},
        headers=HEADERS,
    )
    assert created.status_code == 201
    pid = created.get_json()["product"]["id"]
    assert admin.patch(f"/api/admin/products/{pid}", json={"priceKES": 5000}, headers=HEADERS).status_code == 200

    res = admin.post(
        f"/api/admin/products/{pid}/images",
        data={"files": [(photo(), "a.jpg"), (photo(color=(1, 2, 3)), "b.jpg")]},
        headers=HEADERS,
    )
    assert res.status_code == 201
    body = res.get_json()
    first, second = (i["id"] for i in body["product"]["images"])
    assert all(a["folder"] == "snug-co/products" for a in fake_cloud.uploads)
    for image_id in (first, second):
        src = body["images"][image_id]["src"]
        assert src.startswith("https://res.cloudinary.com/snug-test/") and "f_auto,q_auto" in src
    assert not (tmp_path / "uploads").exists(), "no photo may be written to the server's disk"

    order = admin.post(f"/api/admin/products/{pid}/images/order", json={"ids": [second, first]}, headers=HEADERS)
    assert order.status_code == 200
    assert [i["id"] for i in order.get_json()["product"]["images"]] == [second, first]

    assert admin.delete(f"/api/admin/products/{pid}/images/{first}", headers=HEADERS).status_code == 200
    assert [d[0] for d in fake_cloud.destroyed] == [f"snug-co/products/{first}"]

    shown = admin.get("/api/catalog").get_json()
    assert [p["name"] for p in shown["products"]] == ["First Set"] and len(shown["images"]) == 1

    settings = admin.patch("/api/admin/settings", json={"announcementText": "Free delivery"}, headers=HEADERS)
    assert settings.status_code == 200
    assert admin.get("/api/settings").get_json()["announcementText"] == "Free delivery"

    assert admin.delete(f"/api/admin/products/{pid}", headers=HEADERS).status_code == 200
    assert sorted(d[0] for d in fake_cloud.destroyed) == sorted(f"snug-co/products/{i}" for i in (first, second))
    with app.app_context():
        assert Product.query.count() == 0 and ProductImage.query.count() == 0


def test_upload_is_refused_when_cloudinary_is_not_configured(tmp_path, monkeypatch):
    app = production_app(tmp_path, monkeypatch)
    admin = sign_in(app)
    admin.post("/api/admin/categories", json={"name": "Lounge sets"}, headers=HEADERS)
    pid = admin.post(
        "/api/admin/products",
        json={"name": "Set", "category": "lounge-sets", "description": "Soft."},
        headers=HEADERS,
    ).get_json()["product"]["id"]

    res = admin.post(f"/api/admin/products/{pid}/images", data={"files": (photo(), "a.jpg")}, headers=HEADERS)
    assert res.status_code == 503
    assert "Cloudinary" in res.get_json()["error"]
    assert not (tmp_path / "uploads").exists()
    with app.app_context():
        assert ProductImage.query.count() == 0


def test_uploads_route_is_off_in_production(tmp_path, monkeypatch):
    app = production_app(tmp_path, monkeypatch, **CLOUD)
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "x-480.webp").write_bytes(b"x")
    assert app.test_client().get("/uploads/x-480.webp").status_code == 404


def test_debug_mode_keeps_local_photo_storage_for_development(tmp_path, monkeypatch):
    app = production_app(tmp_path, monkeypatch, FLASK_DEBUG="1")
    assert app.config["LOCAL_UPLOADS"] is True
