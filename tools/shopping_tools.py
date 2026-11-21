"""
create_shopping_campaign: a Standard Shopping campaign that advertises the products of the Merchant Center feed.

Why Standard Shopping and not Performance Max: it runs on Maximize Clicks with a CPC ceiling (the same one lever as the
Site search campaign: the ceiling, stepped by 0.05 EUR), PMax needs conversion bidding and Google advises a daily budget of
at least three times the CPA. The ad is a product card (photo, price, title from the feed); there are no keywords and no texts.

Which products it advertises is always a choice: a list of offer IDs (resolved against the catalog first, see
shopping_selection.py) or, deliberately, the whole feed. Before anything is written check_shopping_readiness's judgement
(assess_shopping_readiness) must not be BLOCKED: the link, the Shopping ads program, the feed label, approved products.

The whole campaign goes in ONE GoogleAdsService.mutate (all or nothing, with temporary IDs): budget, campaign, geo and
schedule criteria, ad group, product ad and the product tree, so a failure leaves nothing behind. dry_run sends the same
request with validate_only, which makes Google itself check it before the owner is asked. A Shopping campaign takes no
language criterion (Google refuses it: the language is the feed's, seen live 31.10.2026). The campaign is always created PAUSED.
"""
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.ads.googleads.errors import GoogleAdsException

from core.client import get_ads_client, eur_to_micros
from core import policy
from core.config import CUSTOMER_ID
from shield.error_shield import ApiError, ServiceResult, Telemetry, handle_google_ads_errors
from shield.models_shopping import CheckShoppingReadinessRequest, CreateShoppingCampaignRequest, SetShoppingProductSelectionRequest
from tools.shopping_readiness_tools import assess_shopping_readiness
from tools.shopping_selection import resolve_selection

# Temporary IDs are unique across the whole request, whatever the resource: the root of the product tree (adGroupId~criterionId) may not reuse -1 of the budget
TEMP_BUDGET, TEMP_CAMPAIGN, TEMP_AD_GROUP, TEMP_TREE_ROOT = -1, -2, -3, -4


def _refusal(code: str, message: str, trigger: str = "", field: str = "name") -> ServiceResult:
    return ServiceResult(
        success=False,
        error=ApiError(code=code, message=message, trigger=trigger, field_path=[field]),
        telemetry=Telemetry(latency_ms=0.0))


def _rn(customer_id: str, collection: str, ident) -> str:
    return f"customers/{customer_id}/{collection}/{ident}"


def build_campaign_operations(client, customer_id: str, req: CreateShoppingCampaignRequest) -> list:
    """The campaign part of the request: every operation uses temporary IDs, Google resolves them inside the one mutate."""
    budget_rn = _rn(customer_id, "campaignBudgets", TEMP_BUDGET)
    campaign_rn = _rn(customer_id, "campaigns", TEMP_CAMPAIGN)
    ad_group_rn = _rn(customer_id, "adGroups", TEMP_AD_GROUP)
    ceiling = eur_to_micros(req.cpc_ceiling_eur)
    operations = []

    op = client.get_type("MutateOperation")
    budget = op.campaign_budget_operation.create
    budget.resource_name = budget_rn
    budget.name = f"Budget - {req.name}"
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.amount_micros = eur_to_micros(req.budget_eur)
    budget.explicitly_shared = False
    operations.append(op)

    op = client.get_type("MutateOperation")
    campaign = op.campaign_operation.create
    campaign.resource_name = campaign_rn
    campaign.name = req.name
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SHOPPING
    campaign.campaign_budget = budget_rn
    campaign.shopping_setting.merchant_id = int(req.merchant_id)
    campaign.shopping_setting.feed_label = req.feed_label
    campaign.shopping_setting.campaign_priority = req.campaign_priority
    campaign.shopping_setting.enable_local = False
    campaign.target_spend.cpc_bid_ceiling_micros = ceiling  # Maximize Clicks with the CPC ceiling
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = False
    campaign.network_settings.target_content_network = False
    campaign.geo_target_type_setting.positive_geo_target_type = client.enums.PositiveGeoTargetTypeEnum.PRESENCE
    campaign.geo_target_type_setting.negative_geo_target_type = client.enums.NegativeGeoTargetTypeEnum.PRESENCE
    campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    operations.append(op)

    for geo_id in req.geo_target_constants:
        op = client.get_type("MutateOperation")
        criterion = op.campaign_criterion_operation.create
        criterion.campaign = campaign_rn
        criterion.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
        operations.append(op)
    for day in (client.enums.DayOfWeekEnum.MONDAY, client.enums.DayOfWeekEnum.TUESDAY, client.enums.DayOfWeekEnum.WEDNESDAY,
                client.enums.DayOfWeekEnum.THURSDAY, client.enums.DayOfWeekEnum.FRIDAY, client.enums.DayOfWeekEnum.SATURDAY,
                client.enums.DayOfWeekEnum.SUNDAY):
        for start, end in policy.schedule_windows():
            op = client.get_type("MutateOperation")
            criterion = op.campaign_criterion_operation.create
            criterion.campaign = campaign_rn
            criterion.ad_schedule.day_of_week = day
            criterion.ad_schedule.start_hour = start
            criterion.ad_schedule.start_minute = client.enums.MinuteOfHourEnum.ZERO
            criterion.ad_schedule.end_hour = end
            criterion.ad_schedule.end_minute = client.enums.MinuteOfHourEnum.ZERO
            operations.append(op)

    op = client.get_type("MutateOperation")
    ad_group = op.ad_group_operation.create
    ad_group.resource_name = ad_group_rn
    ad_group.name = req.ad_group_name
    ad_group.campaign = campaign_rn
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.type_ = client.enums.AdGroupTypeEnum.SHOPPING_PRODUCT_ADS
    ad_group.cpc_bid_micros = ceiling  # ignored under Maximize Clicks, kept equal to the ceiling so that it can never be lower
    operations.append(op)

    op = client.get_type("MutateOperation")
    ad = op.ad_group_ad_operation.create
    ad.ad_group = ad_group_rn
    ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad.ad.shopping_product_ad._pb.SetInParent()  # the product ad has no fields: the product card is built from the feed
    operations.append(op)
    return operations


