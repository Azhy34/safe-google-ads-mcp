"""
check_shopping_readiness: can a Shopping campaign on the Merchant Center feed show a product today?

create_shopping_campaign knew one thing about Merchant Center, that the two accounts are linked. Whether a product shows
depends on more: the Shopping ads program is on for the country, the feed label exists, Google approved products, Google
Ads itself sees them. This tool reads both sides (Google Ads through the account client, Merchant Center through
core/merchant_client.py) and answers with a verdict and the list of checks, so the agent does not need a second MCP.

Reads only. When Merchant Center cannot be read (no gcloud, the owner's session is stale, no access) its checks are
SKIPPED with the reason and the verdict is at most READY_WITH_WARNINGS: "could not check" is not "broken". The reason
also goes to the journal entry (gmc_skipped_reason) together with the verdict, because a read does not log its result.
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.ads.googleads.errors import GoogleAdsException

from core.client import get_ads_client
from core.config import CUSTOMER_ID, MERCHANT_CENTER_ID
from core.merchant_client import ACCESS_FAILURE_CODES, MerchantApiError, merchant_pages, merchant_request
from shield.error_shield import handle_google_ads_errors
from shield.models_shopping import CheckShoppingReadinessRequest, ReadinessCheck, ShoppingReadinessResponse
from shield.telemetry import note_journal

GEO_COUNTRIES = {2276: "DE", 2040: "AT"}  # geo target -> Merchant Center country code
PENDING_INVITATIONS = ("REQUESTED", "PENDING_APPROVAL")
MAX_LISTED = 5


@dataclass
class AdsFacts:
    """What Google Ads says. product_statuses is None when the shopping_product report could not be read."""
    linked: bool = False
    pending_invitation: Optional[str] = None
    product_statuses: Optional[Dict[str, int]] = None
    other_campaigns: List[str] = field(default_factory=list)


@dataclass
class GmcState:
    """What Merchant Center says. A part is None when it could not be read: `skipped` says why for the whole read, `errors` per part."""
    program: Optional[Dict[str, Any]] = None
    feed_labels: Optional[Set[str]] = None
    counts: Optional[Dict[str, Dict[str, int]]] = None
    custom_label_0: Optional[Set[str]] = None
    skipped: Optional[str] = None
    skipped_detail: str = ""
    errors: Dict[str, str] = field(default_factory=dict)


def _to_int(value: Any) -> int:
    """Google sends int64 as strings ("521")."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --- reading: Google Ads ---

def read_ads_facts(client, customer_id: str, req: CheckShoppingReadinessRequest) -> AdsFacts:
    ga_service = client.get_service("GoogleAdsService")
    facts = AdsFacts()

    facts.linked = any(str(row.product_link.merchant_center.merchant_center_id) == req.merchant_id for row in ga_service.search(
        customer_id=customer_id, query="SELECT product_link.product_link_id, product_link.merchant_center.merchant_center_id FROM product_link"))
    if not facts.linked:
        for row in ga_service.search(customer_id=customer_id, query="""
            SELECT product_link_invitation.status, product_link_invitation.merchant_center.merchant_center_id
            FROM product_link_invitation"""):
            invitation = row.product_link_invitation
            if str(invitation.merchant_center.merchant_center_id) == req.merchant_id and invitation.status.name in PENDING_INVITATIONS:
                facts.pending_invitation = invitation.status.name

    try:  # a report that fills in with a delay of up to 24 h and may refuse an account without Shopping campaigns: a WARN, not a failure
        statuses: Dict[str, int] = {}
        for row in ga_service.search(customer_id=customer_id, query="""
            SELECT shopping_product.merchant_center_id, shopping_product.feed_label, shopping_product.status
            FROM shopping_product"""):
            product = row.shopping_product
            if str(product.merchant_center_id) == req.merchant_id and product.feed_label == req.feed_label:
                statuses[product.status.name] = statuses.get(product.status.name, 0) + 1
        facts.product_statuses = statuses
    except GoogleAdsException:
        pass

    facts.other_campaigns = [row.campaign.name for row in ga_service.search(customer_id=customer_id, query="""
        SELECT campaign.name, campaign.shopping_setting.merchant_id FROM campaign
        WHERE campaign.advertising_channel_type = 'SHOPPING' AND campaign.status != 'REMOVED'""")
        if str(row.campaign.shopping_setting.merchant_id) == req.merchant_id]
    return facts


