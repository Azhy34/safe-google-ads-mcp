import sys
from pathlib import Path
from typing import List
from google.api_core import protobuf_helpers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client
from core.config import CUSTOMER_ID
from shield.error_shield import ServiceResult, handle_google_ads_errors
from shield.models_ad import (
    GoogleRSAAdRecord, DeployGoldenRSARequest, AdStatusUpdateRequest,
    HeadlineAsset, DescriptionAsset
)

@handle_google_ads_errors
def list_campaign_ads(campaign_id: str, customer_id: str = CUSTOMER_ID) -> List[GoogleRSAAdRecord]:
    """
    Fetches all Responsive Search Ads in a campaign with headline/description arrays and approval status.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    
    query = f"""
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad_group,
            ad_group_ad.status,
            ad_group_ad.ad.final_urls,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions,
            ad_group_ad.policy_summary.approval_status
        FROM ad_group_ad
        WHERE campaign.id = {campaign_id}
          AND ad_group_ad.status != 'REMOVED'
          AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
    """
    response = ga_service.search(customer_id=customer_id, query=query)
    
    results = []
    for row in response:
        aga = row.ad_group_ad
        ad = aga.ad
        rsa = ad.responsive_search_ad
        
        headlines = []
        for h in rsa.headlines:
            pinned = h.pinned_field.name if hasattr(h.pinned_field, "name") and h.pinned_field else None
            perf = h.asset_performance_label.name if hasattr(h.asset_performance_label, "name") and h.asset_performance_label else None
            headlines.append(HeadlineAsset(
                text=h.text,
                pinned_field=pinned,
                asset_performance_label=perf
            ))
            
        descriptions = []
        for d in rsa.descriptions:
            pinned = d.pinned_field.name if hasattr(d.pinned_field, "name") and d.pinned_field else None
            perf = d.asset_performance_label.name if hasattr(d.asset_performance_label, "name") and d.asset_performance_label else None
            descriptions.append(DescriptionAsset(
                text=d.text,
                pinned_field=pinned,
                asset_performance_label=perf
            ))
            
        approval = aga.policy_summary.approval_status.name if hasattr(aga.policy_summary.approval_status, "name") else str(aga.policy_summary.approval_status)
        ag_id = aga.ad_group.split("/")[-1] if hasattr(aga, "ad_group") else ""
        
        results.append(GoogleRSAAdRecord(
            id=str(ad.id),
            resource_name=aga.resource_name,
            ad_group_id=ag_id,
            status=aga.status.name if hasattr(aga.status, "name") else str(aga.status),
            headlines=headlines,
            descriptions=descriptions,
            final_urls=list(ad.final_urls),
            approval_status=approval
        ))
    return results

@handle_google_ads_errors
def deploy_golden_rsa(req: DeployGoldenRSARequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Deploys or updates a Golden 15/4 RSA in-place with Headline 1 DKI pinning and URL compliance.
    - If `req.ad_id` is provided, or if the ad group already has an ENABLED RSA, updates that existing RSA
      in-place via AdService.mutate_ads (preserving its ad_id and history without creating duplicates)
      and pauses any extra enabled RSAs so only 1 clean RSA stays active.
    - Only creates a new AdGroupAd if the ad group currently has 0 enabled RSAs (and `req.ad_id` is None).
    """
    client = get_ads_client()
    aga_service = client.get_service("AdGroupAdService")
    ad_service = client.get_service("AdService")
    ga_service = client.get_service("GoogleAdsService")

    query_existing = f"""
        SELECT ad_group_ad.resource_name, ad_group_ad.ad.id, ad_group_ad.status
        FROM ad_group_ad
        WHERE ad_group.id = {req.ad_group_id}
          AND ad_group_ad.status != 'REMOVED'
          AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
        ORDER BY ad_group_ad.ad.id ASC
    """
    all_rows = list(ga_service.search(customer_id=customer_id, query=query_existing))
    enabled_rows = [r for r in all_rows if r.ad_group_ad.status.name == "ENABLED"]

    target_ad_id = req.ad_id
    if target_ad_id:
        matching = [r for r in all_rows if str(r.ad_group_ad.ad.id) == target_ad_id]
        if not matching:
            raise ValueError(f"RSA Ad {target_ad_id} not found in AdGroup {req.ad_group_id}.")
    elif enabled_rows:
        # Default to updating the newest enabled RSA in-place instead of creating a duplicate
        target_ad_id = str(enabled_rows[-1].ad_group_ad.ad.id)

    if target_ad_id:
        ad_op = client.get_type("AdOperation")
        ad = ad_op.update
        ad.resource_name = ad_service.ad_path(customer_id, target_ad_id)
        ad.final_urls.append(req.final_url)

        for i, text in enumerate(req.headlines):
            h = client.get_type("AdTextAsset")
            h.text = text
            if i == 0:
                h.pinned_field = client.enums.ServedAssetFieldTypeEnum.HEADLINE_1
            ad.responsive_search_ad.headlines.append(h)

        for text in req.descriptions:
            d = client.get_type("AdTextAsset")
            d.text = text
            ad.responsive_search_ad.descriptions.append(d)

        if req.path1:
            ad.responsive_search_ad.path1 = req.path1
        if req.path2:
            ad.responsive_search_ad.path2 = req.path2

        ad_op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, ad._pb))
        ad_service.mutate_ads(customer_id=customer_id, operations=[ad_op])

        paused = []
        pause_ops = []
        for r in enabled_rows:
            rid = str(r.ad_group_ad.ad.id)
            if rid != target_ad_id:
                op = client.get_type("AdGroupAdOperation")
                ad_to_pause = op.update
                ad_to_pause.resource_name = r.ad_group_ad.resource_name
                ad_to_pause.status = client.enums.AdGroupAdStatusEnum.PAUSED
                op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, ad_to_pause._pb))
                pause_ops.append(op)
                paused.append(rid)
        if pause_ops:
            aga_service.mutate_ad_group_ads(customer_id=customer_id, operations=pause_ops)

        paused_note = f" Paused extra enabled ad(s): {', '.join(paused)}." if paused else ""
        return f"Successfully updated existing Golden RSA {target_ad_id} in-place in AdGroup {req.ad_group_id} (15/4 Matrix + DKI).{paused_note}"

    # No existing enabled RSA in the ad group -> create a new one
    create_operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = create_operation.create
    ad_group_ad.ad_group = client.get_service("AdGroupService").ad_group_path(customer_id, req.ad_group_id)
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED

    rsa_ad = ad_group_ad.ad
    rsa_ad.final_urls.append(req.final_url)

    for i, text in enumerate(req.headlines):
        h = client.get_type("AdTextAsset")
        h.text = text
        if i == 0:
            h.pinned_field = client.enums.ServedAssetFieldTypeEnum.HEADLINE_1
        rsa_ad.responsive_search_ad.headlines.append(h)

    for text in req.descriptions:
        d = client.get_type("AdTextAsset")
        d.text = text
        rsa_ad.responsive_search_ad.descriptions.append(d)

    if req.path1:
        rsa_ad.responsive_search_ad.path1 = req.path1
    if req.path2:
        rsa_ad.responsive_search_ad.path2 = req.path2

    response = aga_service.mutate_ad_group_ads(customer_id=customer_id, operations=[create_operation])
    new_ad_resource = response.results[-1].resource_name
    return f"Successfully created new Golden RSA {new_ad_resource} in AdGroup {req.ad_group_id} (15/4 Matrix + DKI)."

