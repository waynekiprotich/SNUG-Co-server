"""Old Supabase Storage photos: only for importing or copying them into Cloudinary.

Nothing here runs in the normal request path. Supabase is read from its own environment
variables (SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_BUCKET) and only by these commands.
A fresh production deployment does not need any of them.
"""

import json
import os
from pathlib import Path

import click

from .errors import ApiError
from .extensions import db
from .images import _ssl_context
from .models import ProductImage

DEFAULT_CLIENT_DIR = Path(__file__).resolve().parent.parent.parent / "client"


class SupabaseStorage:
    """Photos in a Supabase Storage bucket."""

    def __init__(self, url, service_key, bucket):
        self.base = f"{url.rstrip('/')}/storage/v1"
        self.headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
        self.bucket = bucket

    @staticmethod
    def filename(image_id, width):
        return f"{image_id}-{width}.webp"

    def save(self, image_id, files):
        import urllib.error
        import urllib.request

        written = []
        try:
            for width, blob in files.items():
                req = urllib.request.Request(
                    f"{self.base}/object/{self.bucket}/{self.filename(image_id, width)}",
                    data=blob,
                    method="POST",
                    headers={
                        **self.headers,
                        "Content-Type": "image/webp",
                        "x-upsert": "true",
                        # Names are never reused, so cache for a year.
                        "cache-control": "max-age=31536000",
                    },
                )
                urllib.request.urlopen(req, timeout=20, context=_ssl_context())
                written.append(width)
        except (urllib.error.URLError, OSError) as err:
            from flask import current_app

            detail = err.read().decode(errors="replace")[:200] if isinstance(err, urllib.error.HTTPError) else err
            current_app.logger.error("Supabase Storage upload failed: %s", detail)
            self.delete(image_id, written)
            raise ApiError(502, "The photo couldn’t be uploaded. Try again.")

    def delete(self, image_id, widths):
        import urllib.error
        import urllib.request

        for width in widths or []:
            req = urllib.request.Request(
                f"{self.base}/object/{self.bucket}/{self.filename(image_id, width)}",
                method="DELETE",
                headers=self.headers,
            )
            try:
                urllib.request.urlopen(req, timeout=20, context=_ssl_context())
            except (urllib.error.URLError, OSError):
                pass


def supabase_storage():
    """Supabase Storage from the environment, or None when it isn't configured."""
    url, key = os.environ.get("SUPABASE_URL") or "", os.environ.get("SUPABASE_SERVICE_KEY") or ""
    if url and key:
        return SupabaseStorage(url, key, os.environ.get("SUPABASE_BUCKET") or "product-photos")
    return None


