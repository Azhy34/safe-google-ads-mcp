import os
os.environ["MCP_SERVER_PROCESS"] = "1"
import sys
import io
import json
import functools
from pathlib import Path
from typing import List, Literal, Optional
from mcp.server.fastmcp import FastMCP

# Force UTF-8 encoding for Windows terminal
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Add current folder to sys.path
CURRENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CURRENT_DIR))

from core import policy
from core.config import CUSTOMER_ID, MERCHANT_CENTER_ID
from core.skills_provider import register_skills_over_mcp
from shield.error_shield import build_request
from shield.telemetry import current_tool
from shield import cloud_sink
from shield.models_campaign import (
    CampaignStatusUpdateRequest, CampaignBudgetUpdateRequest, CampaignCeilingStepRequest,
    CampaignDatesUpdateRequest, IdArguments, CreateSearchCampaignRequest
)
from shield.models_adgroup import (
    AdGroupBidUpdateRequest, KeywordStatusUpdateRequest, AdGroupStatusUpdateRequest, CreateAdGroupRequest, AddKeywordsRequest, AdGroupAudienceTargetingRequest
)
from shield.models_ad import DeployGoldenRSARequest, AdStatusUpdateRequest
from shield.models_asset import (
    LinkAssetRequest, UnlinkAssetRequest, CreatePriceAssetRequest, SyncPriceExtensionRequest, CreateSitelinkAssetsRequest,
    CreateCalloutAssetsRequest, CreateStructuredSnippetAssetRequest, CreatePromotionAssetRequest, AssetAssociationsRequest,
)
from shield.models_criterion import (
    ApplyNegativeShieldRequest, RemoveNegativeKeywordRequest, SyncSharedNegativeListRequest
)

from tools.campaign_tools import (
    list_campaigns, get_campaign_detail, 
    update_campaign_status, update_campaign_budget, update_campaign_dates, step_campaign_cpc_ceiling,
    get_campaign_targeting_overview as targeting_func,
    create_search_campaign as create_campaign_func
)
from tools.adgroup_tools import (
    list_ad_groups, update_ad_group_bid, update_keyword_status, update_ad_group_status,
    get_ad_group_keywords as adgroup_keywords_func,
    create_ad_group as create_adgroup_func,
    add_ad_group_keywords as add_keywords_func,
    set_ad_group_audience_targeting as audience_targeting_func
)

from tools.rsa_tools import list_campaign_ads, deploy_golden_rsa as rsa_deploy_func, update_ad_status as update_ad_status_func
from tools.asset_tools import (
    list_account_assets, 
    get_assets_associations as assocs_func, 
    get_assets_performance as perf_func, 
    get_assets_diversity as diversity_func,
    link_asset_to_entity as link_func, 
    unlink_asset_from_entity as unlink_func,
    create_price_asset as create_price_func,
    sync_price_extension as sync_price_func,
    preview_sitelink_assets as preview_sitelinks_func,
    create_sitelink_assets as create_sitelinks_func,
    create_callout_assets as create_callouts_func,
    create_structured_snippet_asset as create_snippet_func,
    create_promotion_asset as create_promotion_func,
    get_campaign_assets_overview as assets_overview_func
)
from tools.negative_tools import (
    apply_negative_shield as neg_shield_func,
    add_negative_keywords as add_neg_func,
    remove_negative_keyword as remove_neg_func,
    sync_shared_negative_list as sync_shared_neg_func,
    mine_search_terms,
    audit_campaign_4level_shield as audit_func,
    get_daily_performance as daily_perf_func,
    get_impression_share_loss as imp_share_func,
    auto_fix_collisions_and_dupes as fix_collisions_func,
    fetch_google_ai_recommendations as ai_recs_func,
    search_keyword_ideas as keyword_ideas_func
)
from tools.history_tools import get_change_history as change_history_func, verify_changes_logged as verify_logged_func
from tools.observability_tools import (
    get_cloud_logs as cloud_logs_func,
    get_cloud_trace as cloud_trace_func,
    analyze_tool_health as tool_health_func
)
from tools.eval_tools import evaluate_agent_process as process_eval_func
from tools.preflight_tools import preflight_log_check as preflight_func
from tools.conversion_tools import (
    check_enhanced_conversions_status as check_conversions_func,
    upload_click_conversions as upload_conversions_func,
    set_conversion_action_goal as set_conversion_goal_func
)
from shield.models_conversion import ClickConversionItem, SetConversionActionGoalRequest
from tools.currency_tools import get_demand_gen_currency_floor as currency_floor_func
from tools.geo_tools import audit_geo_physical_locations as geo_audit_func
from tools.billing_tools import get_google_ads_invoices as invoices_func
from tools.merchant_tools import check_merchant_center_link as mc_link_func
from shield.models_image import CreateImageAssetRequest
from tools.image_tools import create_image_asset as create_image_func
from shield.models_shopping import CreateShoppingCampaignRequest, CheckShoppingReadinessRequest, SetShoppingProductSelectionRequest
from tools.shopping_tools import create_shopping_campaign as create_shopping_func, set_shopping_product_selection as set_shopping_product_selection_func
from tools.shopping_readiness_tools import check_shopping_readiness as shopping_readiness_func
from shield.models_location import CampaignLocationRequest
from tools.location_tools import update_campaign_location as update_location_func

# Initialize FastMCP Server
mcp = FastMCP("Google Ads")

def mcp_tool():
    """
    @mcp.tool() that also tells the journal which MCP tool the agent called (shield.telemetry.current_tool): the entry
    gets `tool` next to `function`, and the span is named 'tools/call <tool>' as on Google's MCP servers.
    functools.wraps keeps the signature and docstring FastMCP builds the tool schema from.
    """
    def decorate(fn):
        @functools.wraps(fn)
        def call(*args, **kwargs):
            token = current_tool.set(fn.__name__)
            try:
                return fn(*args, **kwargs)
            finally:
                current_tool.reset(token)
        return mcp.tool()(call)
    return decorate

def _bad_ids(function: str, action_type: str = "READ", **ids) -> Optional[str]:
    """Refuses a non-numeric campaign / ad group ID before it reaches a GAQL query; logged as REQUEST_REJECTED."""
    _, rejected = build_request(function, IdArguments, action_type=action_type, **{k: v for k, v in ids.items() if v is not None})
    return rejected

# --- STEP 0: MANDATORY LOG CHECK (every tool below refuses until it has passed) ---

# [read] MANDATORY first step of a session: log check (tool errors and anomalies, changes made outside the MCP, breaks of the working order). Until it passes, every ad tool answers PREFLIGHT_REQUIRED; on findings the agent shows them to the owner and, after their go-ahead, calls it again with acknowledge=True.
@mcp_tool()
def preflight_log_check(acknowledge: bool = False, days: Optional[int] = None) -> str:
    """
    MANDATORY before any analysis or change of the ad account, once per session: until it has passed, every other
    Google Ads tool answers PREFLIGHT_REQUIRED (only the journal tools stay open). It runs three checks over the time
    since the last accepted check (default: the last 7 days; `days` 1-30 sets the span by hand):
    TOOL_HEALTH (errors and anomalies of this MCP), OUTSIDE_CHANGES (account changes no MCP call made: scripts, other
    agents, the interface), AGENT_PROCESS (sessions that broke the working order). Verdict OK opens the ad tools.
    Verdict ATTENTION keeps them shut: show the findings to the owner, do not start the analysis, wait for their word
    to continue, and only then call preflight_log_check(acknowledge=True), which opens the tools and moves the
    checkpoint so accepted findings are not reported again. Read-only: it changes nothing in the ad account.
    """
    res = preflight_func(acknowledge, days)
    return res.model_dump_json(indent=2)

# --- LEVEL 1: CAMPAIGN TOOLS ---

