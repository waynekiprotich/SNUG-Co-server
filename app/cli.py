import json
from pathlib import Path

import click
from werkzeug.security import generate_password_hash

from .errors import ApiError
from .extensions import db
from .models import AdminUser, Category, Collection, Product, ProductImage
from .legacy import register_legacy_cli
from .routes.auth import MIN_PASSWORD

SEED_FILE = Path(__file__).resolve().parent.parent / "seed" / "catalog.json"


def load_catalog(data):
    """Insert the seed catalog."""
    categories = {}
    for i, c in enumerate(data["categories"]):
        row = Category(
            name=c["name"], slug=c["slug"], description=c.get("description"), image_id=c.get("image"), sort_order=i
        )
        db.session.add(row)
        categories[c["slug"]] = row
    collections = {}
    for i, c in enumerate(data["collections"]):
        row = Collection(
            name=c["name"], slug=c["slug"], description=c.get("description"), image_id=c.get("image"), sort_order=i
        )
        db.session.add(row)
        collections[c["slug"]] = row

    for p in data["products"]:
        product = Product(
            slug=p["slug"],
            name=p["name"],
            name_confirmed=p.get("nameConfirmed", True),
            category=categories[p["category"]],
            collections=[collections[s] for s in p.get("collections", [])],
            description=p["description"],
            price_kes=p.get("priceKES"),
            compare_at_price_kes=p.get("compareAtPriceKES"),
            colors=p.get("colors"),
            sizes=p.get("sizes"),
            sizes_note=p.get("sizesNote"),
            options=p.get("options") or [],
            details=p.get("details") or [],
            material=p.get("material"),
            care=p.get("care"),
            tags=p.get("tags") or [],
            availability=p.get("availability", "available"),
            made_to_order=p.get("madeToOrder", False),
            badge=p.get("badge"),
            featured=p.get("featured", False),
            new_arrival=p.get("newArrival", False),
            recency=p.get("recency", 0),
            sort_order=p.get("sortOrder", 0),
            source_post=p.get("sourcePost"),
        )
        for position, image in enumerate(p.get("images", [])):
            product.images.append(ProductImage(id=image["id"], position=position, alt=image["alt"], is_upload=False))
        db.session.add(product)
    db.session.commit()
    return len(data["products"])


def register_cli(app):
    @app.cli.command("seed")
    @click.option("--file", "path", type=click.Path(exists=True, dir_okay=False), default=str(SEED_FILE))
    def seed(path):
        """Load the starter catalog into an empty database."""
        if Product.query.first() or Category.query.first():
            raise click.ClickException("The database already has a catalog. Seeding was skipped.")
        count = load_catalog(json.loads(Path(path).read_text()))
        click.echo(f"Loaded {count} products.")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.password_option(confirmation_prompt=True)
    def create_admin(email, password):
        """Create an admin account."""
        email = email.strip().lower()
        if "@" not in email:
            raise click.ClickException("Enter a valid email address.")
        if len(password) < MIN_PASSWORD:
            raise click.ClickException(f"Use a password of at least {MIN_PASSWORD} characters.")
        if AdminUser.query.filter_by(email=email).first():
            raise click.ClickException("An admin with that email already exists. Use reset-password instead.")
        db.session.add(AdminUser(email=email, password_hash=generate_password_hash(password)))
        db.session.commit()
        click.echo(f"Created admin {email}.")

    @app.cli.command("reset-password")
    @click.option("--email", prompt=True)
    @click.password_option(confirmation_prompt=True)
    def reset_password(email, password):
        """Set a new admin password."""
        user = AdminUser.query.filter_by(email=email.strip().lower()).first()
        if user is None:
            raise click.ClickException("No admin has that email.")
        if len(password) < MIN_PASSWORD:
            raise click.ClickException(f"Use a password of at least {MIN_PASSWORD} characters.")
        user.password_hash = generate_password_hash(password)
        user.session_version += 1
        db.session.commit()
        click.echo("Password updated.")

    register_legacy_cli(app)
