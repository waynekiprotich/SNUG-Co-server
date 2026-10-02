from pathlib import Path

from dotenv import load_dotenv
from markupsafe import escape
from flask import Flask, abort, request, send_from_directory
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

from .cli import register_cli
from .config import BASE_DIR, build_config
from .errors import register_errors
from .extensions import db, migrate
from .routes import admin, auth, public, shopper
from .security import csrf_guard


@event.listens_for(Engine, "connect")
def _sqlite_foreign_keys(dbapi_connection, _record):
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_app(test_config=None):
    if test_config is None:
        load_dotenv(BASE_DIR / ".env")
    app = Flask(__name__)
    app.config.update(build_config(test_config))

    if not app.config["SECRET_KEY"]:
        raise RuntimeError(
            "SECRET_KEY is not set. Copy .env.example to .env and set it to a long random value."
        )
    if app.config["TRUSTED_PROXIES"]:
        n = app.config["TRUSTED_PROXIES"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)

    if app.config["LOCAL_UPLOADS"]:
        Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True, compare_type=True)
    register_errors(app)
    register_cli(app)

    allowed_origins = set(app.config["ALLOWED_ORIGINS"])

    @app.before_request
    def cors_preflight():
        # Answer CORS preflight before auth and CSRF checks.
        if request.method == "OPTIONS" and request.headers.get("Origin") in allowed_origins:
            return app.response_class(status=204)

    app.before_request(csrf_guard)
    app.register_blueprint(public.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(shopper.bp)

    @app.get("/uploads/<path:filename>")
    def uploads(filename):
        if not app.config["LOCAL_UPLOADS"] or not filename.endswith(".webp"):
            abort(404)
        response = send_from_directory(app.config["UPLOAD_DIR"], filename, max_age=31536000)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    def site_url():
        return (app.config["SITE_URL"] or request.host_url).rstrip("/")

    @app.get("/robots.txt")
    def robots():
        body = f"User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api\nDisallow: /cart\nDisallow: /wishlist\n\nSitemap: {site_url()}/sitemap.xml\n"
        return app.response_class(body, mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap():
        from .models import Category, Product

        base = site_url()
        urls = [(f"{base}{p}", None) for p in ("/", "/shop", "/about", "/contact", "/shipping-and-orders")]
        urls += [(f"{base}/shop/{c.slug}", None) for c in Category.query.all()]
        urls += [
            (f"{base}/product/{p.slug}", p.updated_at.date().isoformat())
            for p in Product.query.filter_by(published=True).all()
        ]
        rows = "".join(
            f"<url><loc>{escape(loc)}</loc>{f'<lastmod>{mod}</lastmod>' if mod else ''}</url>" for loc, mod in urls
        )
        xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'
        return app.response_class(xml, mimetype="application/xml")

    dist = Path(app.config["CLIENT_DIST"])

    @app.get("/", defaults={"path": ""})
    @app.get("/<path:path>")
    def site(path):
        if path.split("/", 1)[0] in ("api", "uploads") or not (dist / "index.html").exists():
            abort(404)
        target = (dist / path).resolve()
        if path and target.is_file() and dist.resolve() in target.parents:
            response = send_from_directory(dist, path)
            if path.startswith(("assets/", "images/")):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response
        response = send_from_directory(dist, "index.html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.path.startswith("/api/admin"):
            response.headers["Cache-Control"] = "no-store"
        origin = request.headers.get("Origin")
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
            if request.method == "OPTIONS":
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE"
                response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Requested-With"
                response.headers["Access-Control-Max-Age"] = "600"
        return response

    return app