# [read] List campaigns (active and paused): daily budget, bidding strategy, CPC ceiling. Journal name: list_campaigns.
@mcp_tool()
def get_campaigns() -> str:
    """
    List all active and paused Google Ads campaigns with daily budgets, bidding strategies
    and the Maximize Clicks CPC ceiling (cpc_ceiling_eur).
    Returns typed JSON payload protected by Two-Layer Pydantic Error Shield.
    """
    res = list_campaigns()
    return res.model_dump_json(indent=2)

# [read] Details of one campaign by ID: budget, delivery method, strategy, CPC ceiling. Journal name: get_campaign_detail.
@mcp_tool()
def get_campaign_details(campaign_id: str) -> str:
    """
    Get detailed budget, delivery method, bidding strategy and Maximize Clicks CPC ceiling for a single campaign by ID.
    """
    rejected = _bad_ids("get_campaign_detail", campaign_id=campaign_id)
    if rejected:
        return rejected
    res = get_campaign_detail(campaign_id)
    return res.model_dump_json(indent=2)

# [write] Enable or pause a campaign by ID. Journal name: update_campaign_status.
@mcp_tool()
def toggle_campaign_status(campaign_id: str, enabled: bool) -> str:
    """
    Enable or pause a campaign by ID. Uses official sparse update_mask.
    """
    req, rejected = build_request("update_campaign_status", CampaignStatusUpdateRequest, campaign_id=campaign_id, enabled=enabled)
    if rejected:
        return rejected
    res = update_campaign_status(req)
    return res.model_dump_json(indent=2)

# [write] Change a campaign's daily budget in EUR (1-100 allowed). Journal name: update_campaign_budget.
@mcp_tool()
def set_campaign_daily_budget(campaign_id: str, budget_eur: float) -> str:
    """
    Updates campaign daily budget in EUR with bounds validation (1.00 - 100.00 EUR).
    """
    req, rejected = build_request("update_campaign_budget", CampaignBudgetUpdateRequest, campaign_id=campaign_id, budget_eur=budget_eur)
    if rejected:
        return rejected
    res = update_campaign_budget(req)
    return res.model_dump_json(indent=2)

# [read] Targeting audit: geo, languages, ad schedule, device adjustments, networks (Search/Display).
@mcp_tool()
def get_campaign_targeting_overview(campaign_id: str) -> str:
    """
    Audits campaign targeting criteria (Geo Locations, Languages, Dayparting Ad Schedules, Device Modifiers)
    and network settings (Google Search vs Display Network).
    """
    rejected = _bad_ids("get_campaign_targeting_overview", campaign_id=campaign_id)
    if rejected:
        return rejected
    res = targeting_func(campaign_id)
    return res.model_dump_json(indent=2)

# [write] Create a new Search campaign with targeting, an ad group and keywords. Journal name: create_search_campaign.
@mcp_tool()
def create_search_campaign(
    name: str,
    ad_group_name: str,
    budget_eur: float = 3.0,
    cpc_ceiling_eur: float = 0.55,
    status: Literal["PAUSED", "ENABLED"] = "PAUSED",
    geo_target_constants: Optional[list[int]] = None,
    language_constants: Optional[list[int]] = None,
    keywords: list[str] = []
) -> str:
    """
    Creates a new Google Ads Search campaign with dedicated budget, Maximize Clicks (TARGET_SPEND)
    strategy with CPC ceiling, Clean Search network settings (Partners/Display OFF), geo/language criteria,
    dayparting schedule (ads.schedule_windows of the policy), an initial ad group and clean phrase/exact keywords.
    geo_target_constants and language_constants default to ads.default_geo and ads.default_language of the policy.
    """
    req, rejected = build_request(
        "create_search_campaign",
        CreateSearchCampaignRequest,
        name=name,
        ad_group_name=ad_group_name,
        budget_eur=budget_eur,
        cpc_ceiling_eur=cpc_ceiling_eur,
        status=status,
        geo_target_constants=geo_target_constants if geo_target_constants is not None else list(policy.get("ads.default_geo", [])),
        language_constants=language_constants if language_constants is not None else list(policy.get("ads.default_language", [])),
        keywords=keywords
    )
    if rejected:
        return rejected
    res = create_campaign_func(req)
    return res.model_dump_json(indent=2)

# [write] Switch one country on or off inside an existing campaign; the last country cannot be removed. Journal name: update_campaign_location.
@mcp_tool()
def set_campaign_location(campaign_id: str, geo_target_id: int, enabled: bool) -> str:
    """
    Targets (enabled=True) or stops targeting (enabled=False) one country inside an existing campaign:
    geo_target_id 2276 = Germany, 2040 = Austria. Switching off removes the country criterion (switching on adds it back);
    ads, keywords and statistics stay. Refuses to remove the last targeted country. Check with get_campaign_targeting_overview.
    """
    req, rejected = build_request("update_campaign_location", CampaignLocationRequest,
                                  campaign_id=campaign_id, geo_target_id=geo_target_id, enabled=enabled)
    if rejected:
        return rejected
    res = update_location_func(req)
    return res.model_dump_json(indent=2)

# [write] Set or clear campaign start/end dates (end_date in YYYY-MM-DD stops serving at 23:59:59 account time; dry_run=True previews). Journal name: update_campaign_dates.
@mcp_tool()
def set_campaign_dates(
    campaign_id: str,
    end_date: Optional[str] = None,
    start_date: Optional[str] = None,
    clear_end_date: bool = False,
    dry_run: bool = False
) -> str:
    """
    Sets campaign start and/or end dates in Google Ads using an explicit update_mask and verifies via read-back.
    - end_date: last serving day in ISO YYYY-MM-DD (stored as 'YYYY-MM-DD 23:59:59' in account timezone; max 2037-12-30).
    - start_date: optional start day in ISO YYYY-MM-DD (stored as 'YYYY-MM-DD 00:00:00'; only editable before campaign starts).
    - clear_end_date: True to remove the end date (sets Google sentinel 2037-12-30 23:59:59). Mutually exclusive with end_date.
    - dry_run: True validates and previews current vs new dates without writing to Google Ads.
    """
    req, rejected = build_request(
        "update_campaign_dates",
        CampaignDatesUpdateRequest,
        campaign_id=campaign_id,
        end_date=end_date,
        start_date=start_date,
        clear_end_date=clear_end_date,
        dry_run=dry_run,
    )
    if rejected:
        return rejected
    res = update_campaign_dates(req)
    return res.model_dump_json(indent=2)

# [write] Create a Shopping campaign on the Merchant Center feed: always paused, Maximize Clicks with a ceiling, countries from the policy (shopping.default_geo), schedule from the policy (ads.schedule_windows), one ad group and a product ad. Products: only the chosen ones (offers) or the whole feed with an explicit all_products. check_shopping_readiness and a check of the offer IDs against the catalog run before anything is written.
@mcp_tool()
def create_shopping_campaign(
    name: str,
    budget_eur: float = 5.0,
    cpc_ceiling_eur: float = 0.50,
    offers: Optional[List[str]] = None,
    all_products: bool = False,
    exclude_custom_label_0: Optional[List[str]] = None,
    dry_run: bool = False,
    ad_group_name: Optional[str] = None,
    merchant_id: Optional[str] = None,
    feed_label: Optional[str] = None
) -> str:
    """
    Creates a Standard Shopping campaign that advertises the products of the Merchant Center feed (product cards: photo, price,
    title from the feed; no keywords, no texts). Always created PAUSED (enable with toggle_campaign_status after the owner's yes).
    Maximize Clicks with the CPC ceiling, Google search only, geo from shopping.default_geo of the policy (presence),
    schedule ads.schedule_windows of the policy, one ad group with one product ad. The name must start with shopping.name_prefix when the policy sets one.
    merchant_id, feed_label and ad_group_name default to the policy (account.merchant_center_id, account.feed_label,
    shopping.default_ad_group_name).
    Which products is always a choice: offers = the offer_id of the products to advertise (e.g. ['SKU-100-L', 'SKU-100-S']);
    only they are advertised, each must exist in the catalog. all_products=True advertises the whole feed instead (never the default);
    only then exclude_custom_label_0 leaves out products by g:custom_label_0 (e.g. ['clearance']).
    Before anything is written check_shopping_readiness's judgement must not be BLOCKED (link, Shopping ads program, feed label,
    approved products). dry_run=True runs all checks and builds the operations, and writes nothing.
    """
    req, rejected = build_request(
        "create_shopping_campaign", CreateShoppingCampaignRequest,
        name=name, budget_eur=budget_eur, cpc_ceiling_eur=cpc_ceiling_eur,
        offers=offers or [], all_products=all_products,
        exclude_custom_label_0=exclude_custom_label_0 or [], dry_run=dry_run,
        **{key: value for key, value in (("ad_group_name", ad_group_name), ("merchant_id", merchant_id), ("feed_label", feed_label)) if value is not None}
    )
    if rejected:
        return rejected
    res = create_shopping_func(req)
    return res.model_dump_json(indent=2)

