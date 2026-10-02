import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _database_uri(url):
    if not url:
        instance = BASE_DIR / "instance"
        instance.mkdir(exist_ok=True)
        return f"sqlite:///{instance / 'snug.db'}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def build_config(overrides=None):
    env = os.environ.get
    debug = env("FLASK_DEBUG", "0") == "1"
    upload_dir = env("UPLOAD_DIR") or str(BASE_DIR / "uploads")

    allowed_origins = [o.strip() for o in (env("ALLOWED_ORIGINS") or "").split(",") if o.strip()]
    # Set to None only if the admin calls this API cross-site (needs HTTPS).
    samesite = env("SESSION_COOKIE_SAMESITE") or "Lax"

    config = {
        "SITE_URL": env("SITE_URL") or env("VITE_SITE_URL") or "",
        "SECRET_KEY": env("SECRET_KEY"),
        "DEBUG": debug,
        "SQLALCHEMY_DATABASE_URI": _database_uri(env("DATABASE_URL")),
        # Local-disk photos are for development only (FLASK_DEBUG=1). In production, photos go
        # to Cloudinary, and uploads fail with a clear error if it isn't configured.
        "LOCAL_UPLOADS": debug,
        "UPLOAD_DIR": upload_dir,
        "CLIENT_DIST": env("CLIENT_DIST") or str(BASE_DIR.parent / "client" / "dist"),
        "UPLOAD_URL_BASE": (env("UPLOAD_URL_BASE") or "/uploads").rstrip("/"),
        # Cloudinary. The cloud name is public (it's in every image URL); the key and secret
        # stay on the server and are only needed for uploads, deletes and the migration.
        "CLOUDINARY_CLOUD_NAME": env("CLOUDINARY_CLOUD_NAME") or "",
        "CLOUDINARY_API_KEY": env("CLOUDINARY_API_KEY") or "",
        "CLOUDINARY_API_SECRET": env("CLOUDINARY_API_SECRET") or "",
        # "legacy" serves photos stored before Cloudinary (local files, bundled files) again.
        "IMAGE_DELIVERY": (env("IMAGE_DELIVERY") or "cloudinary").lower(),
        # Photo uploads raise this to MAX_UPLOAD_REQUEST_BYTES.
        "MAX_CONTENT_LENGTH": 64 * 1024,
        "MAX_UPLOAD_REQUEST_BYTES": 40 * 1024 * 1024,
        # Cloudinary's free plan accepts photos up to 10 MB.
        "MAX_UPLOAD_BYTES": 10 * 1024 * 1024,
        "MAX_IMAGES_PER_PRODUCT": 12,
        "SESSION_COOKIE_NAME": "snug_admin",
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": samesite,
        "SESSION_COOKIE_SECURE": samesite == "None" or not debug,
        "PERMANENT_SESSION_LIFETIME": timedelta(hours=8),
        # Hard limit, even for an active session.
        "SESSION_MAX_AGE": timedelta(days=7),
        "ALLOWED_ORIGINS": allowed_origins,
        "TRUSTED_PROXIES": int(env("TRUSTED_PROXIES") or 0),
        "LOGIN_MAX_ATTEMPTS": 5,
        "LOGIN_WINDOW_SECONDS": 15 * 60,
    }
    if overrides:
        config.update(overrides)
    return config