def criterion_as_mutate_operation(client, criterion_operation):
    """The product tree nodes are AdGroupCriterionOperations; inside the one request they travel as MutateOperations."""
    op = client.get_type("MutateOperation")
    client.copy_from(op.ad_group_criterion_operation, criterion_operation)
    return op


def build_listing_tree_operations(client, customer_id: str, ad_group_id: str, req: CreateShoppingCampaignRequest,
                                  item_ids: Sequence[str] = ()) -> list:
    """
    The second request, with the real ad group ID.
    A selection (item_ids, the offer IDs as Google Ads writes them): a root subdivision by item ID with one advertised node per
    offer and an 'everything else' node that is excluded.
    The whole feed, no exclusions: one root node that includes every product.
    The whole feed with exclusions: a root subdivision by g:custom_label_0 with one excluded node per value and an
    'everything else' node that is advertised (the node without a value must always exist, Google refuses a subdivision
    without it).
    """
    ad_group_rn = _rn(customer_id, "adGroups", ad_group_id)
    ceiling = eur_to_micros(req.cpc_ceiling_eur)
    group_type = client.enums.ListingGroupTypeEnum
    index0 = client.enums.ProductCustomAttributeIndexEnum.INDEX0
    operations = []

    def new_node():
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.create
        criterion.ad_group = ad_group_rn
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        return op, criterion

    if not item_ids and not req.exclude_custom_label_0:
        op, root = new_node()
        root.listing_group.type_ = group_type.UNIT
        root.cpc_bid_micros = ceiling
        operations.append(op)
        return operations

    root_rn = _rn(customer_id, "adGroupCriteria", f"{ad_group_id}~{TEMP_TREE_ROOT}")
    if item_ids:
        op, root = new_node()
        root.resource_name = root_rn
        root.listing_group.type_ = group_type.SUBDIVISION
        operations.append(op)
        for item_id in item_ids:
            op, node = new_node()
            node.listing_group.type_ = group_type.UNIT
            node.listing_group.parent_ad_group_criterion = root_rn
            node.listing_group.case_value.product_item_id.value = item_id
            node.cpc_bid_micros = ceiling
            operations.append(op)
        op, rest = new_node()
        rest.listing_group.type_ = group_type.UNIT
        rest.listing_group.parent_ad_group_criterion = root_rn
        rest.listing_group.case_value.product_item_id._pb.SetInParent()  # an item ID dimension without a value: everything else
        rest.negative = True
        operations.append(op)
        return operations

    op, root = new_node()
    root.resource_name = root_rn
    root.listing_group.type_ = group_type.SUBDIVISION
    operations.append(op)
    for value in req.exclude_custom_label_0:
        op, node = new_node()
        node.listing_group.type_ = group_type.UNIT
        node.listing_group.parent_ad_group_criterion = root_rn
        node.listing_group.case_value.product_custom_attribute.index = index0
        node.listing_group.case_value.product_custom_attribute.value = value
        node.negative = True
        operations.append(op)
    op, rest = new_node()
    rest.listing_group.type_ = group_type.UNIT
    rest.listing_group.parent_ad_group_criterion = root_rn
    rest.listing_group.case_value.product_custom_attribute.index = index0  # no value: everything else
    rest.cpc_bid_micros = ceiling
    operations.append(op)
    return operations