@mcp_tool()
def set_shopping_product_selection(
    ad_group_id: str,
    offers: List[str],
    cpc_ceiling_eur: Optional[float] = None,
    dry_run: bool = False
) -> str:
    """
    Updates the product partition tree in an existing Shopping ad group to advertise exactly the specified offers (offer IDs
    like SKU-100-S, SKU-100-L). All other feed products are excluded under the negative 'everything else' node.
    Validates offer IDs against Merchant Center catalog before mutating.
    dry_run=True checks and validates the mutation via Google Ads validate_only without writing to the account.
    """
    req, rejected = build_request(
        "set_shopping_product_selection", SetShoppingProductSelectionRequest,
        ad_group_id=ad_group_id, offers=offers,
        cpc_ceiling_eur=cpc_ceiling_eur, dry_run=dry_run
    )
    if rejected:
        return rejected
    res = set_shopping_product_selection_func(req)
    return res.model_dump_json(indent=2)

# --- LEVEL 2: ADGROUP & BIDDING TOOLS ---

# [write] Create a new ad group in an existing campaign (optionally with keywords).
@mcp_tool()
def create_ad_group(
    campaign_id: str,
    name: str,
    status: Literal["ENABLED", "PAUSED"] = "ENABLED",
    keywords: list[str] = []
) -> str:
    """
    Creates a new Search standard Ad Group in an existing campaign, optionally with initial positive keywords.
    """
    req, rejected = build_request(
        "create_ad_group",
        CreateAdGroupRequest,
        campaign_id=campaign_id,
        name=name,
        status=status,
        keywords=keywords
    )
    if rejected:
        return rejected
    res = create_adgroup_func(req)
    return res.model_dump_json(indent=2)

# [write] Add positive keywords (Exact [kw] or Phrase "kw") to an ad group.
@mcp_tool()
def add_ad_group_keywords(
    ad_group_id: str,
    keywords: list[str]
) -> str:
    """
    Adds positive keywords (Exact [kw] or Phrase "kw") to an existing Ad Group.
    """
    req, rejected = build_request(
        "add_ad_group_keywords",
        AddKeywordsRequest,
        ad_group_id=ad_group_id,
        keywords=keywords
    )
    if rejected:
        return rejected
    res = add_keywords_func(req)
    return res.model_dump_json(indent=2)

# [read] Ad groups with base CPC bids and serving status. Journal name: list_ad_groups.
@mcp_tool()
def get_ad_groups(campaign_id: Optional[str] = None) -> str:
    """
    Lists ad groups with their base CPC bids in EUR and serving status.
    """
    rejected = _bad_ids("list_ad_groups", campaign_id=campaign_id)
    if rejected:
        return rejected
    res = list_ad_groups(campaign_id)
    return res.model_dump_json(indent=2)


# [write] Step a campaign's CPC ceiling by +-0.05 EUR (bounds 0.10-3.00). The main bid lever: all campaigns use Maximize Clicks. Journal name: step_campaign_cpc_ceiling.
@mcp_tool()
def step_bid_threshold(campaign_id: str, direction: Literal["+0.05", "-0.05"] = "+0.05") -> str:
    """
    Steps the campaign's Maximize Clicks CPC ceiling (target_spend.cpc_bid_ceiling_micros)
    by +0.05 or -0.05 EUR (bounds 0.10-3.00 EUR). This is the bid lever for campaigns that
    use TARGET_SPEND; ad group bids are ignored under that strategy.
    """
    req, rejected = build_request("step_campaign_cpc_ceiling", CampaignCeilingStepRequest, campaign_id=campaign_id, direction=direction)
    if rejected:
        return rejected
    res = step_campaign_cpc_ceiling(req)
    return res.model_dump_json(indent=2)

# [write] Ad group CPC bid; works only with MANUAL_CPC, otherwise refused. Journal name: update_ad_group_bid.
@mcp_tool()
def set_ad_group_cpc_bid(ad_group_id: str, new_bid_eur: float) -> str:
    """
    Sets the base CPC bid of an Ad Group. Works only in MANUAL_CPC campaigns and refuses otherwise,
    because Maximize Clicks (TARGET_SPEND) ignores ad group bids without error.
    """
    req, rejected = build_request("update_ad_group_bid", AdGroupBidUpdateRequest, ad_group_id=ad_group_id, new_bid_eur=new_bid_eur)
    if rejected:
        return rejected
    res = update_ad_group_bid(req)
    return res.model_dump_json(indent=2)

# [read] Keywords of a group: Quality Score, match type, predicted CTR, relevance, individual bids.
@mcp_tool()
def get_ad_group_keywords(ad_group_id: str) -> str:
    """
    Lists all biddable keywords in an Ad Group with their Google Quality Scores (1-10),
    match types (EXACT, PHRASE, BROAD), predicted CTR, ad relevance, and override bids.
    """
    rejected = _bad_ids("get_ad_group_keywords", ad_group_id=ad_group_id)
    if rejected:
        return rejected
    res = adgroup_keywords_func(ad_group_id)
    return res.model_dump_json(indent=2)

# [write] Pause/enable one positive keyword by criterion_id (negative keywords are not touched). Journal name: update_keyword_status.
@mcp_tool()
def set_keyword_status(ad_group_id: str, criterion_id: str, enabled: bool) -> str:
    """
    Pauses (enabled=False) or enables one positive keyword in an ad group.
    criterion_id comes from get_ad_group_keywords. Refuses negative keywords and non-keyword criteria.
    """
    req, rejected = build_request("update_keyword_status", KeywordStatusUpdateRequest, ad_group_id=ad_group_id, criterion_id=criterion_id, enabled=enabled)
    if rejected:
        return rejected
    res = update_keyword_status(req)
    return res.model_dump_json(indent=2)

# [write] Pause/enable a whole ad group by ID (not the last enabled one of a campaign; enabling only with a keyword and an RSA). Journal name: update_ad_group_status.
@mcp_tool()
def set_ad_group_status(ad_group_id: str, enabled: bool) -> str:
    """
    Pauses (enabled=False) or enables (enabled=True) one ad group; ad_group_id comes from get_ad_groups.
    Refuses to pause the last ENABLED ad group of a campaign, and refuses to enable a group that has no ENABLED positive
    keyword or no ENABLED Responsive Search Ad. Warns when the group has no images of its own (AD_IMAGE at ad-group level).
    Answers with the status Google holds after the change.
    """
    req, rejected = build_request("update_ad_group_status", AdGroupStatusUpdateRequest, ad_group_id=ad_group_id, enabled=enabled)
    if rejected:
        return rejected
    res = update_ad_group_status(req)
    return res.model_dump_json(indent=2)


