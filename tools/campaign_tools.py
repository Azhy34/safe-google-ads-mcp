import sys
from pathlib import Path
from typing import List, Optional
from google.api_core import protobuf_helpers

# Add root of google-ads to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client, eur_to_micros
from core.config import CUSTOMER_ID
from shield.error_shield import ServiceResult, handle_google_ads_errors
from shield.models_campaign import (
    GoogleCampaignRecord, CampaignBudgetRecord, 
    CampaignStatusUpdateRequest, CampaignBudgetUpdateRequest,
    CampaignTargetingRecord, CampaignCeilingStepRequest,
    CreateSearchCampaignRequest, CreateCampaignResponse
)

CEILING_MIN_MICROS = 100_000    # 0.10 EUR
CEILING_MAX_MICROS = 3_000_000  # 3.00 EUR

def _ceiling_eur(c) -> Optional[float]:
    """Maximize Clicks CPC ceiling in EUR, None when uncapped or not TARGET_SPEND."""
    micros = c.target_spend.cpc_bid_ceiling_micros
    return round(micros / 1_000_000.0, 2) if micros else None

@handle_google_ads_errors
def list_campaigns(customer_id: str = CUSTOMER_ID) -> List[GoogleCampaignRecord]:
    """
    Queries all non-removed campaigns and returns typed GoogleCampaignRecord models.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    
    query = """
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            campaign.bidding_strategy_type,
            campaign.target_spend.cpc_bid_ceiling_micros,
            campaign_budget.resource_name,
            campaign_budget.amount_micros,
            campaign_budget.delivery_method
        FROM campaign
        WHERE campaign.status != 'REMOVED'
        ORDER BY campaign.name ASC
    """
    response = ga_service.search(customer_id=customer_id, query=query)
    
    results = []
    for row in response:
        c = row.campaign
        b = row.campaign_budget
        budget_rec = None
        if b and b.amount_micros:
            budget_rec = CampaignBudgetRecord(
                resource_name=b.resource_name,
                amount_micros=b.amount_micros,
                amount_eur=round(b.amount_micros / 1_000_000.0, 2),
                delivery_method=b.delivery_method.name if hasattr(b.delivery_method, "name") else str(b.delivery_method)
            )
        
        status_name = c.status.name if hasattr(c.status, "name") else str(c.status)
        channel_name = c.advertising_channel_type.name if hasattr(c.advertising_channel_type, "name") else str(c.advertising_channel_type)
        bidding_name = c.bidding_strategy_type.name if hasattr(c.bidding_strategy_type, "name") else str(c.bidding_strategy_type)
        
        results.append(GoogleCampaignRecord(
            id=str(c.id),
            resource_name=c.resource_name,
            name=c.name,
            status=status_name,
            advertising_channel_type=channel_name,
            bidding_strategy_type=bidding_name,
            budget=budget_rec,
            cpc_ceiling_eur=_ceiling_eur(c)
        ))
    return results

@handle_google_ads_errors
def get_campaign_detail(campaign_id: str, customer_id: str = CUSTOMER_ID) -> Optional[GoogleCampaignRecord]:
    """
    Fetches detailed settings for a single campaign.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    
    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            campaign.bidding_strategy_type,
            campaign.target_spend.cpc_bid_ceiling_micros,
            campaign_budget.resource_name,
            campaign_budget.amount_micros,
            campaign_budget.delivery_method
        FROM campaign
        WHERE campaign.id = {campaign_id}
        LIMIT 1
    """
    response = ga_service.search(customer_id=customer_id, query=query)
    for row in response:
        c = row.campaign
        b = row.campaign_budget
        budget_rec = None
        if b and b.amount_micros:
            budget_rec = CampaignBudgetRecord(
                resource_name=b.resource_name,
                amount_micros=b.amount_micros,
                amount_eur=round(b.amount_micros / 1_000_000.0, 2),
                delivery_method=b.delivery_method.name if hasattr(b.delivery_method, "name") else str(b.delivery_method)
            )
        return GoogleCampaignRecord(
            id=str(c.id),
            resource_name=c.resource_name,
            name=c.name,
            status=c.status.name if hasattr(c.status, "name") else str(c.status),
            advertising_channel_type=c.advertising_channel_type.name if hasattr(c.advertising_channel_type, "name") else str(c.advertising_channel_type),
            bidding_strategy_type=c.bidding_strategy_type.name if hasattr(c.bidding_strategy_type, "name") else str(c.bidding_strategy_type),
            budget=budget_rec,
            cpc_ceiling_eur=_ceiling_eur(c)
        )
    return None

@handle_google_ads_errors
def update_campaign_status(req: CampaignStatusUpdateRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Safely mutates campaign status using official sparse update_mask.
    """
    client = get_ads_client()
    campaign_service = client.get_service("CampaignService")
    
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = campaign_service.campaign_path(customer_id, req.campaign_id)
    
    status_enum = client.enums.CampaignStatusEnum
    campaign.status = status_enum.ENABLED if req.enabled else status_enum.PAUSED
    operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, campaign._pb))
    
    campaign_service.mutate_campaigns(customer_id=customer_id, operations=[operation])
    state = "ENABLED" if req.enabled else "PAUSED"
    return f"Campaign {req.campaign_id} successfully set to {state}."

