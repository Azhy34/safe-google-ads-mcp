import sys
from pathlib import Path
from typing import List
from google.api_core import protobuf_helpers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client, eur_to_micros
from core.config import CUSTOMER_ID
from shield.error_shield import ApiError, ServiceResult, Telemetry, handle_google_ads_errors
from shield.models_adgroup import (
    GoogleAdGroupRecord, AdGroupBidUpdateRequest, AdGroupKeywordRecord, KeywordStatusUpdateRequest,
    AdGroupStatusUpdateRequest,
    CreateAdGroupRequest, CreateAdGroupResponse, AddKeywordsRequest, AddKeywordsResponse
)

@handle_google_ads_errors
def list_ad_groups(campaign_id: str = None, customer_id: str = CUSTOMER_ID) -> List[GoogleAdGroupRecord]:
    """
    Queries ad groups with their base CPC bids and returns typed GoogleAdGroupRecord models.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    
    where_clause = "WHERE ad_group.status != 'REMOVED'"
    if campaign_id:
        where_clause += f" AND campaign.id = {campaign_id}"
        
    query = f"""
        SELECT
            ad_group.id,
            ad_group.name,
            ad_group.status,
            ad_group.type,
            ad_group.cpc_bid_micros,
            campaign.id
        FROM ad_group
        {where_clause}
        ORDER BY ad_group.name ASC
    """
    response = ga_service.search(customer_id=customer_id, query=query)
    
    results = []
    for row in response:
        ag = row.ad_group
        cpc_micros = ag.cpc_bid_micros or 0
        results.append(GoogleAdGroupRecord(
            id=str(ag.id),
            resource_name=ag.resource_name,
            campaign_id=str(row.campaign.id),
            name=ag.name,
            status=ag.status.name if hasattr(ag.status, "name") else str(ag.status),
            type=ag.type_.name if hasattr(ag.type_, "name") else str(ag.type_),
            cpc_bid_micros=cpc_micros,
            cpc_bid_eur=round(cpc_micros / 1_000_000.0, 2)
        ))
    return results

@handle_google_ads_errors
def update_ad_group_bid(req: AdGroupBidUpdateRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Mutates ad group base CPC bid. Only meaningful for MANUAL_CPC campaigns:
    under TARGET_SPEND (Maximize Clicks) Google ignores ad group bids without error,
    so the call is refused there - use step_campaign_cpc_ceiling instead.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    ad_group_service = client.get_service("AdGroupService")

    query = f"""
        SELECT ad_group.name, campaign.name, campaign.bidding_strategy_type
        FROM ad_group
        WHERE ad_group.id = {req.ad_group_id}
        LIMIT 1
    """
    res = list(ga_service.search(customer_id=customer_id, query=query))
    if not res:
        raise ValueError(f"AdGroup {req.ad_group_id} not found.")
    strategy = res[0].campaign.bidding_strategy_type.name
    if strategy != "MANUAL_CPC":
        raise ValueError(f"Campaign '{res[0].campaign.name}' uses {strategy}; ad group bids are ignored there. Use step_bid_threshold on the campaign CPC ceiling instead.")

    operation = client.get_type("AdGroupOperation")
    ag = operation.update
    ag.resource_name = ad_group_service.ad_group_path(customer_id, req.ad_group_id)
    ag.cpc_bid_micros = eur_to_micros(req.new_bid_eur)
    operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, ag._pb))

    ad_group_service.mutate_ad_groups(customer_id=customer_id, operations=[operation])
    return f"AdGroup {req.ad_group_id} base CPC bid updated to {req.new_bid_eur:.2f} EUR."

@handle_google_ads_errors
def get_ad_group_keywords(ad_group_id: str, customer_id: str = CUSTOMER_ID) -> List[AdGroupKeywordRecord]:
    """
    Lists biddable keywords and their Google Quality Scores (1-10) for a specific Ad Group.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    
    query = f"""
        SELECT
            ad_group.id,
            ad_group_criterion.criterion_id,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group_criterion.cpc_bid_micros,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.quality_info.search_predicted_ctr,
            ad_group_criterion.quality_info.creative_quality_score,
            ad_group_criterion.quality_info.post_click_quality_score
        FROM ad_group_criterion
        WHERE ad_group.id = {ad_group_id}
          AND ad_group_criterion.type = 'KEYWORD'
          AND ad_group_criterion.status != 'REMOVED'
        ORDER BY ad_group_criterion.keyword.text ASC
    """
    response = ga_service.search(customer_id=customer_id, query=query)
    results = []
    
    for row in response:
        agc = row.ad_group_criterion
        qi = agc.quality_info
        qs = qi.quality_score if qi and qi.quality_score else None
        pred_ctr = qi.search_predicted_ctr.name if qi and qi.search_predicted_ctr else None
        ad_rel = qi.creative_quality_score.name if qi and qi.creative_quality_score else None
        land_exp = qi.post_click_quality_score.name if qi and qi.post_click_quality_score else None
        bid_eur = round(agc.cpc_bid_micros / 1_000_000.0, 2) if agc.cpc_bid_micros else None
        
        results.append(AdGroupKeywordRecord(
            criterion_id=str(agc.criterion_id),
            ad_group_id=str(row.ad_group.id),
            keyword_text=agc.keyword.text,
            match_type=agc.keyword.match_type.name if hasattr(agc.keyword.match_type, "name") else str(agc.keyword.match_type),
            status=agc.status.name if hasattr(agc.status, "name") else str(agc.status),
            cpc_bid_eur=bid_eur,
            quality_score=qs,
            search_predicted_ctr=pred_ctr,
            creative_quality_score=ad_rel,
            post_click_quality_score=land_exp
        ))
    return results