@mcp_tool()
def set_ad_group_audience_targeting(
    ad_group_id: str,
    user_list_ids: List[str],
    targeting_mode: Literal["TARGETING", "OBSERVATION"] = "TARGETING",
    bid_modifier: Optional[float] = None
) -> str:
    """
    Configures audience targeting for an ad group (Search or Shopping).
    Supports:
    - TARGETING mode (narrow reach strictly to audience segments only - pure retargeting).
    - OBSERVATION mode (do not narrow reach, bid adjustments only).
    user_list_ids: list of audience UserList IDs (e.g. from user_list entity).
    bid_modifier: optional bid multiplier in OBSERVATION mode (e.g. 1.5 for +50%, 1.8 for +80%).
    """
    req, rejected = build_request(
        "set_ad_group_audience_targeting",
        AdGroupAudienceTargetingRequest,
        ad_group_id=ad_group_id,
        user_list_ids=user_list_ids,
        targeting_mode=targeting_mode,
        bid_modifier=bid_modifier
    )
    if rejected:
        return rejected
    res = audience_targeting_func(req)
    return res.model_dump_json(indent=2)


# --- LEVEL 3: RSA ADS TOOLS ---

# [read] RSA ads of a campaign with all 15 headlines and 4 descriptions. Journal name: list_campaign_ads.
@mcp_tool()
def get_campaign_ads(campaign_id: str) -> str:
    """
    Lists all active/paused Responsive Search Ads with all 15 headlines and 4 descriptions.
    """
    rejected = _bad_ids("list_campaign_ads", campaign_id=campaign_id)
    if rejected:
        return rejected
    res = list_campaign_ads(campaign_id)
    return res.model_dump_json(indent=2)

# [write] Create a "golden" RSA: 15 headlines (<=30) + 4 descriptions (<=90), H1 pinned with DKI; the URL rules of the business policy (ads.url_rules, e.g. a required coupon parameter on one host and none on another) are enforced by the shield; with 3 active RSAs the oldest is paused; with ad_id the ad is edited in place.
@mcp_tool()
def deploy_golden_rsa(
    ad_group_id: str,
    final_url: str,
    headlines: List[str],
    descriptions: List[str],
    pinned_h1_dki: str,
    path1: Optional[str] = None,
    path2: Optional[str] = None,
    ad_id: Optional[str] = None
) -> str:
    """
    Deploys a Golden RSA with strict 15/4 validation:
    - Exactly 15 headlines (<=30 chars); headlines[0] must equal pinned_h1_dki
    - Exactly 4 descriptions (<=90 chars)
    - headlines[0] is pinned to Headline 1 and must use Dynamic Keyword Insertion ({KeyWord:...})
    - URL rules of the business policy (ads.url_rules) apply to final_url and to the texts: a rule can require a query
      parameter (for example a coupon) for one host and forbid it, in the URL and in every headline and description, for
      another (the shield rejects a breach with REQUEST_REJECTED, before any call to Google).
    - path1 and path2 (display URL paths, max 15 chars) default to ads.display_paths of the policy.
    - If `ad_id` is passed (or if the ad group already has an ENABLED RSA), updates the existing RSA in-place via AdService.mutate_ads (preserving ad_id and avoiding duplicates).
    """
    req, rejected = build_request(
        "deploy_golden_rsa", DeployGoldenRSARequest,
        ad_group_id=ad_group_id,
        final_url=final_url,
        headlines=headlines,
        descriptions=descriptions,
        pinned_h1_dki=pinned_h1_dki,
        path1=path1 if path1 is not None else policy.display_paths()[0],
        path2=path2 if path2 is not None else policy.display_paths()[1],
        ad_id=ad_id
    )
    if rejected:
        return rejected
    res = rsa_deploy_func(req)
    return res.model_dump_json(indent=2)

# [write] Pause/enable one RSA ad by ad_id (guard: the only active RSA of a group cannot be stopped). Journal name: update_ad_status.
@mcp_tool()
def set_ad_status(ad_group_id: str, ad_id: str, enabled: bool) -> str:
    """
    Pauses (enabled=False) or enables (enabled=True) one Responsive Search Ad (RSA) in an ad group.
    ad_id comes from get_campaign_ads. Refuses to pause the last enabled RSA in an ad group.
    """
    req, rejected = build_request(
        "update_ad_status", AdStatusUpdateRequest,
        ad_group_id=ad_group_id,
        ad_id=ad_id,
        enabled=enabled
    )
    if rejected:
        return rejected
    res = update_ad_status_func(req)
    return res.model_dump_json(indent=2)

# --- LEVEL 4: ASSETS (OBJECTS) TOOLS (Matching Google Ads UI Tabs) ---

# [read] "Associations" tab: which assets are linked to the account/campaign/group (filter by level, type, campaign, group; image size in the record).
@mcp_tool()
def get_assets_associations(level: Optional[str] = None, field_type: Optional[str] = None,
                            campaign_id: Optional[str] = None, ad_group_id: Optional[str] = None) -> str:
    """
    Google Ads UI Tab 1: 'Associations'.
    Retrieves linked assets across Account (CUSTOMER), CAMPAIGN, and AD_GROUP levels.
    Filters: level ('CUSTOMER', 'CAMPAIGN', 'AD_GROUP'); field_type ('AD_IMAGE', 'SITELINK', 'CALLOUT', 'PRICE', 'STRUCTURED_SNIPPET',
    'PROMOTION', 'BUSINESS_LOGO', 'BUSINESS_NAME', etc.); campaign_id = that campaign and its ad groups; ad_group_id = that ad group only.
    Without campaign_id / ad_group_id the answer covers the whole account (150 rows, ~70 000 characters on 31.10.2026): name one.
    Returns text previews, image CDN URLs, image pixel size (asset_size, e.g. '1200x628 (1.91:1)'), and binding status.
    """
    req, rejected = build_request("get_assets_associations", AssetAssociationsRequest, action_type="READ",
                                  level=level, field_type=field_type, campaign_id=campaign_id, ad_group_id=ad_group_id)
    if rejected:
        return rejected
    res = assocs_func(level=req.level, field_type=req.field_type, campaign_id=req.campaign_id, ad_group_id=req.ad_group_id)
    return res.model_dump_json(indent=2)

# [read] "Performance" tab: impressions, clicks, CTR, cost and Google's rating per asset over N days.
@mcp_tool()
def get_assets_performance(days: int = 30, field_type: Optional[str] = None) -> str:
    """
    Google Ads UI Tab 2: 'Asset Performance'.
    Retrieves Impressions, Clicks, CTR %, Cost (EUR), and Google AI Performance Label (BEST, GOOD, LOW, LEARNING, NOT_APPLICABLE)
    for in-ad assets (Headlines, Descriptions, Landscape/Square Images) and extensions (Sitelinks, Snippets, Callouts, Price).
    Extensions have no Google label; their HEURISTIC_* label is a CTR-based estimate.
    """
    res = perf_func(days=days, field_type=field_type)
    return res.model_dump_json(indent=2)

# [read] "Variety" tab: completeness of the asset set (images, sitelinks, snippets, ...), score 0-100%.
@mcp_tool()
def get_assets_diversity() -> str:
    """
    Google Ads UI Tab 3: 'Asset Variety & Health'.
    Audits active campaigns to ensure complete asset extension variety:
    Images (min 4), Sitelinks (min 4), Snippets, Callouts, Promotions, Price, Logo, and returns a 0-100% Quality Score.
    """
    res = diversity_func()
    return res.model_dump_json(indent=2)

# [write] Link an existing asset to the account, a campaign or a group.
@mcp_tool()
def link_asset_to_entity(level: str, entity_id: str, asset_id: str, field_type: str) -> str:
    """
    Binds an existing asset to Account ('CUSTOMER'), Campaign ('CAMPAIGN'), or Ad Group ('AD_GROUP').
    field_type: AD_IMAGE (search ad image; IMAGE does not exist in the API), SITELINK, CALLOUT, PROMOTION,
    STRUCTURED_SNIPPET, BUSINESS_NAME, BUSINESS_LOGO, PRICE.
    """
    req, rejected = build_request("link_asset_to_entity", LinkAssetRequest, level=level.upper(), entity_id=entity_id, asset_id=asset_id, field_type=field_type.upper())
    if rejected:
        return rejected
    res = link_func(req)
    return res.model_dump_json(indent=2)