@handle_google_ads_errors
def update_campaign_budget(req: CampaignBudgetUpdateRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Safely updates daily campaign budget in EUR using official sparse update_mask.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    budget_service = client.get_service("CampaignBudgetService")
    
    # 1. Fetch budget resource name
    query = f"SELECT campaign.campaign_budget FROM campaign WHERE campaign.id = {req.campaign_id} LIMIT 1"
    res = list(ga_service.search(customer_id=customer_id, query=query))
    if not res:
        raise ValueError(f"Campaign {req.campaign_id} not found.")
    
    budget_resource_name = res[0].campaign.campaign_budget
    
    # 2. Mutate budget
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.update
    budget.resource_name = budget_resource_name
    budget.amount_micros = eur_to_micros(req.budget_eur)
    operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, budget._pb))
    
    budget_service.mutate_campaign_budgets(customer_id=customer_id, operations=[operation])
    return f"Campaign {req.campaign_id} budget updated to {req.budget_eur:.2f} EUR/day."

@handle_google_ads_errors
def step_campaign_cpc_ceiling(req: CampaignCeilingStepRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Steps the Maximize Clicks CPC ceiling (campaign.target_spend.cpc_bid_ceiling_micros) by +/-0.05 EUR.
    Under TARGET_SPEND the ceiling is the only effective bid lever; ad group cpc_bid_micros is ignored.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    campaign_service = client.get_service("CampaignService")

    query = f"""
        SELECT campaign.id, campaign.name, campaign.bidding_strategy_type,
               campaign.target_spend.cpc_bid_ceiling_micros
        FROM campaign
        WHERE campaign.id = {req.campaign_id}
        LIMIT 1
    """
    res = list(ga_service.search(customer_id=customer_id, query=query))
    if not res:
        raise ValueError(f"Campaign {req.campaign_id} not found.")

    c = res[0].campaign
    if c.bidding_strategy_type.name != "TARGET_SPEND":
        raise ValueError(f"Campaign '{c.name}' uses {c.bidding_strategy_type.name}, not TARGET_SPEND (Maximize Clicks); it has no CPC ceiling to step.")

    curr_micros = c.target_spend.cpc_bid_ceiling_micros
    if not curr_micros:
        raise ValueError(f"Campaign '{c.name}' has no CPC ceiling set (uncapped); refusing to guess a starting value.")

    step_micros = 50_000 if req.direction == "+0.05" else -50_000
    new_micros = curr_micros + step_micros
    if not CEILING_MIN_MICROS <= new_micros <= CEILING_MAX_MICROS:
        raise ValueError(f"New ceiling {new_micros / 1e6:.2f} EUR is outside the 0.10-3.00 EUR safeguard.")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = campaign_service.campaign_path(customer_id, req.campaign_id)
    campaign.target_spend.cpc_bid_ceiling_micros = new_micros
    operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, campaign._pb))

    campaign_service.mutate_campaigns(customer_id=customer_id, operations=[operation])
    return f"Campaign '{c.name}' ({req.campaign_id}) CPC ceiling updated from {curr_micros / 1e6:.2f} EUR to {new_micros / 1e6:.2f} EUR."