# --- reading: Merchant Center ---

def _read_program(account: str) -> Dict[str, Any]:
    return merchant_request("GET", f"accounts/v1/{account}/programs/shopping-ads", "programs.get")


def _read_feed_labels(account: str) -> Set[str]:
    sources = merchant_pages(f"datasources/v1/{account}/dataSources", "dataSources", "dataSources.list")
    return {(source.get("primaryProductDataSource") or {}).get("feedLabel") for source in sources} - {None}


def _read_status_counts(account: str) -> Dict[str, Dict[str, int]]:
    """Products of the Shopping ads context per country. Google also sends service rows of other contexts: left out."""
    rows = merchant_pages(f"issueresolution/v1/{account}/aggregateProductStatuses", "aggregateProductStatuses",
                          "aggregateProductStatuses.list")
    counts: Dict[str, Dict[str, int]] = {}
    for row in rows:
        if row.get("reportingContext") == "SHOPPING_ADS":
            stats = row.get("stats") or {}
            counts[row.get("country")] = {key: _to_int(stats.get(f"{key}Count")) for key in ("active", "pending", "disapproved")}
    return counts


def _read_custom_label_0(account: str) -> Set[str]:
    products = merchant_pages(f"products/v1/{account}/products", "products", "products.list", page_size=1000)
    return {(product.get("productAttributes") or {}).get("customLabel0") for product in products} - {None, ""}


def read_gmc_state(req: CheckShoppingReadinessRequest) -> GmcState:
    state = GmcState()
    if req.merchant_id != MERCHANT_CENTER_ID:
        state.skipped, state.skipped_detail = "FOREIGN_MERCHANT_ID", f"the service account reads Merchant Center {MERCHANT_CENTER_ID} only"
        return state
    account = f"accounts/{req.merchant_id}"
    parts = {"program": _read_program, "feed_labels": _read_feed_labels, "counts": _read_status_counts}
    if req.custom_label_0_values:
        parts["custom_label_0"] = _read_custom_label_0
    for part, read in parts.items():
        try:
            setattr(state, part, read(account))
        except MerchantApiError as e:
            if e.code in ACCESS_FAILURE_CODES:  # nothing else will answer either
                state.skipped, state.skipped_detail = e.code, e.message
                break
            state.errors[part] = e.code
    return state


# --- judging: no network below this line ---

def _unread(code: str, part: Optional[Any], key: str, gmc: GmcState) -> Optional[ReadinessCheck]:
    """The SKIPPED check of a Merchant Center part that has no data, or None when the part was read."""
    if part is not None:
        return None
    if gmc.skipped:
        return ReadinessCheck(code=code, side="GMC", status="SKIPPED", detail=f"Merchant Center was not read ({gmc.skipped}): {gmc.skipped_detail}")
    return ReadinessCheck(code=code, side="GMC", status="SKIPPED", detail=f"This part of Merchant Center was not read ({gmc.errors.get(key, 'no data')}).")


def _program_check(countries: List[str], unmapped: List[int], gmc: GmcState) -> ReadinessCheck:
    skipped = _unread("GMC_PROGRAM", gmc.program, "program", gmc)
    if skipped:
        return skipped
    state = gmc.program.get("state")
    regions = set(gmc.program.get("activeRegionCodes") or [])
    missing = [country for country in countries if country not in regions]
    unmet = [r.get("title") for r in gmc.program.get("unmetRequirements") or [] if set(r.get("affectedRegionCodes") or []) & set(countries)]
    if state != "ENABLED":
        return ReadinessCheck(code="GMC_PROGRAM", side="GMC", status="FAIL", detail=f"Shopping ads program in Merchant Center: {state}, it must be ENABLED.")
    if missing:
        return ReadinessCheck(code="GMC_PROGRAM", side="GMC", status="FAIL", detail=f"The Shopping ads program is not active in: {', '.join(missing)}.")
    if unmet or unmapped:
        notes = ([f"requirements not met: {', '.join(unmet)}"] if unmet else []) + \
                ([f"geo targets {unmapped} are outside the DE/AT map, the country was not checked"] if unmapped else [])
        return ReadinessCheck(code="GMC_PROGRAM", side="GMC", status="WARN", detail=f"The program is enabled ({', '.join(countries) or 'no countries'}), but {'; '.join(notes)}.")
    return ReadinessCheck(code="GMC_PROGRAM", side="GMC", status="OK", detail=f"The Shopping ads program is enabled: {', '.join(countries)}.")