# [write] Unlink an asset from the account, a campaign or a group.
@mcp_tool()
def unlink_asset_from_entity(level: str, entity_id: str, asset_id: str, field_type: str) -> str:
    """
    Unlinks/removes an asset association from Account ('CUSTOMER'), Campaign ('CAMPAIGN'), or Ad Group ('AD_GROUP').
    """
    req, rejected = build_request("unlink_asset_from_entity", UnlinkAssetRequest, level=level.upper(), entity_id=entity_id, asset_id=asset_id, field_type=field_type.upper())
    if rejected:
        return rejected
    res = unlink_func(req)
    return res.model_dump_json(indent=2)

# [write] Create a new Price Asset with validation of the offerings.
@mcp_tool()
def create_price_asset(
    name: str,
    offerings: List[dict],
    price_type: str = "PRODUCT_TIERS",
    price_qualifier: str = "FROM",
    language_code: str = "de"
) -> str:
    """
    Creates a new Price Asset in Google Ads.
    price_type: PRODUCT_TIERS, PRODUCT_CATEGORIES, SERVICES.
    price_qualifier: FROM, UP_TO, AVERAGE, UNSPECIFIED.
    offerings: list of dicts with header, description, price_eur, final_url.
    """
    req, rejected = build_request(
        "create_price_asset",
        CreatePriceAssetRequest,
        name=name,
        price_type=price_type,
        price_qualifier=price_qualifier,
        language_code=language_code,
        offerings=offerings
    )
    if rejected:
        return rejected
    res = create_price_func(req)
    return res.model_dump_json(indent=2)

# [read] Sitelinks of the site: what would be created and linked to the campaign, what exists already, how many there would be. Changes nothing. Always call before create_sitelink_assets.
@mcp_tool()
def preview_sitelink_assets(campaign_id: str, sitelinks: List[dict]) -> str:
    """
    Read-only dry run of create_sitelink_assets: validates the sitelinks and says, per sitelink, whether it would be
    created, reused (identical asset exists) or skipped (already linked), and how many sitelinks the campaign would
    have. sitelinks: list of dicts with link_text (max 25), description1 (max 35), description2 (max 35) and final_url
    (clean https address on a host of site.allowed_hosts of the policy, no query string). 2 to 6 sitelinks per call.
    """
    req, rejected = build_request("preview_sitelink_assets", CreateSitelinkAssetsRequest, action_type="READ",
                                  campaign_id=campaign_id, sitelinks=sitelinks)
    if rejected:
        return rejected
    res = preview_sitelinks_func(req)
    return res.model_dump_json(indent=2)

# [write] Create the site's sitelink assets and link them to the campaign. Only after preview_sitelink_assets and the owner's yes with the number of sitelinks.
@mcp_tool()
def create_sitelink_assets(campaign_id: str, sitelinks: List[dict]) -> str:
    """
    Creates the sitelink assets that do not exist yet and links all of them to the campaign. Identical existing assets
    are reused and already linked ones skipped, so a repeat never makes duplicates. Same input as preview_sitelink_assets,
    which must be called first. Writes to the ad account: only with the owner's explicit yes for this exact list.
    """
    req, rejected = build_request("create_sitelink_assets", CreateSitelinkAssetsRequest,
                                  campaign_id=campaign_id, sitelinks=sitelinks)
    if rejected:
        return rejected
    res = create_sitelinks_func(req)
    return res.model_dump_json(indent=2)

# [write] Callouts: create the missing texts and link them to the campaign. A repeat creates no duplicates. Only after the owner's yes on the exact list.
@mcp_tool()
def create_callout_assets(campaign_id: str, callouts: List[str]) -> str:
    """
    Creates callout assets that do not exist yet and links them to the campaign. An existing callout with the same
    text is reused, and one that is already linked is skipped, so a repeat writes nothing. callouts: 2 to 10 texts,
    each 1 to 25 characters. Words listed in site.forbidden_terms of the policy are refused. Writes to the ad account:
    only with the owner's explicit yes for this exact list.
    """
    req, rejected = build_request("create_callout_assets", CreateCalloutAssetsRequest,
                                  campaign_id=campaign_id, callouts=callouts)
    if rejected:
        return rejected
    res = create_callouts_func(req)
    return res.model_dump_json(indent=2)

# [write] One structured snippet: create or reuse and link to the campaign. The header only from Google's list.
@mcp_tool()
def create_structured_snippet_asset(campaign_id: str, header: str, values: List[str]) -> str:
    """
    Creates one structured snippet and links it to the campaign. An asset with the same header and the same set of
    values is reused. header must be an exact Google string: German Typen, Modelle, Stile, Dienstleistungen, Marken,
    Ausstattung, Kurse, Ziele, Studiengänge, Viertel, Versicherungsleistung, or English Types, Models, Styles, Brands,
    Services. Motive, Reiseziele and Versicherungsschutz are rejected (HEADER_NOT_SUPPORTED). values: 3 to 10 texts,
    each 1 to 25 characters, same forbidden words as callouts. If the campaign already has this header with different
    values, nothing is written. Writes to the ad account: only with the owner's explicit yes.
    """
    req, rejected = build_request("create_structured_snippet_asset", CreateStructuredSnippetAssetRequest,
                                  campaign_id=campaign_id, header=header, values=values)
    if rejected:
        return rejected
    res = create_snippet_func(req)
    return res.model_dump_json(indent=2)

