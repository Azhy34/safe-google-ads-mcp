from typing import List, Optional, Literal, Dict, Any
from urllib.parse import urlsplit
from pydantic import BaseModel, Field, field_validator, model_validator

from core import policy

# --- ASSET PAYLOAD DATA ---

class ImageAssetData(BaseModel):
    url: str = Field(..., description="CDN URL of full-size image")
    file_size: Optional[int] = Field(None, description="Image file size in bytes")
    aspect_ratio: Optional[str] = Field(None, description="1:1 Square or 1.91:1 Landscape")

class SitelinkAssetData(BaseModel):
    link_text: str = Field(..., max_length=25, description="Sitelink display text")
    description1: Optional[str] = Field(None, max_length=35, description="Sitelink line 1")
    description2: Optional[str] = Field(None, max_length=35, description="Sitelink line 2")

class StructuredSnippetAssetData(BaseModel):
    header: str = Field(..., description="Snippet header (e.g. Models, Styles, Types)")
    values: List[str] = Field(default_factory=list, description="List of snippet values")

class CalloutAssetData(BaseModel):
    callout_text: str = Field(..., max_length=25, description="Callout text")

class PromotionAssetData(BaseModel):
    promotion_target: str = Field(..., description="Promoted item/category")
    discount_modifier: Optional[str] = Field(None, description="UP_TO, etc.")
    percent_off: Optional[int] = Field(None, description="Discount percentage, e.g. 5 for 5%")
    promotion_code: Optional[str] = Field(default_factory=lambda: policy.text("promotion.default_code") or None, description="Coupon code (default: promotion.default_code of the policy)")

class PriceAssetData(BaseModel):
    header: Optional[str] = Field(None, description="Price header or category")
    price_eur: Optional[float] = Field(None, description="Price in EUR")

# --- ASSET RECORD ---

class GoogleAssetRecord(BaseModel):
    id: str = Field(..., description="Unique Asset ID")
    resource_name: Optional[str] = Field(None, description="Asset resource name")
    name: Optional[str] = Field(None, description="Asset name in Google Ads")
    type: str = Field(..., description="IMAGE, SITELINK, STRUCTURED_SNIPPET, CALLOUT, PROMOTION, BUSINESS_NAME, BUSINESS_LOGO, PRICE, etc.")
    image_data: Optional[ImageAssetData] = None
    sitelink_data: Optional[SitelinkAssetData] = None
    snippet_data: Optional[StructuredSnippetAssetData] = None
    callout_data: Optional[CalloutAssetData] = None
    promotion_data: Optional[PromotionAssetData] = None
    price_data: Optional[PriceAssetData] = None

# --- TAB 1: ASSOCIATIONS ---

class AssetAssociationRecord(BaseModel):
    level: Literal["CUSTOMER", "CAMPAIGN", "AD_GROUP"] = Field(..., description="Level where asset is attached")
    entity_id: str = Field(..., description="Customer, Campaign, or AdGroup ID")
    entity_name: Optional[str] = Field(None, description="Name of the campaign or ad group")
    asset_id: str = Field(..., description="Asset ID")
    asset_name: Optional[str] = Field(None, description="Name of the asset")
    asset_text: Optional[str] = Field(None, description="Text preview (headline, description, sitelink text, snippet header, etc.)")
    asset_image_url: Optional[str] = Field(None, description="Full size CDN image URL for visual assets")
    asset_size: Optional[str] = Field(None, description="Pixel size of an image asset, e.g. '1200x628 (1.91:1)' (1.00:1 = square); None for text assets")
    field_type: str = Field(..., description="IMAGE, BUSINESS_NAME, BUSINESS_LOGO, SITELINK, CALLOUT, STRUCTURED_SNIPPET, PROMOTION, PRICE, etc.")
    status: str = Field(default="ENABLED", description="Binding status: ENABLED, PAUSED, REMOVED")

