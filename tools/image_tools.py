"""
create_image_asset: puts an image of the advertiser's own site into the ad account's image library (an `ImageAsset`).

Why it exists: a search ad shows images only from assets uploaded to Google Ads. The Merchant Center feed does not feed
them to Search campaigns (Google documents that only for Performance Max / Shopping / travel feeds), and the feed's
image_link is WebP, which the Ads API documentation does not list (GIF, JPEG and PNG only). So the tool downloads the
picture from the advertiser's site, cuts it to the ratio (1.91:1 or 1:1, tolerance 1 %), checks the minimum size,
converts to JPEG when needed and uploads the bytes. Linking to an ad group is a separate lever:
link_asset_to_entity(level="AD_GROUP", field_type="AD_IMAGE").
"""
import hashlib
import io
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client
from core import policy
from core.config import CUSTOMER_ID
from shield.error_shield import ApiError, ServiceResult, Telemetry, handle_google_ads_errors
from shield.models_image import CreateImageAssetRequest, check_site_image_url

MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024  # what we accept from the site
MAX_ASSET_BYTES = 5 * 1024 * 1024     # Google's limit for an image asset (5120 KB)
MAX_REDIRECTS = 3
RATIO_TOLERANCE = 0.01                # Google accepts the ratio with a 1 % deviation
SPECS = {
    "LANDSCAPE": {"ratio": 1.91, "min": (600, 314), "max_width": 1200, "label": "1.91x1"},
    "SQUARE": {"ratio": 1.0, "min": (300, 300), "max_width": 1200, "label": "1x1"},
}


class ImageSourceError(ValueError):
    """The picture cannot be used (download, format, size); a refusal, not a crash."""


def fetch_site_image(url: str) -> bytes:
    """Downloads the file. Every redirect must stay on the advertiser's own site; the size is capped while reading."""
    import requests

    current = check_site_image_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        response = requests.get(current, timeout=(5, 20), stream=True, allow_redirects=False,
                                headers={"User-Agent": "safe-google-ads-mcp/1.0"})
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise ImageSourceError(f"{current} redirects without a Location")
                current = check_site_image_url(urljoin(current, location))
                continue
            if response.status_code != 200:
                raise ImageSourceError(f"{current} answered HTTP {response.status_code}")
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ImageSourceError(f"{current} is larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
    raise ImageSourceError(f"{url} redirects more than {MAX_REDIRECTS} times")


def prepare_image(data: bytes, ratio: str) -> dict:
    """
    Cuts the picture to the ratio (centre crop, only when it is outside the 1 % tolerance), checks the minimum size,
    shrinks it to the recommended width and returns the bytes to upload. A JPEG or PNG that needs no change goes up as it is.
    """
    from PIL import Image, ImageOps

    spec = SPECS[ratio]
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # Pillow raises several types for a broken or foreign file
        raise ImageSourceError(f"the file is not a readable image ({type(exc).__name__})")
    source_format = image.format or "UNKNOWN"
    image = ImageOps.exif_transpose(image)
    source_w, source_h = image.size

    cropped = False
    width, height = source_w, source_h
    if abs((width / height) / spec["ratio"] - 1) > RATIO_TOLERANCE:
        if width / height > spec["ratio"]:
            new_w = round(height * spec["ratio"])
            left = (width - new_w) // 2
            box = (left, 0, left + new_w, height)
        else:
            new_h = round(width / spec["ratio"])
            top = (height - new_h) // 2
            box = (0, top, width, top + new_h)
        image = image.crop(box)
        cropped = True
        width, height = image.size

    min_w, min_h = spec["min"]
    if width < min_w or height < min_h:
        raise ImageSourceError(
            f"the picture is {width}x{height} after cutting to {spec['label']}; Google needs at least {min_w}x{min_h}")

    resized = False
    if width > spec["max_width"]:
        new_w = spec["max_width"]
        image = image.resize((new_w, round(new_w * height / width)), Image.LANCZOS)
        width, height = image.size
        resized = True

    if not cropped and not resized and source_format in ("JPEG", "PNG") and len(data) <= MAX_ASSET_BYTES:
        payload, mime = data, f"IMAGE_{source_format}"
    else:
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            image = flat
        else:
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=90, optimize=True)
        payload, mime = out.getvalue(), "IMAGE_JPEG"

    if len(payload) > MAX_ASSET_BYTES:
        raise ImageSourceError(f"the picture is {len(payload) // 1024} KB; Google accepts at most {MAX_ASSET_BYTES // 1024} KB")

    return {
        "bytes": payload, "mime": mime, "width": width, "height": height,
        "source_format": source_format, "source_size": (source_w, source_h),
        "cropped": cropped, "resized": resized,
        "sha8": hashlib.sha256(payload).hexdigest()[:8],
    }