# [write] Promotion asset: create (or reuse identical) and optionally link to a campaign or ad group; supports dry_run preview and read-back verification.
@mcp_tool()
def create_promotion_asset(
    name: str,
    promotion_target: str,
    final_url: str,
    language_code: str = "de",
    percent_off: Optional[int] = None,
    money_amount_off_eur: Optional[float] = None,
    currency_code: str = "EUR",
    discount_modifier: Optional[str] = None,
    promotion_code: Optional[str] = None,
    orders_over_eur: Optional[float] = None,
    occasion: Optional[str] = None,
    redemption_start_date: Optional[str] = None,
    redemption_end_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    campaign_id: Optional[str] = None,
    ad_group_id: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Creates a Promotion Asset (PROMOTION extension) in Google Ads, reads it back to confirm stored fields,
    and optionally links it to a Campaign (campaign_id) or Ad Group (ad_group_id). Reuses an identical existing
    promotion asset instead of creating duplicates.
    - promotion_target: promoted item/category (max 20 chars).
    - Discount: provide exactly one of percent_off (integer 1-100, stored in API as percent_off * 10,000) or money_amount_off_eur.
    - Trigger (optional): at most one of promotion_code (max 15 chars; omit for automatic sales without a code) or orders_over_eur.
    - Dates: ISO YYYY-MM-DD (redemption_start_date / redemption_end_date shown on ad; start_date / end_date serving window).
    - occasion: optional Google enum (e.g. BLACK_FRIDAY, CYBER_MONDAY, CHRISTMAS, WINTER_SALE).
    - Policy ads.url_rules forbid_query / forbid_text rules are enforced before calling Google.
    - dry_run=True validates everything and previews the action without writing to Google Ads.
    """
    req, rejected = build_request(
        "create_promotion_asset",
        CreatePromotionAssetRequest,
        name=name,
        promotion_target=promotion_target,
        final_url=final_url,
        language_code=language_code,
        percent_off=percent_off,
        money_amount_off_eur=money_amount_off_eur,
        currency_code=currency_code,
        discount_modifier=discount_modifier,
        promotion_code=promotion_code,
        orders_over_eur=orders_over_eur,
        occasion=occasion,
        redemption_start_date=redemption_start_date,
        redemption_end_date=redemption_end_date,
        start_date=start_date,
        end_date=end_date,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        dry_run=dry_run,
    )
    if rejected:
        return rejected
    res = create_promotion_func(req)
    return res.model_dump_json(indent=2)

# [write] Smart Facade: atomic price sync (create + find old + unlink + link)
@mcp_tool()
def sync_price_extension(
    entity_id: str,
    name: str,
    offerings: List[dict],
    level: str = "CAMPAIGN",
    price_type: str = "PRODUCT_TIERS",
    price_qualifier: str = "FROM",
    language_code: str = "de"
) -> str:
    """
    Smart Facade Macro Tool for Price Extensions:
    Atomically creates a new Price Asset with validated offerings (EUR prices, valid URLs, headers),
    discovers existing active Price Assets linked to the target entity, unlinks them, and links the new
    Price Asset in a single atomic flow.
    level: 'CAMPAIGN' (default), 'AD_GROUP', or 'CUSTOMER'.
    price_type: 'PRODUCT_TIERS' (default), 'PRODUCT_CATEGORIES', 'SERVICES'.
    price_qualifier: 'FROM' (default), 'UP_TO', 'AVERAGE', 'UNSPECIFIED'.
    language_code: 'de' (default).
    offerings: list of 3 to 8 dicts with 'header', 'description', 'price_eur', 'final_url'.
    """
    req, rejected = build_request(
        "sync_price_extension",
        SyncPriceExtensionRequest,
        entity_id=entity_id,
        level=level.upper(),
        name=name,
        offerings=offerings,
        price_type=price_type,
        price_qualifier=price_qualifier,
        language_code=language_code
    )
    if rejected:
        return rejected
    res = sync_price_func(req)
    return res.model_dump_json(indent=2)



# [write] Upload an image of the advertiser's site to the account's library (1.91:1 or 1:1, cut, JPEG); dry_run only checks. Does not link: that is link_asset_to_entity. Journal name: create_image_asset.
@mcp_tool()
def create_image_asset(source_url: str, ratio: Literal["LANDSCAPE", "SQUARE"], name: Optional[str] = None, dry_run: bool = False) -> str:
    """
    Uploads an image of the advertiser's own site (a host of site.allowed_hosts, e.g. the og image of a page, a product photo
    or the Merchant Center image_link) as an image asset. ratio LANDSCAPE = 1.91:1 (at least 600x314), SQUARE = 1:1 (at least 300x300);
    a picture outside the 1 % tolerance is cut from the centre, WebP is converted to JPEG. Any other host is refused.
    dry_run=True downloads and checks the picture and writes nothing. The same content is uploaded once (the name carries a
    content hash). Returns the asset_id; link it with link_asset_to_entity(level='AD_GROUP', field_type='AD_IMAGE').
    """
    req, rejected = build_request("create_image_asset", CreateImageAssetRequest, source_url=source_url, ratio=ratio, name=name, dry_run=dry_run)
    if rejected:
        return rejected
    res = create_image_func(req)
    return res.model_dump_json(indent=2)

# [read] All images of the account with full-size previews. Journal name: list_account_assets.
@mcp_tool()
def get_campaign_images(name_contains: Optional[str] = None) -> str:
    """
    Lists all image assets in the account (no 100-row cap) with full-size CDN preview URLs and pixel size
    (aspect_ratio "WxH (r:1)": 1.00 square, 1.91 landscape). name_contains filters by asset name, case-insensitive.
    """
    res = list_account_assets("IMAGE", name_contains=name_contains)
    return res.model_dump_json(indent=2)

# [read] Asset matrix of a campaign and its groups: which types are connected, which are missing.
@mcp_tool()
def get_campaign_assets_overview(campaign_id: str) -> str:
    """
    Inspects all Level 4 Assets (Objects) linked to a Campaign and its Ad Groups
    (Images, Sitelinks, Callouts, Structured Snippets, Promotions, Prices).
    Returns a comprehensive status matrix showing active and missing asset types.
    """
    rejected = _bad_ids("get_campaign_assets_overview", campaign_id=campaign_id)
    if rejected:
        return rejected
    res = assets_overview_func(campaign_id)
    return res.model_dump_json(indent=2)

# --- LEVEL 5: NEGATIVE SHIELDS & AUDITS ---

# [write] Load a language set of negative keywords (DE/FR/ES): DIY stores, "free", furniture.
@mcp_tool()
def apply_negative_shield(campaign_id: str, language: str = "de") -> str:
    """
    Deploys language-specific Negative Keyword Shield (DE, FR, ES) blocking DIY stores, gratis seekers, and furniture clutter.
    """
    req, rejected = build_request("apply_negative_shield", ApplyNegativeShieldRequest, campaign_id=campaign_id, language=language)
    if rejected:
        return rejected
    res = neg_shield_func(req)
    return res.model_dump_json(indent=2)

# [write] Add negative keywords to a campaign (PHRASE if several words, BROAD if one).
@mcp_tool()
def add_negative_keywords(campaign_id: str, keywords: List[str]) -> str:
    """
    Directly adds negative keywords to a campaign. Automatically sets match type:
    PHRASE if keyword contains whitespace, BROAD if single word.
    """
    rejected = _bad_ids("add_negative_keywords", action_type="MUTATION", campaign_id=campaign_id)
    if rejected:
        return rejected
    res = add_neg_func(campaign_id, keywords)
    return res.model_dump_json(indent=2)

# [write] Remove a campaign negative keyword by text (match_type is needed for duplicates).
@mcp_tool()
def remove_negative_keyword(campaign_id: str, keyword_text: str, match_type: Optional[str] = None) -> str:
    """
    Removes one campaign-level negative keyword by its exact text.
    match_type (EXACT, PHRASE, BROAD) is required only when the same text exists with several match types.
    """
    req, rejected = build_request("remove_negative_keyword", RemoveNegativeKeywordRequest, campaign_id=campaign_id, keyword_text=keyword_text, match_type=match_type)
    if rejected:
        return rejected
    res = remove_neg_func(req)
    return res.model_dump_json(indent=2)

# [write] Sync the account's shared negative keyword list (SharedSetService, SharedCriterionService, CampaignSharedSetService).
@mcp_tool()
def sync_shared_negative_list(
    list_name: str,
    keywords: Optional[List[str]] = None,
    match_type: str = "BROAD",
    campaign_ids: Optional[List[str]] = None
) -> str:
    """
    Synchronizes an account-level shared negative keyword list (SharedSetService, SharedCriterionService, CampaignSharedSetService).
    Creates or reuses the shared set, adds new negative keywords without duplicates, and attaches the list to target campaigns atomically.
    """
    req, rejected = build_request(
        "sync_shared_negative_list",
        SyncSharedNegativeListRequest,
        list_name=list_name,
        keywords=keywords,
        match_type=match_type,
        campaign_ids=campaign_ids
    )
    if rejected:
        return rejected
    res = sync_shared_neg_func(req)
    return res.model_dump_json(indent=2)

# [read] Search term analysis over N days: irrelevant ones without conversions are candidates for negatives. Journal name: mine_search_terms.
@mcp_tool()
def mine_search_terms_and_harvest_negatives(campaign_id: str, days: int = 14) -> str:
    """
    Scans search_term_view to identify non-converting, irrelevant search queries leaking ad budget.
    """
    rejected = _bad_ids("mine_search_terms", campaign_id=campaign_id)
    if rejected:
        return rejected
    res = mine_search_terms(campaign_id, days)
    return res.model_dump_json(indent=2)

# [read] Audit of a campaign on 4 levels (budget/ceiling, groups, RSA, negative shield), score 0-100%.
@mcp_tool()
def audit_campaign_4level_shield(campaign_id: str) -> str:
    """
    Comprehensive 4-Level Quality Audit:
    - Level 1: Campaign budget, primary status, Maximize Clicks CPC ceiling
    - Level 2: Ad Groups (bids checked only for MANUAL_CPC)
    - Level 3: RSA Copy (15/4 Matrix, the URL rules of the policy, Ad Strength notes)
    - Level 4: Negative Keyword Shield strength
    Returns an objective 0-100% Quality Score.
    """
    rejected = _bad_ids("audit_campaign_4level_shield", campaign_id=campaign_id)
    if rejected:
        return rejected
    res = audit_func(campaign_id)
    return res.model_dump_json(indent=2)

# [read] Impressions, clicks, cost by day and weekday per campaign.
@mcp_tool()
def get_daily_performance(days: int = 14) -> str:
    """
    Fetches daily impressions, clicks, cost (EUR), and day-of-week breakdown per campaign.
    """
    res = daily_perf_func(days)
    return res.model_dump_json(indent=2)

# [read] Impression share and losses due to budget and ad rank.
@mcp_tool()
def get_impression_share_loss(days: int = 7) -> str:
    """
    Fetches Impression Share and Lost Impression Share due to Budget and Ad Rank for active campaigns.
    """
    res = imp_share_func(days)
    return res.model_dump_json(indent=2)

# [read] Account change history (1-30 days): who changed what - UI, API, scripts, automated rules.
@mcp_tool()
def get_change_history(days: int = 7, campaign_id: Optional[str] = None, limit: int = 200) -> str:
    """
    Account change history (Google change_event, last 1-30 days): every change made via the Google Ads UI,
    the API (this MCP, scripts, other agents) or automated rules, with who made it and old -> new values.
    Use it to answer "what changed in the account"; the local audit log only sees calls made through this MCP.
    Newest first, at most `limit` events (1-500, default 200); `truncated` = true means older changes of the period
    were cut off: narrow days or campaign_id. changed_at is account time (Europe/Berlin), the journal is UTC.
    """
    rejected = _bad_ids("get_change_history", campaign_id=campaign_id)
    if rejected:
        return rejected
    res = change_history_func(days, campaign_id, limit)
    return res.model_dump_json(indent=2)

# [read] Reconcile Google's history with the MCP journal: which changes were made through the MCP and which outside it (scripts, other agents). Changes older than the journal are not reconciled: verdict NOT_VERIFIABLE_BEFORE_JOURNAL.
@mcp_tool()
def verify_changes_logged(days: int = 2, source: str = "local") -> str:
    """
    Reconciles Google's change history with the MCP audit log: which API changes in the account
    were made through this MCP (logged) and which were not (scripts, other agents). Also flags logged
    calls chained in one process (script_suspected) and logged mutations that left no change in the account.
    source="local": this machine's google-ads/logs/audit.jsonl; source="cloud": the online journal of every
    operator (needs the reader key), so other operators' MCP changes count as logged; source="auto": the online
    journal if it reaches back over the whole period, else the local file. Changes older than the journal cannot be
    checked (before_log_started); if that is all there is, the verdict is NOT_VERIFIABLE_BEFORE_JOURNAL. Reads up to
    9999 changes of the period; if Google has more, the verdict is INCOMPLETE_HISTORY (history_truncated=true) and the
    calls after the cut-off are unchecked_after_truncation: narrow `days`. Dry runs (dry_run=true) are only counted in
    dry_run_calls; a change is credited to the call whose arguments name the same ids, then to the nearest in time. Read-only.
    """
    res = verify_logged_func(days, source)
    return res.model_dump_json(indent=2)

# --- OBSERVABILITY: ONLINE JOURNAL (Cloud Logging) AND TRACES (Cloud Trace) ---

# [read] The MCP's online journal from Cloud Logging, all machines (filters: severity, operator, function, session_id); start error analysis here.
@mcp_tool()
def get_cloud_logs(hours: int = 24, severity: Optional[str] = None, operator: Optional[str] = None,
                   function: Optional[str] = None, request_id: Optional[str] = None, limit: int = 50,
                   session_id: Optional[str] = None, tool: Optional[str] = None) -> str:
    """
    Online journal of this MCP from every operator's machine (Cloud Logging, last 1-720 hours).
    Filters: severity = a minimum level (DEBUG observer reads, INFO read, NOTICE account change, WARNING rejected by the
    shield, ERROR failure, CRITICAL token dead; default INFO hides the observer's own DEBUG entries), operator,
    function (Python function, e.g. step_campaign_cpc_ceiling), tool (MCP tool name, e.g. step_bid_threshold; entries
    from 06.10.2026 on),
    request_id (Google Ads), session_id (one agent session = one MCP server process; every entry carries it).
    Returns counts by severity / operator / function / error code, the newest entries
    with args, error, latency and trace_id, and Logs Explorer links. Start any error or failure investigation here;
    the local audit.jsonl only has this machine's calls. Read-only.
    """
    res = cloud_logs_func(hours, severity, operator, function, request_id, limit, session_id, tool)
    return res.model_dump_json(indent=2)

# [read] Trace of one call from Cloud Trace: timings and where the time went (Google Ads API or our own code).
@mcp_tool()
def get_cloud_trace(trace_id: str) -> str:
    """
    One tool call from Cloud Trace by trace_id (from get_cloud_logs or a ServiceResult telemetry):
    the call span and every Google Ads API call inside it with timings, and where the time went
    (Google Ads API vs our own code). Use it for slow or failed calls. Read-only.
    """
    res = cloud_trace_func(trace_id)
    return res.model_dump_json(indent=2)

# [read] Health of the MCP itself from the journal: errors, p50/p95, findings (CODE_BUG, SLOW, AUTH...) and anomalies against the previous window (new error code, spike, rejection loop, edits by one process). What to fix.
@mcp_tool()
def analyze_tool_health(days: int = 7, operator: Optional[str] = None, since: Optional[str] = None, source: str = "auto") -> str:
    """
    How well this MCP itself works, from the journal (online journal of every operator if the reader key is set,
    else this machine's audit.jsonl): per function calls, error rate, p50/p95 latency, error codes, and findings
    with what to fix and where — CODE_BUG (exception in tools/*.py), UNCLEAR_CONTRACT (agents rejected by the
    Pydantic shield and retrying), MISSING_PREVALIDATION (Google rejects what the shield could catch), SLOW,
    AUTH, GOOGLE_SIDE. Anomalies are measured against the window of equal length before it: NEW_ERROR_CODE,
    ERROR_SPIKE, REJECTION_BURST (agent looping on the shield), MUTATION_CHAIN (account changes made by one
    process, i.e. a script), UNKNOWN_OPERATOR (change from a clone without MCP_OPERATOR); baseline_calls /
    baseline_note say whether the baseline is long enough. since="2026-10-05" analyses only calls after a fix,
    to confirm it worked. source: auto (the online journal of every operator only if it reaches back to the window
    start, else this machine's audit.jsonl, which starts earlier), cloud, local. Read-only; code changes it suggests
    need the owner's approval.
    """
    res = tool_health_func(days, operator, since, source)
    return res.model_dump_json(indent=2)

# [read] Check whether the agent kept the skill's working order (READ_FIRST, ONE_LEVER, AUTO_FIX_ALONE, VERIFY_AFTER); advises only. record=True writes the verdicts to the cloud log.
@mcp_tool()
def evaluate_agent_process(days: int = 7, since: Optional[str] = None, session_id: Optional[str] = None,
                           operator: Optional[str] = None, source: str = "auto", record: bool = False) -> str:
    """
    Did the agent follow the working order of the google-ads-mcp skill? Judges every session that changed the
    account, from the journal, with fixed rules (no model): READ_FIRST (get_change_history before the first change),
    ONE_LEVER (changes made together touch one lever group), AUTO_FIX_ALONE (auto_fix_collisions_and_dupes never
    together with other changes), VERIFY_AFTER (verify_changes_logged 15 min to 24 h after the changes).
    Returns PASS/FAIL/PENDING counts per rule and the FAIL/PENDING verdicts with session_id and trace_id.
    since="2026-10-05" or session_id narrow it; source: auto (online journal of every operator only if it reaches
    back to the window start, else this machine's audit.jsonl), cloud, local. record=True also writes the final verdicts to the cloud log
    google-ads-mcp-evals, each once. Advisory: it blocks nothing. Run it after any session that changed ads.
    """
    res = process_eval_func(days, since, session_id, operator, source, record)
    return res.model_dump_json(indent=2)

# [write] Find duplicate keywords between the priority campaign (policy ads.priority_campaign_marker) and the other campaigns, and pause the duplicates in the other campaigns.
@mcp_tool()
def auto_fix_collisions_and_dupes() -> str:
    """
    Scans active campaigns for duplicate keywords between the priority campaign (campaign name containing
    ads.priority_campaign_marker of the policy) and all other campaigns, and automatically pauses the duplicates
    in the other campaigns.
    """
    res = fix_collisions_func()
    return res.model_dump_json(indent=2)

# [read] Google AI recommendations from the Recommendations API (view only).
@mcp_tool()
def fetch_google_ai_recommendations() -> str:
    """
    Queries Google Ads Recommendations API to audit Google AI suggestions.
    """
    res = ai_recs_func()
    return res.model_dump_json(indent=2)

# [read] Keyword ideas and search volume via KeywordPlanIdeaService (default: German, Germany).
@mcp_tool()
def search_keyword_ideas(seeds: List[str], language_id: str = "1001", location_ids: List[str] = ["2276"]) -> str:
    """
    Fetches keyword ideas and search volumes using Google Ads KeywordPlanIdeaService.
    Default: German (1001), Germany (2276).
    """
    res = keyword_ideas_func(seeds, language_id, location_ids)
    return res.model_dump_json(indent=2)

# [read] Enhanced Conversions audit: conversion_tracking_setting status, Customer Data Terms acceptance, list of primary/secondary conversions.
@mcp_tool()
def check_enhanced_conversions_status() -> str:
    """
    Audits the Google Ads account conversion tracking configuration, Enhanced Conversions
    enablement settings, Customer Data Terms acceptance, and active primary/secondary conversion actions.
    """
    res = check_conversions_func()
    return res.model_dump_json(indent=2)

# [read] Minimum budget threshold for Demand Gen / Video campaigns in the chosen currency via currency_constant.
@mcp_tool()
def get_demand_gen_currency_floor(currency_code: str = "EUR") -> str:
    """
    Queries Google Ads currency_constant resource to determine the exact minimum daily budget floor
    for Demand Gen / Video campaigns in the specified currency (default: EUR).
    """
    res = currency_floor_func(currency_code)
    return res.model_dump_json(indent=2)

# [read] Audit of the physical location of clicking users (user_location_view): find budget leaks outside the target countries.
@mcp_tool()
def audit_geo_physical_locations(days: int = 30) -> str:
    """
    Audits physical user locations from user_location_view to detect budget wasted
    on users outside target countries (e.g. clicks from US, TR, IN via 'Presence or Interest' default).
    Returns breakdown of spend, clicks, and specific campaigns with foreign traffic leakage.
    """
    res = geo_audit_func(days)
    return res.model_dump_json(indent=2)

# [write] Upload offline/server conversions (e.g. a payment, a storefront order) to Google Ads via ConversionUploadService.
@mcp_tool()
def upload_click_conversion(
    conversion_action_id: str,
    conversion_date_time: str,
    conversion_value: Optional[float] = None,
    currency_code: str = "EUR",
    order_id: Optional[str] = None,
    gclid: Optional[str] = None,
    gbraid: Optional[str] = None,
    wbraid: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    ad_user_data_consent: str = "GRANTED",
    ad_personalization_consent: str = "GRANTED"
) -> str:
    """
    Uploads an offline/server-side click conversion (e.g. Stripe checkout purchase, backend order)
    to Google Ads API via ConversionUploadService. Supports gclid, iOS gbraid/wbraid,
    order_id deduplication, EU DMA Consent (ad_user_data, ad_personalization), and Enhanced Conversions hashed customer data.
    """
    item = ClickConversionItem(
        conversion_action_id=conversion_action_id,
        conversion_date_time=conversion_date_time,
        conversion_value=conversion_value,
        currency_code=currency_code,
        order_id=order_id,
        gclid=gclid,
        gbraid=gbraid,
        wbraid=wbraid,
        email=email,
        phone=phone,
        ad_user_data_consent=ad_user_data_consent,
        ad_personalization_consent=ad_personalization_consent
    )
    res = upload_conversions_func([item])
    return res.model_dump_json(indent=2)

# [write] Manage the goal status of a conversion action (Primary vs Secondary) to steer Smart Bidding.
@mcp_tool()
def set_conversion_action_goal(
    conversion_action_id: str,
    primary_for_goal: bool,
    status: Optional[Literal["ENABLED", "PAUSED", "HIDDEN", "REMOVED"]] = None,
    include_in_conversions_metric: Optional[bool] = None
) -> str:
    """
    Updates a Google Ads conversion action's goal optimization status (Primary vs Secondary),
    serving status (ENABLED, PAUSED, HIDDEN, REMOVED), and inclusion in the Conversions metric.
    Use this to make the purchase on your own site Primary and demote a legacy purchase action to Secondary.
    """
    req, rejected = build_request(
        "set_conversion_action_goal",
        SetConversionActionGoalRequest,
        conversion_action_id=conversion_action_id,
        primary_for_goal=primary_for_goal,
        status=status,
        include_in_conversions_metric=include_in_conversions_metric
    )
    if rejected:
        return rejected
    res = set_conversion_goal_func(req)
    return res.model_dump_json(indent=2)

# [read] Retrieve official invoices and credit notes of Google Ads via InvoiceService and BillingSetup.
@mcp_tool()
def get_google_ads_invoices(year: Optional[str] = None, month: Optional[str] = None) -> str:
    """
    Retrieves official Google Ads invoices and credit memos via InvoiceService and BillingSetup,
    including subtotal, VAT 19% / Reverse Charge, total EUR, and direct PDF download links for accounting & tax compliance.
    """
    res = invoices_func(year, month)
    return res.model_dump_json(indent=2)

# [read] Audit of the link between the Google Ads account and Google Merchant Center (ProductLink / ProductLinkInvitation).
@mcp_tool()
def check_merchant_center_link(merchant_id: Optional[str] = None) -> str:
    """
    Audits the Google Merchant Center product link status and link invitations
    in Google Ads via ProductLink and ProductLinkInvitation resources.
    Verifies that the product catalog and feed are actively connected for Shopping and Performance Max.
    """
    res = mc_link_func(merchant_id=merchant_id or MERCHANT_CENTER_ID or None)
    return res.model_dump_json(indent=2)

# [read] Shopping readiness on both sides: link, Shopping ads program in Merchant Center, feed label, approved products, what Google Ads sees, other Shopping campaigns. When Merchant Center cannot be read its checks are SKIPPED with a reason. Journal name: check_shopping_readiness (verdict and gmc_skipped_reason).
@mcp_tool()
def check_shopping_readiness(
    merchant_id: Optional[str] = None,
    feed_label: Optional[str] = None,
    geo_target_constants: Optional[List[int]] = None,
    custom_label_0_values: Optional[List[str]] = None
) -> str:
    """
    Checks that a Shopping campaign on the Merchant Center feed has something to show, reading both accounts: the Ads <-> Merchant
    Center link, the Shopping ads program for the countries (2276 Germany, 2040 Austria), the feed label, approved products per
    country, the products Google Ads itself sees, and other Shopping campaigns on the same Merchant Center. custom_label_0_values:
    values of g:custom_label_0 the campaign will select or leave out; each must exist in the catalog. Answer: verdict READY /
    READY_WITH_WARNINGS / BLOCKED and a list of checks (OK / WARN / FAIL / SKIPPED). Reads only.
    """
    req, rejected = build_request(
        "check_shopping_readiness", CheckShoppingReadinessRequest, action_type="READ",
        **{key: value for key, value in (("merchant_id", merchant_id), ("feed_label", feed_label)) if value is not None},
        **({"geo_target_constants": geo_target_constants} if geo_target_constants else {}),
        custom_label_0_values=custom_label_0_values or []
    )
    if rejected:
        return rejected
    res = shopping_readiness_func(req)
    return res.model_dump_json(indent=2)

# Expose bundled Agent Skills (skills/*/SKILL.md + references/*.md) over MCP Resources (SEP-2640 structure)
register_skills_over_mcp(mcp, skills_dir=CURRENT_DIR / "skills")

if __name__ == "__main__":
    cloud_sink.start()
    mcp.run()
