"""Request and answer models of the Shopping tools: a Standard Shopping campaign on the Merchant Center feed of the site."""
import re
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from core import policy

# Merchant Center ID and feed label default to account.merchant_center_id and account.feed_label of the policy
MAX_OFFERS = 100                  # one product tree level: far below Google's 20,000 product groups per ad group


def _default_merchant() -> str:
    return policy.text("account.merchant_center_id")


def _default_feed_label() -> str:
    return policy.text("account.feed_label")


def _default_geo() -> List[int]:
    return list(policy.get("shopping.default_geo", [2276]))


def _need_merchant_and_feed(model) -> None:
    if not model.merchant_id or not model.feed_label:
        raise ValueError("merchant_id and feed_label are needed: pass them or set account.merchant_center_id and account.feed_label in the policy (docs/POLICY.md)")


def _require_values(value: List[int]) -> List[int]:
    if not value:
        raise ValueError("at least one value is required")
    return value


def _clean_custom_labels(value: List[str]) -> List[str]:
    cleaned = []
    for label in value:
        label = label.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", label):
            raise ValueError("a custom label value is lower-case letters, digits, _ and - (1-40 characters)")
        if label not in cleaned:
            cleaned.append(label)
    return cleaned


def _clean_offers(value: List[str]) -> List[str]:
    cleaned: List[str] = []
    for offer in value:
        offer = offer.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", offer):
            raise ValueError("an offer ID is letters, digits, . _ and - (1-60 characters), e.g. SKU-100-L")
        if offer.lower() not in (known.lower() for known in cleaned):
            cleaned.append(offer)
    if len(cleaned) > MAX_OFFERS:
        raise ValueError(f"at most {MAX_OFFERS} offers in one campaign")
    return cleaned


class CreateShoppingCampaignRequest(BaseModel):
    name: str = Field(..., min_length=5, max_length=120, description="Campaign name; must start with shopping.name_prefix of the policy when one is set")
    budget_eur: float = Field(default=5.0, ge=1.0, le=50.0, description="Daily budget in EUR (1.00 - 50.00)")
    cpc_ceiling_eur: float = Field(default=0.50, ge=0.10, le=3.00, description="Maximize Clicks CPC ceiling in EUR (0.10 - 3.00); later only +-0.05 via step_bid_threshold")
    merchant_id: str = Field(default_factory=_default_merchant, pattern=r"^\d{6,12}$", description="Merchant Center ID (default: account.merchant_center_id)")
    feed_label: str = Field(default_factory=_default_feed_label, pattern=r"^[A-Za-z0-9_-]{1,20}$", description="Feed label of the products to advertise (default: account.feed_label)")
    campaign_priority: int = Field(default=0, ge=0, le=2, description="Shopping campaign priority 0-2")
    geo_target_constants: List[int] = Field(default_factory=_default_geo, description="Targeted geo IDs, e.g. 2276 Germany, 2040 Austria (default: shopping.default_geo of the policy)")
    ad_group_name: str = Field(default_factory=lambda: policy.text("shopping.default_ad_group_name") or "All products", min_length=3, max_length=120, description="Name of the one Shopping ad group")
    offers: List[str] = Field(default_factory=list, description="offer_id of the products to advertise, e.g. ['SKU-100-L', 'SKU-100-S']; nothing else is advertised. Each must exist in the catalog")
    all_products: bool = Field(default=False, description="True: advertise the whole feed instead of a selection (never the default)")
    exclude_custom_label_0: List[str] = Field(default_factory=list, description="With all_products only: values of g:custom_label_0 to leave out (e.g. ['clearance'])")
    dry_run: bool = Field(default=False, description="True: check everything and build the operations, write nothing")

    @field_validator("name", "ad_group_name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9 ._+&|-]+", value):
            raise ValueError("name may use letters, digits, space and . _ + & | - only")
        return value

    @field_validator("name")
    @classmethod
    def _our_prefix(cls, value: str) -> str:
        prefix = policy.text("shopping.name_prefix")
        if prefix and not value.startswith(prefix):
            raise ValueError(f"name must start with '{prefix}' (policy shopping.name_prefix)")
        return value

    @field_validator("geo_target_constants")
    @classmethod
    def _not_empty(cls, value: List[int]) -> List[int]:
        return _require_values(value)

    @field_validator("offers")
    @classmethod
    def _offer_ids(cls, value: List[str]) -> List[str]:
        return _clean_offers(value)

    @field_validator("exclude_custom_label_0")
    @classmethod
    def _labels(cls, value: List[str]) -> List[str]:
        return _clean_custom_labels(value)

    @model_validator(mode="after")
    def _a_choice_of_products(self):
        if self.offers and self.all_products:
            raise ValueError("offers and all_products exclude each other: either a selection or the whole feed")
        if not self.offers and not self.all_products:
            raise ValueError("choose the products: offers=['SKU-100-L', ...] or, deliberately, all_products=True; the whole feed is never the default")
        if self.offers and self.exclude_custom_label_0:
            raise ValueError("exclude_custom_label_0 works with all_products only; a selection by offers already leaves out everything else")
        _need_merchant_and_feed(self)
        return self