def _feed_label_check(label: str, gmc: GmcState) -> ReadinessCheck:
    skipped = _unread("GMC_FEED_LABEL", gmc.feed_labels, "feed_labels", gmc)
    if skipped:
        return skipped
    if label in gmc.feed_labels:
        return ReadinessCheck(code="GMC_FEED_LABEL", side="GMC", status="OK", detail=f"A data source with the label {label} exists.")
    return ReadinessCheck(code="GMC_FEED_LABEL", side="GMC", status="FAIL",
                          detail=f"No data source with the label {label}. There are: {', '.join(sorted(gmc.feed_labels)) or 'none'}.")


def _products_check(countries: List[str], gmc: GmcState) -> ReadinessCheck:
    skipped = _unread("GMC_PRODUCTS", gmc.counts, "counts", gmc)
    if skipped:
        return skipped
    rows = {country: gmc.counts[country] for country in countries if country in gmc.counts}
    if not rows:
        return ReadinessCheck(code="GMC_PRODUCTS", side="GMC", status="WARN",
                              detail=f"No product status summary for {', '.join(countries) or 'these countries'} yet: Google has not processed the products.")
    total = {key: sum(row[key] for row in rows.values()) for key in ("active", "pending", "disapproved")}
    text = "; ".join(f"{country}: approved {row['active']}, pending {row['pending']}, disapproved {row['disapproved']}" for country, row in rows.items())
    if total["active"] == 0:
        return ReadinessCheck(code="GMC_PRODUCTS", side="GMC", status="FAIL", detail=f"No approved products ({text}).")
    if total["pending"] or total["disapproved"]:
        return ReadinessCheck(code="GMC_PRODUCTS", side="GMC", status="WARN", detail=f"{text}.")
    return ReadinessCheck(code="GMC_PRODUCTS", side="GMC", status="OK", detail=f"{text}.")


def _ads_products_check(label: str, ads: AdsFacts) -> ReadinessCheck:
    if ads.product_statuses is None:
        return ReadinessCheck(code="ADS_SEES_PRODUCTS", side="ADS", status="WARN", detail="The Google Ads product report (shopping_product) could not be read.")
    total = sum(ads.product_statuses.values())
    if total == 0:
        return ReadinessCheck(code="ADS_SEES_PRODUCTS", side="ADS", status="WARN",
                              detail=f"Google Ads does not see products with the label {label} yet (statuses arrive with a delay of up to 24 hours).")
    text = ", ".join(f"{status} {count}" for status, count in sorted(ads.product_statuses.items()))
    if set(ads.product_statuses) == {"NOT_ELIGIBLE"}:
        if not ads.other_campaigns:   # seen live 31.10.2026: before the first Shopping campaign Google Ads lists every product as NOT_ELIGIBLE
            return ReadinessCheck(code="ADS_SEES_PRODUCTS", side="ADS", status="OK",
                                  detail=f"Google Ads sees {total} products. All are NOT_ELIGIBLE, but there is no Shopping campaign yet: the status changes once a campaign includes the products.")
        return ReadinessCheck(code="ADS_SEES_PRODUCTS", side="ADS", status="WARN", detail=f"Google Ads sees {total} products, but none can be shown ({text}).")
    return ReadinessCheck(code="ADS_SEES_PRODUCTS", side="ADS", status="OK", detail=f"Google Ads sees {total} products: {text}.")


def _custom_label_check(values: List[str], gmc: GmcState) -> ReadinessCheck:
    skipped = _unread("GMC_CUSTOM_LABEL", gmc.custom_label_0, "custom_label_0", gmc)
    if skipped:
        return skipped
    missing = [value for value in values if value not in gmc.custom_label_0]
    if missing:
        found = ", ".join(sorted(gmc.custom_label_0)[:MAX_LISTED]) or "no labels at all"
        return ReadinessCheck(code="GMC_CUSTOM_LABEL", side="GMC", status="FAIL",
                              detail=f"The catalog has no custom_label_0 values: {', '.join(missing)} (case matters). It has: {found}.")
    return ReadinessCheck(code="GMC_CUSTOM_LABEL", side="GMC", status="OK", detail=f"The custom_label_0 values exist in the catalog: {', '.join(values)}.")