# The field of the request a failed readiness check is about; every other check is about the Merchant Center ID
CHECK_FIELDS = {"GMC_FEED_LABEL": "feed_label", "GMC_CUSTOM_LABEL": "exclude_custom_label_0"}


def _selection_text(req: CreateShoppingCampaignRequest, item_ids: Sequence[str], source: str, notes: Sequence[str] = ()) -> str:
    if req.offers:
        text = f"only {len(item_ids)} offer(s) ({', '.join(item_ids)}), found in {source}; the rest of the feed is left out"
        return text + "".join(f"; {note}" for note in notes)
    if req.exclude_custom_label_0:
        return f"the whole feed except custom_label_0 = {', '.join(req.exclude_custom_label_0)}"
    return "the whole feed"


def _readiness_text(readiness) -> str:
    notes = [f"{check.code} {check.status}: {check.detail}" for check in readiness.checks if check.status in ("WARN", "SKIPPED")]
    return readiness.verdict + (f" ({' | '.join(notes)})" if notes else "")


def _schedule_text() -> str:
    windows = policy.schedule_windows()
    return ("windows " + ", ".join(f"{start:02d}:00-{end:02d}:00" for start, end in windows) + f" ({7 * len(windows)} criteria)") if windows else "none (all day)"


def _plan_text(req: CreateShoppingCampaignRequest, n_campaign: int, n_tree: int, products: str, readiness: str) -> str:
    return (f"'{req.name}': Standard Shopping, PAUSED, {req.budget_eur:.2f} EUR a day, Maximize Clicks with a ceiling of "
            f"{req.cpc_ceiling_eur:.2f} EUR, Merchant Center {req.merchant_id} / feed label {req.feed_label}, Google search only "
            f"(no partners, no display), geo {req.geo_target_constants} (presence), "
            f"schedule {_schedule_text()}, one ad group '{req.ad_group_name}' with one product ad; products: {products}. "
            f"Readiness: {readiness}. "
            f"One all-or-nothing request: {n_campaign + n_tree} operations ({n_campaign} for the campaign, {n_tree} for the product tree).")


def _mutate(client, ga_service, customer_id: str, operations: list, validate_only: bool = False):
    """GoogleAdsService.mutate; validate_only is not a flattened argument of the method, so the request is built by hand."""
    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(operations)
    request.validate_only = validate_only
    return ga_service.mutate(request=request)


def _ad_group_product_statuses(ga_service, customer_id: str, campaign_id: str, ad_group_id: str) -> Optional[Dict[str, int]]:
    """
    Status -> number of products Google Ads lists for the ad group; None when the report cannot be read. Asked for the ad group,
    not the campaign: the campaign scope lists the whole feed (528 seen live 31.10.2026), the product tree acts on the ad group.
    The report fills in with a delay of up to 24 h.
    """
    try:
        statuses: Dict[str, int] = {}
        for row in ga_service.search(customer_id=customer_id, query=f"""
            SELECT shopping_product.item_id, shopping_product.status FROM shopping_product
            WHERE shopping_product.campaign = 'customers/{customer_id}/campaigns/{campaign_id}'
              AND shopping_product.ad_group = 'customers/{customer_id}/adGroups/{ad_group_id}'"""):
            name = row.shopping_product.status.name
            statuses[name] = statuses.get(name, 0) + 1
        return statuses
    except GoogleAdsException:
        return None