class AssetAssociationsRequest(BaseModel):
    """
    Filters of get_assets_associations. Without campaign_id / ad_group_id the tool answers for the whole account
    (150 rows, ~70 000 characters on 31.10.2026, more than a tool answer can hold): name the campaign or the ad group.
    campaign_id = that campaign and its ad groups (no account-level rows); ad_group_id = that ad group only.
    The values go into GAQL, so they are checked here (digits only, letters and underscores only).
    """
    level: Optional[Literal["CUSTOMER", "CAMPAIGN", "AD_GROUP"]] = Field(None, description="Only this level: CUSTOMER (account), CAMPAIGN or AD_GROUP; empty = all three")
    field_type: Optional[str] = Field(None, pattern=r"^[A-Z_]{3,40}$", description="Only this field type: AD_IMAGE (search ad image), SITELINK, CALLOUT, STRUCTURED_SNIPPET, PROMOTION, PRICE, BUSINESS_NAME, BUSINESS_LOGO, ...")
    campaign_id: Optional[str] = Field(None, pattern=r"^\d{1,20}$", description="Numeric campaign ID: that campaign and its ad groups (get_campaigns lists them)")
    ad_group_id: Optional[str] = Field(None, pattern=r"^\d{1,20}$", description="Numeric ad group ID: that ad group only (get_ad_groups lists them)")

    @field_validator("level", "field_type", mode="before")
    @classmethod
    def _upper(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _levels_fit_the_ids(self):
        if self.ad_group_id and self.level in ("CUSTOMER", "CAMPAIGN"):
            raise ValueError("ad_group_id is an ad group filter: leave level empty or use AD_GROUP")
        if self.campaign_id and self.level == "CUSTOMER":
            raise ValueError("campaign_id has no account-level rows: leave level empty, CAMPAIGN or AD_GROUP")
        return self

# --- TAB 2: PERFORMANCE ---

class AssetPerformanceRecord(BaseModel):
    asset_id: str = Field(..., description="Asset ID")
    asset_name: Optional[str] = Field(None, description="Asset Name")
    asset_text: Optional[str] = Field(None, description="Text content (Headline, Description, Sitelink text, Snippet, Callout)")
    asset_image_url: Optional[str] = Field(None, description="Image URL for visual creatives")
    field_type: str = Field(..., description="HEADLINE, DESCRIPTION, IMAGE, SITELINK, CALLOUT, PRICE, etc.")
    level: str = Field(..., description="Origin level: AD_ASSET, CAMPAIGN_ASSET, CUSTOMER_ASSET, AD_GROUP_ASSET")
    campaign_name: Optional[str] = Field(None, description="Parent Campaign Name")
    ad_group_name: Optional[str] = Field(None, description="Parent Ad Group Name")
    impressions: int = Field(default=0, description="Total impressions")
    clicks: int = Field(default=0, description="Total clicks")
    ctr_percent: float = Field(default=0.0, description="Click-through rate %")
    cost_eur: float = Field(default=0.0, description="Total cost in EUR")
    performance_label: str = Field(default="UNKNOWN", description="Google AI Label (BEST, GOOD, LOW, LEARNING, ...) for in-ad assets; HEURISTIC_* = own CTR-based estimate for extensions")

# --- TAB 3: ASSET DIVERSITY & HEALTH ---

class CampaignAssetDiversity(BaseModel):
    campaign_id: str = Field(..., description="Campaign ID")
    campaign_name: str = Field(..., description="Campaign Name")
    has_images: bool = Field(default=False, description="Has image assets")
    image_count: int = Field(default=0, description="Number of linked images")
    has_sitelinks: bool = Field(default=False, description="Has sitelinks")
    sitelink_count: int = Field(default=0, description="Number of linked sitelinks (min 4 recommended)")
    has_snippets: bool = Field(default=False, description="Has structured snippets")
    has_callouts: bool = Field(default=False, description="Has callout extensions")
    has_promotions: bool = Field(default=False, description="Has promotion extension")
    has_price: bool = Field(default=False, description="Has price extension")
    has_business_name: bool = Field(default=False, description="Has business name")
    has_business_logo: bool = Field(default=False, description="Has business logo")
    missing_asset_types: List[str] = Field(default_factory=list, description="Recommended assets that are currently missing")
    diversity_score: int = Field(default=0, description="Asset Diversity Quality Score 0-100%")

# --- MUTATIONS: LINK / UNLINK ---

class LinkAssetRequest(BaseModel):
    level: Literal["CUSTOMER", "CAMPAIGN", "AD_GROUP"] = Field(..., description="Target level")
    entity_id: str = Field(..., description="Customer ID, Campaign ID, or Ad Group ID")
    asset_id: str = Field(..., description="Asset ID to link")
    field_type: str = Field(..., description="AD_IMAGE (search ad image; there is no IMAGE in the API), SITELINK, CALLOUT, PROMOTION, STRUCTURED_SNIPPET, BUSINESS_NAME, BUSINESS_LOGO, PRICE")

class UnlinkAssetRequest(BaseModel):
    level: Literal["CUSTOMER", "CAMPAIGN", "AD_GROUP"] = Field(..., description="Target level")
    entity_id: str = Field(..., description="Customer ID, Campaign ID, or Ad Group ID")
    asset_id: str = Field(..., description="Asset ID to unlink")
    field_type: str = Field(..., description="AD_IMAGE (search ad image; there is no IMAGE in the API), SITELINK, CALLOUT, PROMOTION, STRUCTURED_SNIPPET, BUSINESS_NAME, BUSINESS_LOGO, PRICE")

class PriceOfferingItem(BaseModel):
    header: str = Field(..., description="Offering title, e.g. 150 x 250 cm")
    description: str = Field(..., description="Offering description, e.g. Premium Leather Shoes")
    price_eur: float = Field(..., description="Price in EUR, e.g. 149.00")
    final_url: str = Field(..., description="Landing URL with tracking/coupon")

class CreatePriceAssetRequest(BaseModel):
    name: str = Field(..., description="Name of the price asset in Google Ads")
    price_type: Literal["PRODUCT_CATEGORIES", "PRODUCT_TIERS", "SERVICES"] = Field(default="PRODUCT_TIERS", description="Price extension type")
    price_qualifier: Literal["FROM", "UP_TO", "AVERAGE", "UNSPECIFIED"] = Field(default="FROM", description="Price qualifier")
    language_code: str = Field(default="de", description="2-letter language code")
    offerings: List[PriceOfferingItem] = Field(..., min_items=3, max_items=8, description="List of 3 to 8 price offerings")

# --- MUTATIONS: SITELINKS AND CALLOUTS OF THE ADVERTISER'S OWN SITE ---
# Allowed hosts and the words kept out of the site's ads come from the business policy (site.allowed_hosts and
# site.forbidden_terms); with no hosts configured the site tools refuse to run.
# Exact header strings Google accepts. German column of the localized-headers table, plus the English
# headers this shop's second language uses. "Motive", "Reiseziele" and "Versicherungsschutz" are not in that table.
APPROVED_SNIPPET_HEADERS = (
    "Typen", "Modelle", "Stile", "Dienstleistungen", "Marken", "Ausstattung", "Kurse",
    "Ziele", "Studiengänge", "Viertel", "Versicherungsleistung",
    "Types", "Models", "Styles", "Brands", "Services",
)
SNIPPET_HEADER_HINTS = {
    "reiseziele": "Ziele",
    "versicherungsschutz": "Versicherungsleistung",
    "motive": None,
}

class SitelinkItem(BaseModel):
    link_text: str = Field(..., min_length=1, max_length=25, description="Sitelink text, up to 25 characters")
    description1: str = Field(..., min_length=1, max_length=35, description="Description line 1, up to 35 characters")
    description2: str = Field(..., min_length=1, max_length=35, description="Description line 2, up to 35 characters")
    final_url: str = Field(..., description="Clean https address of a page on the advertiser's site (policy site.allowed_hosts), no query string")

    @field_validator("link_text", "description1", "description2", "final_url", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("final_url")
    @classmethod
    def _url_on_the_site(cls, value: str) -> str:
        parts = urlsplit(value)
        hosts = policy.require_site_hosts("sitelinks")
        if parts.scheme != "https" or (parts.hostname or "").lower() not in hosts:
            raise ValueError(f"final_url must be an https address on {' or '.join(hosts)}")
        if parts.query or parts.fragment:
            raise ValueError("final_url must be clean: no query string and no #anchor (auto-tagging adds the gclid itself)")
        return value

    @model_validator(mode="after")
    def _no_forbidden_words(self):
        text = " ".join((self.link_text, self.description1, self.description2, self.final_url)).lower()
        for term in policy.site_forbidden_terms():
            if term in text:
                raise ValueError(f"'{term}' is not allowed in the site's ads (policy site.forbidden_terms)")
        return self

class CreateSitelinkAssetsRequest(BaseModel):
    campaign_id: str = Field(..., pattern=r"^\d+$", description="Campaign the sitelinks are attached to")
    sitelinks: List[SitelinkItem] = Field(..., min_length=2, max_length=6, description="2 to 6 sitelinks (Google shows two or more; the site rules want at least 4 on the campaign)")

    @model_validator(mode="after")
    def _unique(self):
        texts = [s.link_text.casefold() for s in self.sitelinks]
        urls = [s.final_url.rstrip("/").casefold() for s in self.sitelinks]
        if len(set(texts)) != len(texts):
            raise ValueError("link_text must be unique within the request")
        if len(set(urls)) != len(urls):
            raise ValueError("final_url must be unique within the request")
        return self


def _clean_ad_texts(values, label: str) -> List[str]:
    """Strip each text, enforce 1–25 characters, the site's forbidden words, and case-insensitive uniqueness."""
    cleaned = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"each {label} must be text")
        text = value.strip()
        if not text or len(text) > 25:
            raise ValueError(f"each {label} must be 1 to 25 characters")
        lowered = text.lower()
        for term in policy.site_forbidden_terms():
            if term in lowered:
                raise ValueError(f"'{term}' is not allowed in the site's ads (policy site.forbidden_terms)")
        cleaned.append(text)
    keys = [text.casefold() for text in cleaned]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{label}s must be unique ignoring case")
    return cleaned


class CreateCalloutAssetsRequest(BaseModel):
    campaign_id: str = Field(..., pattern=r"^\d+$", description="Campaign the callouts are attached to")
    callouts: List[str] = Field(..., min_length=2, max_length=10, description="2 to 10 callout texts, each 1 to 25 characters")

    @field_validator("callouts")
    @classmethod
    def _callouts(cls, values):
        return _clean_ad_texts(values, "callout")


class CreateStructuredSnippetAssetRequest(BaseModel):
    campaign_id: str = Field(..., pattern=r"^\d+$", description="Campaign the snippet is attached to")
    header: str = Field(..., description="One header from APPROVED_SNIPPET_HEADERS, the exact Google string")
    values: List[str] = Field(..., min_length=3, max_length=10, description="3 to 10 snippet values, each 1 to 25 characters")

    @field_validator("header")
    @classmethod
    def _header(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("header must be text")
        header = value.strip()
        if header in APPROVED_SNIPPET_HEADERS:
            return header
        hint = SNIPPET_HEADER_HINTS.get(header.casefold())
        if hint:
            raise ValueError(f"'{header}' is not a Google snippet header; use '{hint}'")
        raise ValueError(f"'{header}' is not a Google snippet header (HEADER_NOT_SUPPORTED). Allowed: {', '.join(APPROVED_SNIPPET_HEADERS)}")

    @field_validator("values")
    @classmethod
    def _values(cls, values):
        return _clean_ad_texts(values, "value")

class SyncPriceExtensionRequest(BaseModel):
    entity_id: str = Field(..., description="Target entity ID: Campaign ID, Ad Group ID, or Customer ID")
    level: Literal["CUSTOMER", "CAMPAIGN", "AD_GROUP"] = Field(default="CAMPAIGN", description="Target level: CAMPAIGN (default), AD_GROUP, or CUSTOMER")
    name: str = Field(..., description="Descriptive name for the new Price Asset, e.g. 'Category prices 2026-10'")
    offerings: List[PriceOfferingItem] = Field(..., min_items=3, max_items=8, description="List of 3 to 8 price offerings")
    price_type: Literal["PRODUCT_CATEGORIES", "PRODUCT_TIERS", "SERVICES"] = Field(default="PRODUCT_TIERS", description="Price extension type")
    price_qualifier: Literal["FROM", "UP_TO", "AVERAGE", "UNSPECIFIED"] = Field(default="FROM", description="Price qualifier")
    language_code: str = Field(default="de", description="2-letter language code")


