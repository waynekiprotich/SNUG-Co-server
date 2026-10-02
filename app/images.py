"""Photo processing and storage: Cloudinary in production, local disk for development."""

import io
import os
import secrets
import tempfile
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import ApiError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
WIDTHS = (480, 800, 1080, 1600)
RATIO = 4 / 5
MIN_WIDTH = 600
ORIENTATION_TAG = 0x0112
Image.MAX_IMAGE_PIXELS = 80_000_000
# Checked before decoding to protect memory.
MAX_PIXELS = 40_000_000  # PNG/WebP decode at full size
MAX_JPEG_PIXELS = 120_000_000  # JPEG decodes at a reduced size via draft()


def new_image_id():
    return "u" + secrets.token_hex(6)


def open_image(data):
    """Decode and check an uploaded photo. Returns (image, full_size).

    full_size is the upright size of the original; JPEGs are decoded smaller.
    """
    try:
        img = Image.open(io.BytesIO(data), formats=sorted(ALLOWED_FORMATS))
        fmt = img.format
        pixels = img.width * img.height
        if pixels > (MAX_JPEG_PIXELS if fmt == "JPEG" else MAX_PIXELS):
            raise ApiError(422, "That photo is too large. Use one under 40 megapixels.")
        full_size = img.size
        if img.getexif().get(ORIENTATION_TAG) in (5, 6, 7, 8):
            full_size = full_size[::-1]
        if fmt == "JPEG":
            # Decode JPEGs at reduced size.
            img.draft("RGB", (2000, 2000))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise ApiError(422, "That file isn’t a photo we can read. Use a JPEG, PNG or WebP image.")

    img = ImageOps.exif_transpose(img)
    if img.width < MIN_WIDTH:
        raise ApiError(422, f"That photo is too small. Use one at least {MIN_WIDTH}px wide.")
    return img, full_size


def crop_box(width, height, focus_y=0.5):
    """The 4:5 crop (x, y, w, h), placed vertically by focus_y."""
    if width / height > RATIO:
        new_w = round(height * RATIO)
        return (width - new_w) // 2, 0, new_w, height
    new_h = round(width / RATIO)
    return 0, round((height - new_h) * min(1.0, max(0.0, focus_y))), width, new_h


def process_image(data, focus_y=0.5):
    """Return (widths, {width: webp_bytes}) for an uploaded photo."""
    img, _ = open_image(data)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(background, img)
    img = img.convert("RGB")

    x, y, w, h = crop_box(*img.size, focus_y)
    img = img.crop((x, y, x + w, y + h))

    widths = [x for x in WIDTHS if x <= img.width]
    if img.width < WIDTHS[-1] and img.width not in widths:
        widths.append(img.width)
    files = {}
    for width in widths:
        out = img if width == img.width else img.resize((width, round(width / RATIO)), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, "WEBP", quality=82, method=6)
        files[width] = buf.getvalue()
    return widths, files


class LocalStorage:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def filename(image_id, width):
        return f"{image_id}-{width}.webp"

    def save(self, image_id, files):
        """Write every width."""
        written = []
        try:
            for width, blob in files.items():
                final = self.dir / self.filename(image_id, width)
                fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".part")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(blob)
                os.replace(tmp, final)
                written.append(final)
        except OSError:
            for path in written:
                path.unlink(missing_ok=True)
            raise ApiError(500, "The photo couldn’t be saved. Check the server’s upload folder.")

    def delete(self, image_id, widths):
        for width in widths or []:
            (self.dir / self.filename(image_id, width)).unlink(missing_ok=True)


def _ssl_context():
    """HTTPS verified with certifi's CA bundle."""
    import ssl

    import certifi

    return ssl.create_default_context(cafile=certifi.where())


# Cloudinary layout: snug-co/products, snug-co/categories, snug-co/homepage.
# Covers and homepage photos are picked from product photos, so only products is used today.
CLOUDINARY_ROOT = "snug-co"
PRODUCT_FOLDER = f"{CLOUDINARY_ROOT}/products"
# Private: the original (with any camera GPS data) needs a signed URL. Resized copies are public
# and Cloudinary strips their metadata.
CLOUDINARY_TYPE = "private"