@handle_google_ads_errors
def create_shopping_campaign(req: CreateShoppingCampaignRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Creates a PAUSED Standard Shopping campaign on the Merchant Center feed (see the module text) and reads it back.
    dry_run=True runs the checks and builds the operations, writes nothing.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")

    if list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT campaign.id FROM campaign
        WHERE campaign.name = '{req.name}' AND campaign.status != 'REMOVED' LIMIT 1
    """)):
        return _refusal("CAMPAIGN_NAME_EXISTS", f"A campaign named '{req.name}' already exists in this account; nothing was created.", req.name)

    readiness = assess_shopping_readiness(client, customer_id, CheckShoppingReadinessRequest(
        merchant_id=req.merchant_id, feed_label=req.feed_label, geo_target_constants=req.geo_target_constants,
        custom_label_0_values=req.exclude_custom_label_0))
    failed = next((check for check in readiness.checks if check.status == "FAIL"), None)
    if failed:
        code = "MERCHANT_CENTER_NOT_LINKED" if failed.code == "ADS_LINK" else failed.code
        return _refusal(code, f"{failed.detail} Nothing was created (check_shopping_readiness shows all checks).",
                        req.merchant_id, CHECK_FIELDS.get(failed.code, "merchant_id"))

    item_ids: Sequence[str] = ()
    source = ""
    notes: List[str] = []
    if req.offers:
        resolution = resolve_selection(ga_service, customer_id, req)
        if resolution.unreadable:
            return _refusal("CATALOG_NOT_READABLE", "Neither Google Ads nor Merchant Center could list the products, so the offers cannot be checked; "
                            "nothing was created (check_shopping_readiness shows which side is silent).", ", ".join(req.offers), "offers")
        if resolution.unknown:
            return _refusal("OFFER_NOT_FOUND", f"The catalog has no offer {', '.join(resolution.unknown)} (looked in {resolution.source}); "
                            "nothing was created. Offer IDs look like SKU-100-L and SKU-100-S.", ", ".join(resolution.unknown), "offers")
        if resolution.not_eligible:
            return _refusal("OFFER_NOT_ELIGIBLE", f"Merchant Center disapproved {', '.join(resolution.not_eligible)} for Shopping ads in the "
                            "campaign's countries: they cannot show; nothing was created.", ", ".join(resolution.not_eligible), "offers")
        item_ids, source = resolution.item_ids, resolution.source
        if resolution.pending:
            notes.append(f"waiting for the first review in Merchant Center, they show once approved: {', '.join(resolution.pending)}")
        if not resolution.approval_checked:
            notes.append("approval in Merchant Center could not be checked")

    operations = build_campaign_operations(client, customer_id, req)
    tree = build_listing_tree_operations(client, customer_id, str(TEMP_AD_GROUP), req, item_ids)
    n_campaign = len(operations)
    operations += [criterion_as_mutate_operation(client, node) for node in tree]
    plan = _plan_text(req, n_campaign, len(tree), _selection_text(req, item_ids, source, notes), _readiness_text(readiness))
    if req.dry_run:
        _mutate(client, ga_service, customer_id, operations, validate_only=True)
        return f"DRY RUN: {plan} Google checked the whole request without writing it (validate_only). Nothing was written to the account."

    response = _mutate(client, ga_service, customer_id, operations)
    campaign_id = ad_group_id = None
    for item in response.mutate_operation_responses:
        if item.campaign_result.resource_name:
            campaign_id = item.campaign_result.resource_name.split("/")[-1]
        if item.ad_group_result.resource_name:
            ad_group_id = item.ad_group_result.resource_name.split("/")[-1]
    if not campaign_id or not ad_group_id:
        return _refusal("NOT_APPLIED", "Google accepted request 1 but the campaign or ad group ID is missing in the answer; check get_campaigns.", req.name)

    back = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
               campaign.shopping_setting.merchant_id, campaign.shopping_setting.feed_label,
               campaign.target_spend.cpc_bid_ceiling_micros, campaign_budget.amount_micros
        FROM campaign WHERE campaign.id = {campaign_id} LIMIT 1
    """))
    tree_rows = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT ad_group_criterion.criterion_id, ad_group_criterion.negative, ad_group_criterion.listing_group.type
        FROM ad_group_criterion
        WHERE ad_group.id = {ad_group_id} AND ad_group_criterion.type = 'LISTING_GROUP' AND ad_group_criterion.status != 'REMOVED'
    """))
    if not back:
        return _refusal("NOT_APPLIED", f"Campaign {campaign_id} cannot be read back; check get_campaigns.", campaign_id)
    row = back[0]
    excluded = sum(1 for r in tree_rows if r.ad_group_criterion.negative)
    listed = _ad_group_product_statuses(ga_service, customer_id, campaign_id, ad_group_id)
    if listed is None:
        seen = "the products report could not be read"
    else:
        breakdown = ", ".join(f"{status} {count}" for status, count in sorted(listed.items())) or "nothing yet"
        seen = (f"Google Ads lists {sum(listed.values())} product(s) for the ad group: {breakdown} "
                f"(the report fills in within 24 hours; look at it again then, {len(item_ids) or 'all'} product(s) were selected)")
    return (f"Shopping campaign {campaign_id} '{row.campaign.name}' created: {row.campaign.status.name}, "
            f"{row.campaign.advertising_channel_type.name}, budget {row.campaign_budget.amount_micros / 1e6:.2f} EUR a day, "
            f"ceiling {row.campaign.target_spend.cpc_bid_ceiling_micros / 1e6:.2f} EUR, Merchant Center "
            f"{row.campaign.shopping_setting.merchant_id} / {row.campaign.shopping_setting.feed_label}, ad group {ad_group_id}, "
            f"products: {_selection_text(req, item_ids, source, notes)}, product tree {len(tree_rows)} node(s), {excluded} excluded; {seen}. "
            f"Readiness: {_readiness_text(readiness)}. Enable with toggle_campaign_status only after the owner's yes.")


def _placeholder_campaign_name() -> str:
    """
    A valid campaign name for the selection check, which needs a CreateShoppingCampaignRequest but creates no campaign. It starts
    with the policy's shopping.name_prefix (the request model insists on it when one is set), so the advertiser's name is
    never written in the code: a public copy of this file carries no private word.
    """
    return f"{policy.text('shopping.name_prefix')} selection check".strip()


@handle_google_ads_errors
def set_shopping_product_selection(req: SetShoppingProductSelectionRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Updates the product partition tree in an existing Shopping ad group to advertise exactly the specified offers.
    All other products in the feed are excluded under the negative 'everything else' node.
    dry_run=True sends the request with validate_only to Google Ads API.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")

    ad_group_rows = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT ad_group.id, ad_group.name, ad_group.campaign,
               campaign.id, campaign.name, campaign.target_spend.cpc_bid_ceiling_micros,
               campaign.shopping_setting.merchant_id, campaign.shopping_setting.feed_label
        FROM ad_group
        WHERE ad_group.id = {req.ad_group_id} AND ad_group.status != 'REMOVED' LIMIT 1
    """))
    if not ad_group_rows:
        return _refusal("AD_GROUP_NOT_FOUND", f"Ad group {req.ad_group_id} was not found or is removed.", req.ad_group_id, "ad_group_id")

    ag = ad_group_rows[0].ad_group
    camp = ad_group_rows[0].campaign
    merchant_id = str(camp.shopping_setting.merchant_id)
    feed_label = camp.shopping_setting.feed_label
    campaign_ceiling = camp.target_spend.cpc_bid_ceiling_micros or eur_to_micros(0.50)
    ceiling = eur_to_micros(req.cpc_ceiling_eur) if req.cpc_ceiling_eur else campaign_ceiling
    ad_group_rn = _rn(customer_id, "adGroups", req.ad_group_id)

    dummy_req = CreateShoppingCampaignRequest(
        name=_placeholder_campaign_name(),
        merchant_id=merchant_id,
        feed_label=feed_label,
        offers=req.offers
    )
    resolution = resolve_selection(ga_service, customer_id, dummy_req)
    if resolution.unreadable:
        return _refusal("CATALOG_NOT_READABLE", "Neither Google Ads nor Merchant Center could list the products, so the offers cannot be checked.", ", ".join(req.offers), "offers")
    if resolution.unknown:
        return _refusal("OFFER_NOT_FOUND", f"The catalog has no offer {', '.join(resolution.unknown)} (looked in {resolution.source}).", ", ".join(resolution.unknown), "offers")
    if resolution.not_eligible:
        return _refusal("OFFER_NOT_ELIGIBLE", f"Merchant Center disapproved {', '.join(resolution.not_eligible)} for Shopping ads.", ", ".join(resolution.not_eligible), "offers")

    target_item_ids = resolution.item_ids

    tree_rows = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT 
            ad_group_criterion.resource_name,
            ad_group_criterion.criterion_id,
            ad_group_criterion.listing_group.type,
            ad_group_criterion.listing_group.parent_ad_group_criterion,
            ad_group_criterion.listing_group.case_value.product_item_id.value,
            ad_group_criterion.negative
        FROM ad_group_criterion
        WHERE ad_group.id = {req.ad_group_id} AND ad_group_criterion.type = 'LISTING_GROUP' AND ad_group_criterion.status != 'REMOVED'
    """))

    operations = []
    subdivision_row = None
    negative_rest_row = None
    existing_units: Dict[str, Any] = {}

    for r in tree_rows:
        c = r.ad_group_criterion
        if c.listing_group.type_.name == "SUBDIVISION":
            subdivision_row = r
        elif c.negative:
            negative_rest_row = r
        elif c.listing_group.type_.name == "UNIT" and c.listing_group.case_value.product_item_id.value:
            existing_units[c.listing_group.case_value.product_item_id.value.lower()] = r

    target_keys = {item.lower() for item in target_item_ids}
    removed_count = 0
    created_count = 0

    if subdivision_row:
        root_rn = subdivision_row.ad_group_criterion.resource_name
        for key, row in existing_units.items():
            if key not in target_keys:
                op = client.get_type("MutateOperation")
                op.ad_group_criterion_operation.remove = row.ad_group_criterion.resource_name
                operations.append(op)
                removed_count += 1

        for item_id in target_item_ids:
            if item_id.lower() not in existing_units:
                op = client.get_type("MutateOperation")
                node = op.ad_group_criterion_operation.create
                node.ad_group = ad_group_rn
                node.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
                node.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
                node.listing_group.parent_ad_group_criterion = root_rn
                node.listing_group.case_value.product_item_id.value = item_id
                node.cpc_bid_micros = ceiling
                operations.append(op)
                created_count += 1

        if not negative_rest_row:
            op = client.get_type("MutateOperation")
            rest = op.ad_group_criterion_operation.create
            rest.ad_group = ad_group_rn
            rest.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            rest.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
            rest.listing_group.parent_ad_group_criterion = root_rn
            rest.listing_group.case_value.product_item_id._pb.SetInParent()
            rest.negative = True
            operations.append(op)
    else:
        for r in tree_rows:
            op = client.get_type("MutateOperation")
            op.ad_group_criterion_operation.remove = r.ad_group_criterion.resource_name
            operations.append(op)

        root_rn = _rn(customer_id, "adGroupCriteria", f"{req.ad_group_id}~{TEMP_TREE_ROOT}")
        op = client.get_type("MutateOperation")
        root = op.ad_group_criterion_operation.create
        root.resource_name = root_rn
        root.ad_group = ad_group_rn
        root.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        root.listing_group.type_ = client.enums.ListingGroupTypeEnum.SUBDIVISION
        operations.append(op)

        for item_id in target_item_ids:
            op = client.get_type("MutateOperation")
            node = op.ad_group_criterion_operation.create
            node.ad_group = ad_group_rn
            node.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            node.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
            node.listing_group.parent_ad_group_criterion = root_rn
            node.listing_group.case_value.product_item_id.value = item_id
            node.cpc_bid_micros = ceiling
            operations.append(op)
            created_count += 1

        op = client.get_type("MutateOperation")
        rest = op.ad_group_criterion_operation.create
        rest.ad_group = ad_group_rn
        rest.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        rest.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
        rest.listing_group.parent_ad_group_criterion = root_rn
        rest.listing_group.case_value.product_item_id._pb.SetInParent()
        rest.negative = True
        operations.append(op)

    if not operations:
        return f"Ad group {req.ad_group_id} already has the exact selection of {len(target_item_ids)} offers. No changes needed."

    kept_count = len(target_item_ids) - created_count
    plan = (f"Ad group {req.ad_group_id} ('{ag.name}'): update product selection to {len(target_item_ids)} offers "
            f"({kept_count} kept, {created_count} added, {removed_count} removed; everything else excluded).")

    if req.dry_run:
        _mutate(client, ga_service, customer_id, operations, validate_only=True)
        return f"DRY RUN: {plan} Google verified the operations via validate_only. Nothing was written."

    response = _mutate(client, ga_service, customer_id, operations)
    return (f"Product selection updated for ad group {req.ad_group_id} ('{ag.name}') in campaign '{camp.name}': "
            f"{len(target_item_ids)} offers active ({kept_count} kept, {created_count} added, {removed_count} removed), "
            f"all other feed products excluded; ceiling {ceiling / 1e6:.2f} EUR.")