@handle_google_ads_errors
def get_campaign_targeting_overview(campaign_id: str = "24015242821", customer_id: str = CUSTOMER_ID) -> CampaignTargetingRecord:
    """
    Audits campaign targeting criteria (Geo Locations, Languages, Dayparting Ad Schedules, Device Modifiers)
    and network settings (Google Search vs Display Network).
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    
    # 1. Fetch network settings
    q_net = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.network_settings.target_google_search,
            campaign.network_settings.target_search_network,
            campaign.network_settings.target_content_network
        FROM campaign
        WHERE campaign.id = {campaign_id}
        LIMIT 1
    """
    res_net = list(ga_service.search(customer_id=customer_id, query=q_net))
    if not res_net:
        raise ValueError(f"Campaign {campaign_id} not found.")
        
    c = res_net[0].campaign
    cname = c.name
    net = c.network_settings
    
    # 2. Fetch targeting criteria
    q_crit = f"""
        SELECT
            campaign.id,
            campaign_criterion.criterion_id,
            campaign_criterion.type,
            campaign_criterion.negative,
            campaign_criterion.location.geo_target_constant,
            campaign_criterion.language.language_constant,
            campaign_criterion.ad_schedule.day_of_week,
            campaign_criterion.ad_schedule.start_hour,
            campaign_criterion.ad_schedule.start_minute,
            campaign_criterion.ad_schedule.end_hour,
            campaign_criterion.ad_schedule.end_minute,
            campaign_criterion.device.type,
            campaign_criterion.bid_modifier
        FROM campaign_criterion
        WHERE campaign.id = {campaign_id}
    """
    res_crit = ga_service.search(customer_id=customer_id, query=q_crit)
    
    locations = []
    ex_locations = []
    languages = []
    schedules = []
    devices = []
    
    for r in res_crit:
        cc = r.campaign_criterion
        ctype = cc.type_.name
        if ctype == "LOCATION":
            loc_id = cc.location.geo_target_constant.split("/")[-1] if cc.location and cc.location.geo_target_constant else str(cc.criterion_id)
            loc_name = "Germany (2276)" if loc_id == "2276" else ("Austria (2040)" if loc_id == "2040" else f"Geo {loc_id}")
            if cc.negative:
                ex_locations.append(loc_name)
            else:
                locations.append(loc_name)
        elif ctype == "LANGUAGE":
            lang_id = cc.language.language_constant.split("/")[-1] if cc.language and cc.language.language_constant else str(cc.criterion_id)
            lang_name = "German (1001)" if lang_id == "1001" else ("French (1002)" if lang_id == "1002" else ("Spanish (1003)" if lang_id == "1003" else f"Lang {lang_id}"))
            languages.append(lang_name)
        elif ctype == "AD_SCHEDULE":
            sch = cc.ad_schedule
            schedules.append(f"{sch.day_of_week.name}: {sch.start_hour:02d}:{sch.start_minute.name} - {sch.end_hour:02d}:{sch.end_minute.name}")
        elif ctype == "DEVICE":
            dev_name = cc.device.type_.name if cc.device else "DEVICE"
            devices.append(f"{dev_name} (bid mod: {cc.bid_modifier})")
            
    return CampaignTargetingRecord(
        campaign_id=str(c.id),
        campaign_name=cname,
        target_google_search=net.target_google_search,
        target_search_network=net.target_search_network,
        target_content_network=net.target_content_network,
        targeted_locations=locations,
        excluded_locations=ex_locations,
        targeted_languages=languages,
        ad_schedules=schedules,
        device_modifiers=devices
    )