def evaluate_checks(req: CheckShoppingReadinessRequest, countries: List[str], unmapped: List[int],
                    ads: AdsFacts, gmc: GmcState) -> List[ReadinessCheck]:
    if ads.linked:
        link = ReadinessCheck(code="ADS_LINK", side="ADS", status="OK", detail=f"The Google Ads account is linked to Merchant Center {req.merchant_id}.")
    elif ads.pending_invitation:
        link = ReadinessCheck(code="ADS_LINK", side="ADS", status="FAIL",
                              detail=f"No link, but there is an invitation ({ads.pending_invitation}): accept it in Google Ads or Merchant Center.")
    else:
        link = ReadinessCheck(code="ADS_LINK", side="ADS", status="FAIL", detail=f"The account is not linked to Merchant Center {req.merchant_id} (check_merchant_center_link).")
    checks = [link, _program_check(countries, unmapped, gmc), _feed_label_check(req.feed_label, gmc), _products_check(countries, gmc),
              _ads_products_check(req.feed_label, ads)]
    if req.custom_label_0_values:
        checks.append(_custom_label_check(req.custom_label_0_values, gmc))
    if ads.other_campaigns:
        others = ", ".join(ads.other_campaigns[:MAX_LISTED])
        checks.append(ReadinessCheck(code="SHOPPING_OVERLAP", side="ADS", status="WARN",
                                     detail=f"This Merchant Center already has Shopping campaigns: {others}. The same products will compete in them."))
    else:
        checks.append(ReadinessCheck(code="SHOPPING_OVERLAP", side="ADS", status="OK", detail="There are no other Shopping campaigns on this Merchant Center."))
    return checks


def verdict_of(checks: List[ReadinessCheck]) -> str:
    statuses = {check.status for check in checks}
    if "FAIL" in statuses:
        return "BLOCKED"
    return "READY_WITH_WARNINGS" if statuses & {"WARN", "SKIPPED"} else "READY"


def assess_shopping_readiness(client, customer_id: str, req: CheckShoppingReadinessRequest) -> ShoppingReadinessResponse:
    """Reads both sides and judges them. Not decorated, so that create_shopping_campaign can use it inside its own journal entry."""
    countries = [GEO_COUNTRIES[geo] for geo in req.geo_target_constants if geo in GEO_COUNTRIES]
    unmapped = [geo for geo in req.geo_target_constants if geo not in GEO_COUNTRIES]
    ads = read_ads_facts(client, customer_id, req)
    gmc = read_gmc_state(req)
    checks = evaluate_checks(req, countries, unmapped, ads, gmc)
    verdict = verdict_of(checks)

    # A read does not log its result, so what the owner will search for in Logs Explorer is put into the entry here
    note_journal(
        readiness_verdict=verdict,
        failed_checks=[c.code for c in checks if c.status == "FAIL"] or None,
        warned_checks=[c.code for c in checks if c.status in ("WARN", "SKIPPED")] or None,
        gmc_skipped_reason=gmc.skipped,
        gmc_read_errors=gmc.errors or None,
    )
    return ShoppingReadinessResponse(
        customer_id=customer_id, merchant_id=req.merchant_id, feed_label=req.feed_label, countries=countries,
        verdict=verdict, checks=checks,
        gmc={"program_state": (gmc.program or {}).get("state"), "feed_labels": sorted(gmc.feed_labels) if gmc.feed_labels is not None else None,
             "product_counts": {c: gmc.counts[c] for c in countries if gmc.counts and c in gmc.counts} if gmc.counts is not None else None,
             "skipped": gmc.skipped},
        ads={"linked": ads.linked, "product_statuses": ads.product_statuses, "other_shopping_campaigns": ads.other_campaigns},
    )


@handle_google_ads_errors
def check_shopping_readiness(req: CheckShoppingReadinessRequest, customer_id: str = CUSTOMER_ID) -> ShoppingReadinessResponse:
    """
    Checks, on both sides, that a Shopping campaign on the Merchant Center feed has something to show: the link, the Shopping ads
    program for the countries, the feed label, approved products, what Google Ads sees, other Shopping campaigns, and (when
    asked) that the custom_label_0 values exist in the catalog. Reads only.
    """
    return assess_shopping_readiness(get_ads_client(), customer_id, req)