def _asset_name(req: CreateImageAssetRequest, prepared: dict) -> str:
    """Same content = same name: a repeat finds the asset again instead of filling the library with copies."""
    if req.name:
        base = req.name
    else:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(urlsplit(req.source_url).path).stem).strip("-")[:40] or "image"
        base = f"{policy.text('site.asset_name_prefix') or 'Site image'} {stem} {SPECS[req.ratio]['label']}"
    return f"{base} {prepared['sha8']}"


def _refusal(code: str, message: str, trigger: str) -> ServiceResult:
    return ServiceResult(
        success=False,
        error=ApiError(code=code, message=message, trigger=trigger, field_path=["source_url"]),
        telemetry=Telemetry(latency_ms=0.0))


@handle_google_ads_errors
def create_image_asset(req: CreateImageAssetRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Downloads an image of the advertiser's site (policy site.allowed_hosts), cuts it to the ratio, uploads it as an ImageAsset and reads it back.
    dry_run=True stops after the check and writes nothing. A picture already uploaded (same content, same name) is
    reused. The asset is not linked anywhere: use link_asset_to_entity for that.
    """
    try:
        prepared = prepare_image(fetch_site_image(req.source_url), req.ratio)
    except ValueError as exc:  # ImageSourceError and a redirect that left the site
        return _refusal("IMAGE_SOURCE_REJECTED", str(exc), req.source_url)

    name = _asset_name(req, prepared)
    report = (f"{prepared['width']}x{prepared['height']} ({SPECS[req.ratio]['label']}), {len(prepared['bytes']) // 1024} KB "
              f"{prepared['mime']}; source {prepared['source_format']} {prepared['source_size'][0]}x{prepared['source_size'][1]}"
              + (", cut to the ratio" if prepared["cropped"] else "") + (", shrunk" if prepared["resized"] else ""))
    if req.dry_run:
        return f"DRY RUN: '{name}' would be uploaded: {report}. Nothing was written to the account."

    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    existing = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT asset.id, asset.name
        FROM asset
        WHERE asset.type = 'IMAGE' AND asset.name = '{name}'
        LIMIT 1
    """))
    if existing:
        return f"Image asset {existing[0].asset.id} '{name}' is already in the library; nothing uploaded ({report})."

    operation = client.get_type("AssetOperation")
    asset = operation.create
    asset.name = name
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = prepared["bytes"]
    asset.image_asset.file_size = len(prepared["bytes"])
    asset.image_asset.mime_type = getattr(client.enums.MimeTypeEnum, prepared["mime"])
    asset.image_asset.full_size.width_pixels = prepared["width"]
    asset.image_asset.full_size.height_pixels = prepared["height"]
    response = client.get_service("AssetService").mutate_assets(customer_id=customer_id, operations=[operation])
    asset_id = response.results[0].resource_name.split("/")[-1]

    back = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT asset.id, asset.name, asset.image_asset.full_size.width_pixels, asset.image_asset.full_size.height_pixels
        FROM asset
        WHERE asset.id = {asset_id}
        LIMIT 1
    """))
    if not back:
        return _refusal("NOT_APPLIED", f"Google accepted the upload (asset {asset_id}) but the asset cannot be read back.", req.source_url)
    row = back[0].asset
    return (f"Image asset {asset_id} '{row.name}' created: {row.image_asset.full_size.width_pixels}x"
            f"{row.image_asset.full_size.height_pixels} ({report}). Not linked yet: "
            f"link_asset_to_entity(level='AD_GROUP', entity_id=<ad group>, asset_id='{asset_id}', field_type='AD_IMAGE').")