@handle_google_ads_errors
def update_keyword_status(req: KeywordStatusUpdateRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Pauses or enables one positive keyword in an ad group. Refuses negatives and non-keyword criteria.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    agc_service = client.get_service("AdGroupCriterionService")

    query = f"""
        SELECT ad_group.name, ad_group_criterion.type, ad_group_criterion.negative,
               ad_group_criterion.status, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type
        FROM ad_group_criterion
        WHERE ad_group.id = {req.ad_group_id} AND ad_group_criterion.criterion_id = {req.criterion_id}
        LIMIT 1
    """
    res = list(ga_service.search(customer_id=customer_id, query=query))
    if not res:
        raise ValueError(f"Criterion {req.criterion_id} not found in AdGroup {req.ad_group_id}.")
    agc = res[0].ad_group_criterion
    if agc.type_.name != "KEYWORD" or agc.negative:
        raise ValueError(f"Criterion {req.criterion_id} is not a positive keyword ({agc.type_.name}, negative={agc.negative}).")

    old_status = agc.status.name
    new_status = "ENABLED" if req.enabled else "PAUSED"
    label = f"'{agc.keyword.text}' [{agc.keyword.match_type.name}] in '{res[0].ad_group.name}'"
    if old_status == new_status:
        return f"Keyword {label} is already {new_status}; nothing changed."

    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = agc_service.ad_group_criterion_path(customer_id, req.ad_group_id, req.criterion_id)
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED if req.enabled else client.enums.AdGroupCriterionStatusEnum.PAUSED
    operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, criterion._pb))

    agc_service.mutate_ad_group_criteria(customer_id=customer_id, operations=[operation])
    return f"Keyword {label}: {old_status} -> {new_status}."

def _group_refusal(code: str, message: str, ad_group_id: str) -> ServiceResult:
    """A refusal of the shield (not a bug): typed, so the health report does not count it as a crash."""
    return ServiceResult(
        success=False,
        error=ApiError(code=code, message=message, trigger=ad_group_id, field_path=["ad_group_id"]),
        telemetry=Telemetry(latency_ms=0.0))

