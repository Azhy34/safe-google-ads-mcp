"""
Which products a Shopping campaign advertises: the owner's list of offer IDs, checked against what the catalog really holds.

A product tree that names an offer ID the catalog does not have is valid for Google and simply matches nothing, so a typo
would become an empty campaign. The IDs are therefore resolved first, against what Google Ads itself lists
(shopping_product: the IDs are spelled there as in the feed, seen live 31.10.2026: WE-003-150x250) and against the Merchant Center catalog.
An ID that neither knows is refused.

Whether a product can show is read from Merchant Center (approved, waiting for the first review, disapproved for the
campaign's countries), not from the Google Ads status: before a Shopping campaign includes the products, Google Ads lists
every one of them as NOT_ELIGIBLE (seen live 31.10.2026: 528 of 528), which says nothing about the product.
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.ads.googleads.errors import GoogleAdsException

from core.merchant_client import MerchantApiError, merchant_pages
from shield.models_shopping import CreateShoppingCampaignRequest
from tools.shopping_readiness_tools import GEO_COUNTRIES

APPROVED, PENDING, DISAPPROVED, UNKNOWN = "APPROVED", "PENDING", "DISAPPROVED", "UNKNOWN"


@dataclass
class Resolution:
    """
    The offers of a request, as the tree must write them. `source` is where they were found: google_ads, merchant_center, both
    or none. `not_eligible` are the offers Merchant Center disapproved for the campaign's countries, `pending` the ones still
    waiting for the first review; `approval_checked` is False when Merchant Center could not say anything about approval.
    """
    item_ids: List[str] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)
    not_eligible: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)
    source: str = "none"
    approval_checked: bool = False

    @property
    def unreadable(self) -> bool:
        """Neither Google Ads nor Merchant Center could say what the catalog holds: nothing can be checked."""
        return self.source == "none" and not self.item_ids and not self.unknown


def read_ads_items(ga_service, customer_id: str, req: CreateShoppingCampaignRequest) -> Set[str]:
    """item_id, spelled as Google Ads spells it, of the products of this Merchant Center and feed label."""
    items: Set[str] = set()
    try:
        for row in ga_service.search(customer_id=customer_id, query="""
            SELECT shopping_product.merchant_center_id, shopping_product.feed_label, shopping_product.item_id
            FROM shopping_product"""):
            product = row.shopping_product
            if str(product.merchant_center_id) == req.merchant_id and product.feed_label == req.feed_label:
                items.add(product.item_id)
    except GoogleAdsException:
        return set()  # the report may refuse an account without Shopping campaigns: Merchant Center answers then
    return items


def shopping_ads_state(product: Dict, countries: List[str]) -> str:
    """APPROVED / PENDING / DISAPPROVED of one Merchant Center product for Shopping ads in all the given countries."""
    for destination in (product.get("productStatus") or {}).get("destinationStatuses") or []:
        if destination.get("reportingContext") != "SHOPPING_ADS" or not countries:
            continue
        if any(country in (destination.get("disapprovedCountries") or []) for country in countries):
            return DISAPPROVED
        if any(country in (destination.get("pendingCountries") or []) for country in countries):
            return PENDING
        if all(country in (destination.get("approvedCountries") or []) for country in countries):
            return APPROVED
    return UNKNOWN


def read_gmc_states(req: CreateShoppingCampaignRequest) -> Optional[Dict[str, str]]:
    """offerId -> Shopping ads state in the campaign's countries, for every product of the Merchant Center catalog; None when unreadable."""
    countries = [GEO_COUNTRIES[geo] for geo in req.geo_target_constants if geo in GEO_COUNTRIES]
    try:
        products = merchant_pages(f"products/v1/accounts/{req.merchant_id}/products", "products", "products.list", page_size=1000)
    except MerchantApiError:
        return None
    return {product["offerId"]: shopping_ads_state(product, countries) for product in products if product.get("offerId")}


def resolve_offers(requested: List[str], ads_items: Iterable[str], gmc_states: Optional[Dict[str, str]]) -> Resolution:
    """Pure part: matches the requested IDs without regard to case. Google Ads spells the ID, Merchant Center says whether it can show."""
    in_ads = {item.lower(): item for item in ads_items}
    in_gmc = {offer.lower(): (offer, state) for offer, state in (gmc_states or {}).items()}
    result = Resolution(source="+".join(name for name, found in (("google_ads", in_ads), ("merchant_center", in_gmc)) if found) or "none",
                        approval_checked=bool(in_gmc))
    if not in_ads and not in_gmc:
        return result
    for offer in requested:
        key = offer.lower()
        if key in in_ads:
            result.item_ids.append(in_ads[key])
        elif key in in_gmc:
            result.item_ids.append(in_gmc[key][0])  # the catalog's own spelling
        else:
            result.unknown.append(offer)
            continue
        state = in_gmc[key][1] if key in in_gmc else None
        if state == DISAPPROVED:
            result.not_eligible.append(offer)
        elif state == PENDING:
            result.pending.append(offer)
    return result


def resolve_selection(ga_service, customer_id: str, req: CreateShoppingCampaignRequest) -> Resolution:
    """Reads both catalogs and resolves the request."""
    return resolve_offers(req.offers, read_ads_items(ga_service, customer_id, req), read_gmc_states(req))