@handle_google_ads_errors
def create_search_campaign(req: CreateSearchCampaignRequest, customer_id: str = CUSTOMER_ID) -> CreateCampaignResponse:
    """
    Creates a new Google Ads Search campaign with dedicated budget, Maximize Clicks (TARGET_SPEND)
    strategy with CPC ceiling, Clean Search network settings (Partners/Display OFF), geo/language criteria,
    dayparting schedule (08:00 - 01:00), an initial ad group and clean phrase/exact keywords.
    """
    import time
    client = get_ads_client()
    
    # 1. Create Dedicated Budget
    budget_service = client.get_service("CampaignBudgetService")
    budget_op = client.get_type("CampaignBudgetOperation")
    budget = budget_op.create
    budget.name = f"Budget - {req.name} #{int(time.time())}"
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.amount_micros = eur_to_micros(req.budget_eur)
    budget.explicitly_shared = False
    
    budget_res = budget_service.mutate_campaign_budgets(customer_id=customer_id, operations=[budget_op])
    budget_resource_name = budget_res.results[0].resource_name
    budget_id = budget_resource_name.split("/")[-1]
    
    # 2. Create Campaign
    campaign_service = client.get_service("CampaignService")
    campaign_op = client.get_type("CampaignOperation")
    campaign = campaign_op.create
    campaign.name = req.name
    campaign.campaign_budget = budget_resource_name
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    campaign.status = client.enums.CampaignStatusEnum.PAUSED if req.status == "PAUSED" else client.enums.CampaignStatusEnum.ENABLED
    
    # Bidding strategy: TARGET_SPEND with CPC ceiling
    campaign.bidding_strategy_type = client.enums.BiddingStrategyTypeEnum.TARGET_SPEND
    campaign.target_spend.cpc_bid_ceiling_micros = eur_to_micros(req.cpc_ceiling_eur)
    
    # Clean Search Network settings (no display, no partners)
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = False
    campaign.network_settings.target_content_network = False
    campaign.network_settings.target_partner_search_network = False
    
    # Positive & Negative geo target type: PRESENCE (people physically in targeted locations)
    campaign.geo_target_type_setting.positive_geo_target_type = client.enums.PositiveGeoTargetTypeEnum.PRESENCE
    campaign.geo_target_type_setting.negative_geo_target_type = client.enums.NegativeGeoTargetTypeEnum.PRESENCE
    
    # EU declaration standard
    campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    
    campaign_res = campaign_service.mutate_campaigns(customer_id=customer_id, operations=[campaign_op])
    campaign_resource_name = campaign_res.results[0].resource_name
    campaign_id = campaign_resource_name.split("/")[-1]
    
    # 3. Add Targeting Criteria (Locations, Languages, Dayparting Schedule)
    crit_service = client.get_service("CampaignCriterionService")
    crit_ops = []
    
    # Targeted Geo Constants
    for geo_id in req.geo_target_constants:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = campaign_resource_name
        crit.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
        crit_ops.append(op)
        
    # Targeted Languages
    for lang_id in req.language_constants:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = campaign_resource_name
        crit.language.language_constant = f"languageConstants/{lang_id}"
        crit_ops.append(op)
        
    # Dayparting Schedule: 08:00 - 24:00 and 00:00 - 01:00 for Monday-Sunday
    all_days = [
        client.enums.DayOfWeekEnum.MONDAY,
        client.enums.DayOfWeekEnum.TUESDAY,
        client.enums.DayOfWeekEnum.WEDNESDAY,
        client.enums.DayOfWeekEnum.THURSDAY,
        client.enums.DayOfWeekEnum.FRIDAY,
        client.enums.DayOfWeekEnum.SATURDAY,
        client.enums.DayOfWeekEnum.SUNDAY,
    ]
    for d in all_days:
        # Window 1: 08:00 - 24:00
        op1 = client.get_type("CampaignCriterionOperation")
        c1 = op1.create
        c1.campaign = campaign_resource_name
        c1.ad_schedule.day_of_week = d
        c1.ad_schedule.start_hour = 8
        c1.ad_schedule.start_minute = client.enums.MinuteOfHourEnum.ZERO
        c1.ad_schedule.end_hour = 24
        c1.ad_schedule.end_minute = client.enums.MinuteOfHourEnum.ZERO
        crit_ops.append(op1)
        
        # Window 2: 00:00 - 01:00
        op2 = client.get_type("CampaignCriterionOperation")
        c2 = op2.create
        c2.campaign = campaign_resource_name
        c2.ad_schedule.day_of_week = d
        c2.ad_schedule.start_hour = 0
        c2.ad_schedule.start_minute = client.enums.MinuteOfHourEnum.ZERO
        c2.ad_schedule.end_hour = 1
        c2.ad_schedule.end_minute = client.enums.MinuteOfHourEnum.ZERO
        crit_ops.append(op2)
        
    if crit_ops:
        crit_service.mutate_campaign_criteria(customer_id=customer_id, operations=crit_ops)
        
    # 4. Create Initial Ad Group
    ag_service = client.get_service("AdGroupService")
    ag_op = client.get_type("AdGroupOperation")
    ag = ag_op.create
    ag.name = req.ad_group_name
    ag.campaign = campaign_resource_name
    ag.status = client.enums.AdGroupStatusEnum.ENABLED
    ag.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    
    ag_res = ag_service.mutate_ad_groups(customer_id=customer_id, operations=[ag_op])
    ad_group_resource_name = ag_res.results[0].resource_name
    ad_group_id = ad_group_resource_name.split("/")[-1]
    
    # 5. Add Initial Keywords
    kw_count = 0
    if req.keywords:
        agc_service = client.get_service("AdGroupCriterionService")
        agc_ops = []
        for kw in req.keywords:
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            op = client.get_type("AdGroupCriterionOperation")
            agc = op.create
            agc.ad_group = ad_group_resource_name
            agc.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            
            if kw_clean.startswith("[") and kw_clean.endswith("]"):
                agc.keyword.text = kw_clean[1:-1].strip()
                agc.keyword.match_type = client.enums.KeywordMatchTypeEnum.EXACT
            elif kw_clean.startswith('"') and kw_clean.endswith('"'):
                agc.keyword.text = kw_clean[1:-1].strip()
                agc.keyword.match_type = client.enums.KeywordMatchTypeEnum.PHRASE
            else:
                agc.keyword.text = kw_clean
                agc.keyword.match_type = client.enums.KeywordMatchTypeEnum.PHRASE
            agc_ops.append(op)
            
        if agc_ops:
            agc_service.mutate_ad_group_criteria(customer_id=customer_id, operations=agc_ops)
            kw_count = len(agc_ops)
            
    return CreateCampaignResponse(
        campaign_id=campaign_id,
        campaign_resource_name=campaign_resource_name,
        budget_id=budget_id,
        ad_group_id=ad_group_id,
        status="PAUSED" if req.status == "PAUSED" else "ENABLED",
        cpc_ceiling_eur=req.cpc_ceiling_eur,
        keywords_count=kw_count
    )