@handle_google_ads_errors
def update_ad_group_status(req: AdGroupStatusUpdateRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Pauses or enables one ad group (the whole group: its keywords and ads keep their own status).
    Safeguards:
    - never pauses the last ENABLED ad group of a campaign;
    - never enables a group without at least one ENABLED positive keyword and one ENABLED Responsive Search Ad
      (a half-built group spends budget: a group once created enabled and finished only later did exactly that);
    - warns (does not refuse) when the group has no images of its own (field AD_IMAGE at ad-group level).
    The update mask names `status` explicitly and the group is read back: the answer is what Google holds, not what was asked.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    ag_service = client.get_service("AdGroupService")

    rows = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT ad_group.id, ad_group.name, ad_group.status, campaign.id, campaign.name
        FROM ad_group
        WHERE ad_group.id = {req.ad_group_id} AND ad_group.status != 'REMOVED'
        LIMIT 1
    """))
    if not rows:
        return _group_refusal("AD_GROUP_NOT_FOUND", f"Ad group {req.ad_group_id} does not exist in this account (or was removed).", req.ad_group_id)

    group, campaign = rows[0].ad_group, rows[0].campaign
    old_status = group.status.name
    new_status = "ENABLED" if req.enabled else "PAUSED"
    label = f"'{group.name}' ({req.ad_group_id}) in campaign '{campaign.name}'"
    if old_status == new_status:
        return f"Ad group {label} is already {new_status}; nothing changed."

    warnings = []
    if not req.enabled:
        enabled_groups = list(ga_service.search(customer_id=customer_id, query=f"""
            SELECT ad_group.id
            FROM ad_group
            WHERE campaign.id = {campaign.id} AND ad_group.status = 'ENABLED'
        """))
        if len(enabled_groups) <= 1:
            return _group_refusal(
                "LAST_ENABLED_AD_GROUP",
                f"SAFEGUARD: ad group {label} is the only ENABLED ad group of its campaign; pausing it would leave the campaign without ads. "
                "Pause the campaign itself (toggle_campaign_status) if that is the intent.", req.ad_group_id)
    else:
        # Google refuses a WHERE on ad_group.id unless ad_group.id is also selected (INVALID query on the live account 31.10.2026)
        keywords = list(ga_service.search(customer_id=customer_id, query=f"""
            SELECT ad_group.id, ad_group_criterion.criterion_id
            FROM ad_group_criterion
            WHERE ad_group.id = {req.ad_group_id} AND ad_group_criterion.type = 'KEYWORD'
              AND ad_group_criterion.negative = FALSE AND ad_group_criterion.status = 'ENABLED'
        """))
        ads = list(ga_service.search(customer_id=customer_id, query=f"""
            SELECT ad_group.id, ad_group_ad.ad.id
            FROM ad_group_ad
            WHERE ad_group.id = {req.ad_group_id} AND ad_group_ad.status = 'ENABLED'
              AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
        """))
        missing = []
        if not keywords:
            missing.append("an ENABLED positive keyword (add_ad_group_keywords / set_keyword_status)")
        if not ads:
            missing.append("an ENABLED Responsive Search Ad (deploy_golden_rsa / set_ad_status)")
        if missing:
            return _group_refusal(
                "AD_GROUP_NOT_READY",
                f"Ad group {label} stays {old_status}: it has no " + " and no ".join(missing) + ".", req.ad_group_id)
        images = list(ga_service.search(customer_id=customer_id, query=f"""
            SELECT ad_group.id, ad_group_asset.asset
            FROM ad_group_asset
            WHERE ad_group.id = {req.ad_group_id} AND ad_group_asset.field_type = 'AD_IMAGE'
              AND ad_group_asset.status != 'REMOVED'
        """))
        if not images:
            warnings.append("The group has no images of its own (AD_IMAGE at ad-group level): it will show the campaign's images.")

    operation = client.get_type("AdGroupOperation")
    update = operation.update
    update.resource_name = ag_service.ad_group_path(customer_id, req.ad_group_id)
    update.status = client.enums.AdGroupStatusEnum.ENABLED if req.enabled else client.enums.AdGroupStatusEnum.PAUSED
    # Named explicitly: a mask built from the object leaves out a field set to its default (lesson of 30.10.2026)
    operation.update_mask.paths.extend(["status"])
    ag_service.mutate_ad_groups(customer_id=customer_id, operations=[operation])

    after = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT ad_group.status FROM ad_group WHERE ad_group.id = {req.ad_group_id} LIMIT 1
    """))
    actual = after[0].ad_group.status.name if after else "UNKNOWN"
    if actual != new_status:
        return _group_refusal(
            "NOT_APPLIED", f"Google accepted the request but ad group {label} is {actual}, not {new_status}.", req.ad_group_id)
    return f"Ad group {label}: {old_status} -> {actual}." + "".join(f" WARNING: {w}" for w in warnings)

@handle_google_ads_errors
def create_ad_group(req: CreateAdGroupRequest, customer_id: str = CUSTOMER_ID) -> CreateAdGroupResponse:
    """
    Creates a new Ad Group in the specified campaign and optionally adds positive exact/phrase keywords.
    """
    client = get_ads_client()
    campaign_service = client.get_service("CampaignService")
    campaign_resource_name = campaign_service.campaign_path(customer_id, req.campaign_id)
    
    ag_service = client.get_service("AdGroupService")
    ag_op = client.get_type("AdGroupOperation")
    ag = ag_op.create
    ag.name = req.name
    ag.campaign = campaign_resource_name
    ag.status = client.enums.AdGroupStatusEnum.ENABLED if req.status == "ENABLED" else client.enums.AdGroupStatusEnum.PAUSED
    ag.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    
    ag_res = ag_service.mutate_ad_groups(customer_id=customer_id, operations=[ag_op])
    ad_group_resource_name = ag_res.results[0].resource_name
    ad_group_id = ad_group_resource_name.split("/")[-1]
    
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
            
    return CreateAdGroupResponse(
        ad_group_id=ad_group_id,
        ad_group_resource_name=ad_group_resource_name,
        campaign_id=req.campaign_id,
        name=req.name,
        keywords_count=kw_count
    )

@handle_google_ads_errors
def add_ad_group_keywords(req: AddKeywordsRequest, customer_id: str = CUSTOMER_ID) -> AddKeywordsResponse:
    """
    Adds positive keywords (Exact [kw] or Phrase "kw") to an existing Ad Group.
    """
    client = get_ads_client()
    ad_group_service = client.get_service("AdGroupService")
    ad_group_resource_name = ad_group_service.ad_group_path(customer_id, req.ad_group_id)
    
    agc_service = client.get_service("AdGroupCriterionService")
    agc_ops = []
    added = []
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
        added.append(kw_clean)
        
    if agc_ops:
        agc_service.mutate_ad_group_criteria(customer_id=customer_id, operations=agc_ops)
        
    return AddKeywordsResponse(
        ad_group_id=req.ad_group_id,
        added_count=len(added),
        keywords=added
    )