def register_legacy_cli(app):
    @app.cli.command("import-bundled-photos")
    @click.option(
        "--client-dir",
        type=click.Path(exists=True, file_okay=False),
        default=str(DEFAULT_CLIENT_DIR),
        help="Path to the client repo (default: ../client next to this one, on this machine).",
    )
    def import_bundled_photos(client_dir):
        """Upload the client's bundled photos to storage. Safe to re-run."""
        storage = supabase_storage()
        if storage is None:
            raise click.ClickException("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in server/.env first.")

        client_dir = Path(client_dir)
        images_dir = client_dir / "public" / "images"
        manifest_file = client_dir / "src" / "data" / "image-manifest.json"
        if not manifest_file.exists():
            raise click.ClickException(f"Can't find {manifest_file}. Pass --client-dir.")
        manifest = json.loads(manifest_file.read_text())

        images = ProductImage.query.filter_by(is_upload=False).all()
        done = skipped = 0
        for image in images:
            meta = manifest.get(image.id)
            if not meta:
                click.echo(f"skip {image.id}: not in image-manifest.json")
                skipped += 1
                continue
            files = {}
            for width in meta["widths"]:
                path = images_dir / f"{image.id}-{width}.webp"
                if not path.exists():
                    click.echo(f"skip {image.id}: missing {path.name}")
                    break
                files[width] = path.read_bytes()
            else:
                try:
                    storage.save(image.id, files)
                except ApiError as err:
                    raise click.ClickException(
                        f"Stopped at {image.id}: {err.message} {done} photos were saved; run the command again to continue."
                    )
                image.is_upload = True
                image.widths, image.width, image.height = meta["widths"], meta["width"], meta["height"]
                db.session.commit()
                done += 1
        click.echo(f"Uploaded {done} photos. {skipped} skipped.")

    @app.cli.command("migrate-images-to-cloudinary")
    @click.option("--dry-run", is_flag=True, help="List what would be copied, change nothing.")
    @click.option("--limit", type=int, default=0, help="Copy at most this many photos (0 = all).")
    @click.option(
        "--client-dir",
        type=click.Path(file_okay=False),
        default=str(DEFAULT_CLIENT_DIR),
        help="Client repo, for photos still bundled with the site (default: ../client).",
    )
    def migrate_images_to_cloudinary(dry_run, limit, client_dir):
        """Copy every photo into Cloudinary. Safe to re-run; deletes nothing."""
        from flask import current_app

        from .images import PRODUCT_FOLDER, check_delivery, cloudinary_storage, cloudinary_url

        cloud = cloudinary_storage(current_app)
        if cloud is None:
            raise click.ClickException(
                "Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET first."
            )
        manifest_file = Path(client_dir) / "src" / "data" / "image-manifest.json"
        manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}

        pending = (
            ProductImage.query.filter(ProductImage.cloudinary_public_id.is_(None))
            .order_by(ProductImage.product_id, ProductImage.position)
            .all()
        )
        total = ProductImage.query.count()
        click.echo(f"{total - len(pending)} of {total} photos are already in Cloudinary. {len(pending)} to go.")
        if limit:
            pending = pending[:limit]

        done = failed = skipped = 0
        for image in pending:
            source, width, height = legacy_source(current_app, image, manifest, Path(client_dir))
            if source is None:
                click.echo(f"skip {image.id}: no stored copy found")
                skipped += 1
                continue
            if dry_run:
                click.echo(f"would copy {image.id} ({width}x{height}) from {source}")
                continue
            try:
                asset = cloud.upload(source, PRODUCT_FOLDER, image.id)
            except ApiError as err:
                click.echo(f"FAILED {image.id}: {err.message}")
                failed += 1
                continue
            check = cloudinary_url(cloud.cloud_name, asset["public_id"], asset["version"], 320)
            if not check_delivery(check):
                click.echo(f"FAILED {image.id}: uploaded, but {check} didn’t return an image")
                failed += 1
                continue
            # Old columns (is_upload, widths) are kept so IMAGE_DELIVERY=legacy can roll back.
            image.cloudinary_public_id = asset["public_id"]
            image.cloudinary_version = asset["version"]
            image.width, image.height = width, height
            db.session.commit()
            done += 1
            click.echo(f"copied {image.id} -> {asset['public_id']}")

        click.echo(f"Copied {done}, failed {failed}, skipped {skipped}." + (" (dry run)" if dry_run else ""))
        if failed:
            raise click.ClickException("Some photos failed. Run the command again to retry them.")



def legacy_source(app, image, manifest, client_dir):
    """Where a photo is stored today, as (bytes or URL, width, height), or (None, None, None).

    Uses the largest size kept: originals were never stored before Cloudinary.
    """
    if image.is_upload and image.widths:
        width = max(image.widths)
        name = f"{image.id}-{width}.webp"
        supabase_url = os.environ.get("SUPABASE_URL") or ""
        if supabase_url:
            bucket = os.environ.get("SUPABASE_BUCKET") or "product-photos"
            source = f"{supabase_url.rstrip('/')}/storage/v1/object/public/{bucket}/{name}"
        else:
            path = Path(app.config["UPLOAD_DIR"]) / name
            if not path.exists():
                return None, None, None
            source = path.read_bytes()
        return source, image.width or width, image.height or round(width * 5 / 4)
    meta = manifest.get(image.id)
    if meta:
        path = client_dir / "public" / "images" / f"{image.id}-{meta['width']}.webp"
        if path.exists():
            return path.read_bytes(), meta["width"], meta["height"]
    return None, None, None