class CheckShoppingReadinessRequest(BaseModel):
    merchant_id: str = Field(default_factory=_default_merchant, pattern=r"^\d{6,12}$", description="Merchant Center ID (default: account.merchant_center_id)")
    feed_label: str = Field(default_factory=_default_feed_label, pattern=r"^[A-Za-z0-9_-]{1,20}$", description="Feed label of the products to advertise (default: account.feed_label)")
    geo_target_constants: List[int] = Field(default_factory=_default_geo, description="Countries the campaign will target: 2276 Germany, 2040 Austria (default: shopping.default_geo)")
    custom_label_0_values: List[str] = Field(default_factory=list, description="Values of g:custom_label_0 the campaign will select or leave out; each must exist in the catalog")

    @field_validator("geo_target_constants")
    @classmethod
    def _not_empty(cls, value: List[int]) -> List[int]:
        return _require_values(value)

    @field_validator("custom_label_0_values")
    @classmethod
    def _labels(cls, value: List[str]) -> List[str]:
        return _clean_custom_labels(value)

    @model_validator(mode="after")
    def _merchant_is_set(self):
        _need_merchant_and_feed(self)
        return self


class ReadinessCheck(BaseModel):
    code: str = Field(..., description="ADS_LINK, GMC_PROGRAM, GMC_FEED_LABEL, GMC_PRODUCTS, ADS_SEES_PRODUCTS, GMC_CUSTOM_LABEL, SHOPPING_OVERLAP")
    side: Literal["ADS", "GMC"] = Field(..., description="Which account the check read")
    status: Literal["OK", "WARN", "FAIL", "SKIPPED"] = Field(..., description="SKIPPED: the check could not read its side, the detail says why")
    detail: str = Field(..., description="What was found, in plain words")


class ShoppingReadinessResponse(BaseModel):
    customer_id: str = Field(..., description="Google Ads Customer ID")
    merchant_id: str = Field(..., description="Merchant Center ID that was checked")
    feed_label: str = Field(..., description="Feed label that was checked")
    countries: List[str] = Field(default_factory=list, description="Country codes the geo targets stand for (DE, AT)")
    verdict: Literal["READY", "READY_WITH_WARNINGS", "BLOCKED"] = Field(..., description="BLOCKED: at least one FAIL; READY_WITH_WARNINGS: a WARN or a SKIPPED, no FAIL")
    checks: List[ReadinessCheck] = Field(default_factory=list)
    gmc: Dict[str, Any] = Field(default_factory=dict, description="What Merchant Center said: program state, feed labels, product counts per country")
    ads: Dict[str, Any] = Field(default_factory=dict, description="What Google Ads said: link, product statuses it sees, other Shopping campaigns")