@handle_google_ads_errors
def update_ad_status(req: AdStatusUpdateRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Pauses or enables a specific Responsive Search Ad (RSA) in an ad group.
    Safeguard: Refuses to pause the last ENABLED RSA in an ad group so the ad group is never left with zero active ads.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    aga_service = client.get_service("AdGroupAdService")

    query = f"""
        SELECT
            ad_group.name,
            ad_group_ad.resource_name,
            ad_group_ad.ad.id,
            ad_group_ad.status,
            ad_group_ad.ad.type
        FROM ad_group_ad
        WHERE ad_group.id = {req.ad_group_id}
          AND ad_group_ad.status != 'REMOVED'
          AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
    """
    rows = list(ga_service.search(customer_id=customer_id, query=query))
    target_row = None
    enabled_count = 0
    for row in rows:
        if row.ad_group_ad.status.name == "ENABLED":
            enabled_count += 1
        if str(row.ad_group_ad.ad.id) == req.ad_id:
            target_row = row

    if not target_row:
        raise ValueError(f"RSA Ad {req.ad_id} not found in AdGroup {req.ad_group_id}.")

    ag_name = target_row.ad_group.name
    old_status = target_row.ad_group_ad.status.name
    new_status = "ENABLED" if req.enabled else "PAUSED"
    if old_status == new_status:
        return f"RSA {req.ad_id} in '{ag_name}' is already {new_status}; nothing changed."

    if not req.enabled and old_status == "ENABLED" and enabled_count <= 1:
        raise ValueError(
            f"SAFEGUARD: Cannot pause RSA {req.ad_id} because it is the only ENABLED RSA in AdGroup '{ag_name}' ({req.ad_group_id}). Deploy a replacement RSA first."
        )

    op = client.get_type("AdGroupAdOperation")
    ad_to_update = op.update
    ad_to_update.resource_name = aga_service.ad_group_ad_path(customer_id, req.ad_group_id, req.ad_id)
    ad_to_update.status = client.enums.AdGroupAdStatusEnum.ENABLED if req.enabled else client.enums.AdGroupAdStatusEnum.PAUSED
    op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, ad_to_update._pb))

    aga_service.mutate_ad_group_ads(customer_id=customer_id, operations=[op])
    return f"RSA {req.ad_id} in '{ag_name}' ({req.ad_group_id}): {old_status} -> {new_status}."