def cloudinary_template(cloud_name, public_id, version, crop=None):
    """Delivery URL with {w} where the width goes.

    The original stays untouched in Cloudinary. Each URL crops to 4:5, resizes (never up),
    and lets Cloudinary choose the format (AVIF/WebP/JPEG) and quality per browser.
    The version changes whenever the file changes, so CDN copies are never stale.
    """
    steps = []
    if crop:
        x, y, w, h = crop
        steps.append(f"c_crop,h_{h},w_{w},x_{x},y_{y}")
    steps.append("f_auto,q_auto,c_limit,w_{w}")
    return f"https://res.cloudinary.com/{cloud_name}/image/{CLOUDINARY_TYPE}/{'/'.join(steps)}/v{version}/{public_id}"


def cloudinary_url(cloud_name, public_id, version, width, crop=None):
    return cloudinary_template(cloud_name, public_id, version, crop).replace("{w}", str(width))


class CloudinaryStorage:
    """Original photos in Cloudinary, uploaded and deleted only from this server."""

    def __init__(self, cloud_name, api_key, api_secret):
        self.cloud_name = cloud_name
        self.auth = {"cloud_name": cloud_name, "api_key": api_key, "api_secret": api_secret, "secure": True}

    def upload(self, file, folder, name):
        """Upload a file (bytes, file object or URL). Returns {public_id, version}.

        Never overwrites: uploading the same folder/name again returns the existing asset,
        so an interrupted run can be repeated without making duplicates.
        """
        import cloudinary.uploader
        from flask import current_app

        if isinstance(file, bytes):
            file = io.BytesIO(file)
        try:
            result = cloudinary.uploader.upload(
                file,
                folder=folder,
                public_id=name,
                type=CLOUDINARY_TYPE,
                resource_type="image",
                overwrite=False,
                unique_filename=False,
                timeout=60,
                **self.auth,
            )
            return {"public_id": result["public_id"], "version": int(result["version"])}
        except Exception as err:  # the SDK raises several types, including network errors
            current_app.logger.error("Cloudinary upload of %s/%s failed: %s", folder, name, err)
            raise ApiError(502, "The photo couldn’t be uploaded to image storage. Try again.")

    def destroy(self, public_id):
        """Delete an asset and purge CDN copies. Returns False if Cloudinary refused."""
        import cloudinary.uploader
        from flask import current_app

        try:
            result = cloudinary.uploader.destroy(
                public_id, type=CLOUDINARY_TYPE, resource_type="image", invalidate=True, timeout=30, **self.auth
            )
        except Exception as err:
            current_app.logger.error("Cloudinary delete of %s failed: %s", public_id, err)
            return False
        if result.get("result") not in ("ok", "not found"):
            current_app.logger.error("Cloudinary delete of %s failed: %s", public_id, result)
            return False
        return True


def cloudinary_storage(app):
    """Cloudinary uploads, or None when its credentials aren't set."""
    cfg = app.config
    if cfg["CLOUDINARY_CLOUD_NAME"] and cfg["CLOUDINARY_API_KEY"] and cfg["CLOUDINARY_API_SECRET"]:
        return CloudinaryStorage(cfg["CLOUDINARY_CLOUD_NAME"], cfg["CLOUDINARY_API_KEY"], cfg["CLOUDINARY_API_SECRET"])
    return None


def check_delivery(url):
    """True if the URL serves an image."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=30, context=_ssl_context()) as res:
            return res.status == 200 and res.headers.get("Content-Type", "").startswith("image/")
    except (urllib.error.URLError, OSError):
        return False


def local_storage(app):
    """Local-disk photo storage for development, or None when it is off.

    Production never writes photos to the server's disk: Cloudinary is the only store.
    """
    if app.config["LOCAL_UPLOADS"]:
        return LocalStorage(app.config["UPLOAD_DIR"])
    return None
