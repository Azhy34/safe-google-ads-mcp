"""Request model of create_image_asset: an image of the advertiser's own site becomes an image asset in the ad account."""
import re
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from core import policy

# Only the advertiser's own site (policy site.allowed_hosts): its og images, product photos and the image_link of the
# Merchant Center feed all live there; any other host is refused so the tool can never be used to fetch something from
# the internet, and with no host configured the tool refuses to run.


def check_site_image_url(url: str) -> str:
    """The URL when it is an https address of the advertiser's own site; ValueError otherwise (also used for every redirect)."""
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https":
        raise ValueError("source_url must be an https address")
    if parts.username or parts.password:
        raise ValueError("source_url must not contain a login or password")
    hosts = policy.require_site_hosts("create_image_asset")
    if (parts.hostname or "").lower() not in hosts:
        raise ValueError(
            f"source_url must be on {' or '.join(hosts)} "
            "(the site's og images, product photos and the Merchant Center image_link qualify)")
    if parts.port not in (None, 443):
        raise ValueError("source_url must use the standard https port")
    if not parts.path or parts.path == "/":
        raise ValueError("source_url must point to an image file")
    return parts.geturl()


class CreateImageAssetRequest(BaseModel):
    source_url: str = Field(..., description="https address of an image on the advertiser's site (policy site.allowed_hosts), e.g. https://www.example.com/img/hero.jpg")
    ratio: Literal["LANDSCAPE", "SQUARE"] = Field(..., description="LANDSCAPE = 1.91:1 (at least 600x314), SQUARE = 1:1 (at least 300x300)")
    name: Optional[str] = Field(default=None, max_length=80, description="Friendly asset name; default '<site.asset_name_prefix> <file> <ratio>'. A hash of the content is always appended")
    dry_run: bool = Field(default=False, description="True: download and check the image, write nothing to the account")

    @field_validator("source_url")
    @classmethod
    def _own_site_only(cls, value: str) -> str:
        return check_site_image_url(value)

    @field_validator("name")
    @classmethod
    def _plain_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9 ._-]{3,80}", value):
            raise ValueError("name may use letters, digits, space, dot, underscore and hyphen (3-80 characters)")
        return value
