import json
import sys
from pathlib import Path
from typing import List, Optional
from datetime import date, timedelta
from google.api_core import protobuf_helpers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client
from core.config import CUSTOMER_ID
from shield.error_shield import ApiError, ServiceResult, Telemetry, handle_google_ads_errors
from shield.models_asset import (
    GoogleAssetRecord, ImageAssetData, SitelinkAssetData,
    StructuredSnippetAssetData, CalloutAssetData, PromotionAssetData, PriceAssetData,
    AssetAssociationRecord, AssetPerformanceRecord, CampaignAssetDiversity,
    LinkAssetRequest, UnlinkAssetRequest, CreatePriceAssetRequest,
    SyncPriceExtensionRequest, CreateSitelinkAssetsRequest,
    CreateCalloutAssetsRequest, CreateStructuredSnippetAssetRequest,
)

@handle_google_ads_errors
def list_account_assets(asset_type: str = None, customer_id: str = CUSTOMER_ID) -> List[GoogleAssetRecord]:
    """
    Fetches all base assets in the account (Images, Sitelinks, Structured Snippets, Promotions, Prices).
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    
    where_clause = ""
    if asset_type:
        where_clause = f"WHERE asset.type = '{asset_type.upper()}'"
        
    query = f"""
        SELECT
            asset.id,
            asset.name,
            asset.type,
            asset.image_asset.full_size.url,
            asset.image_asset.file_size,
            asset.sitelink_asset.link_text,
            asset.sitelink_asset.description1,
            asset.sitelink_asset.description2,
            asset.structured_snippet_asset.header,
            asset.structured_snippet_asset.values,
            asset.callout_asset.callout_text,
            asset.promotion_asset.promotion_target,
            asset.promotion_asset.discount_modifier,
            asset.promotion_asset.percent_off
        FROM asset
        {where_clause}
        LIMIT 100
    """
    response = ga_service.search(customer_id=customer_id, query=query)
    
    results = []
    for row in response:
        a = row.asset
        type_str = a.type_.name if hasattr(a.type_, "name") else str(a.type_)
        
        img_data = None
        if a.image_asset and a.image_asset.full_size and a.image_asset.full_size.url:
            img_data = ImageAssetData(
                url=a.image_asset.full_size.url,
                file_size=a.image_asset.file_size or None
            )
            
        sitelink_data = None
        if a.sitelink_asset and a.sitelink_asset.link_text:
            sitelink_data = SitelinkAssetData(
                link_text=a.sitelink_asset.link_text,
                description1=a.sitelink_asset.description1 or None,
                description2=a.sitelink_asset.description2 or None
            )
            
        snippet_data = None
        if a.structured_snippet_asset and a.structured_snippet_asset.header:
            snippet_data = StructuredSnippetAssetData(
                header=a.structured_snippet_asset.header,
                values=list(a.structured_snippet_asset.values)
            )
            
        callout_data = None
        if a.callout_asset and a.callout_asset.callout_text:
            callout_data = CalloutAssetData(callout_text=a.callout_asset.callout_text)
            
        promo_data = None
        if a.promotion_asset and a.promotion_asset.promotion_target:
            promo_data = PromotionAssetData(
                promotion_target=a.promotion_asset.promotion_target,
                discount_modifier=a.promotion_asset.discount_modifier.name if hasattr(a.promotion_asset.discount_modifier, "name") else None,
                percent_off=a.promotion_asset.percent_off or None
            )
            
        results.append(GoogleAssetRecord(
            id=str(a.id),
            resource_name=a.resource_name,
            name=a.name or None,
            type=type_str,
            image_data=img_data,
            sitelink_data=sitelink_data,
            snippet_data=snippet_data,
            callout_data=callout_data,
            promotion_data=promo_data
        ))
    return results

# --- TAB 1: СВЯЗИ (ASSOCIATIONS) ---

@handle_google_ads_errors
def get_assets_associations(
    level: Optional[str] = None, 
    field_type: Optional[str] = None, 
    customer_id: str = CUSTOMER_ID
) -> List[AssetAssociationRecord]:
    """
    Reproduces Google Ads UI Tab 'Связи' (Associations).
    Retrieves asset links across Customer (Account), Campaign, and Ad Group levels
    with full text preview and image CDN URLs.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    results = []
    
    # 1. Customer Level (Account-wide, e.g. Business Logo, Price)
    if not level or level.upper() == "CUSTOMER":
        q_cust = """
            SELECT
                asset.id,
                asset.name,
                asset.type,
                asset.text_asset.text,
                asset.image_asset.full_size.url,
                customer_asset.field_type,
                customer_asset.status
            FROM customer_asset
            WHERE customer_asset.status != 'REMOVED'
        """
        if field_type:
            q_cust += f" AND customer_asset.field_type = '{field_type.upper()}'"
        for r in ga_service.search(customer_id=customer_id, query=q_cust):
            f_type = r.customer_asset.field_type.name if hasattr(r.customer_asset.field_type, "name") else str(r.customer_asset.field_type)
            a = r.asset
            txt = a.text_asset.text if a.text_asset else (a.name or None)
            img_url = a.image_asset.full_size.url if a.image_asset and a.image_asset.full_size else None
            results.append(AssetAssociationRecord(
                level="CUSTOMER",
                entity_id=customer_id,
                entity_name="Account Level",
                asset_id=str(a.id),
                asset_name=a.name or None,
                asset_text=txt,
                asset_image_url=img_url,
                field_type=f_type,
                status=r.customer_asset.status.name if hasattr(r.customer_asset.status, "name") else str(r.customer_asset.status)
            ))

    # 2. Campaign Level (Sitelinks, Callouts, Snippets, Promotions)
    if not level or level.upper() == "CAMPAIGN":
        q_camp = """
            SELECT
                campaign.id,
                campaign.name,
                asset.id,
                asset.name,
                asset.type,
                asset.text_asset.text,
                asset.sitelink_asset.link_text,
                asset.callout_asset.callout_text,
                asset.structured_snippet_asset.header,
                asset.image_asset.full_size.url,
                campaign_asset.field_type,
                campaign_asset.status
            FROM campaign_asset
            WHERE campaign_asset.status != 'REMOVED'
        """
        if field_type:
            q_camp += f" AND campaign_asset.field_type = '{field_type.upper()}'"
        for r in ga_service.search(customer_id=customer_id, query=q_camp):
            f_type = r.campaign_asset.field_type.name if hasattr(r.campaign_asset.field_type, "name") else str(r.campaign_asset.field_type)
            a = r.asset
            txt = (a.text_asset.text if a.text_asset else None) or \
                  (a.sitelink_asset.link_text if a.sitelink_asset else None) or \
                  (a.callout_asset.callout_text if a.callout_asset else None) or \
                  (a.structured_snippet_asset.header if a.structured_snippet_asset else None) or \
                  a.name
            img_url = a.image_asset.full_size.url if a.image_asset and a.image_asset.full_size else None
            results.append(AssetAssociationRecord(
                level="CAMPAIGN",
                entity_id=str(r.campaign.id),
                entity_name=r.campaign.name,
                asset_id=str(a.id),
                asset_name=a.name or None,
                asset_text=txt,
                asset_image_url=img_url,
                field_type=f_type,
                status=r.campaign_asset.status.name if hasattr(r.campaign_asset.status, "name") else str(r.campaign_asset.status)
            ))

    # 3. Ad Group Level (Image assets, group sitelinks)
    if not level or level.upper() == "AD_GROUP":
        q_ag = """
            SELECT
                ad_group.id,
                ad_group.name,
                asset.id,
                asset.name,
                asset.type,
                asset.text_asset.text,
                asset.sitelink_asset.link_text,
                asset.image_asset.full_size.url,
                ad_group_asset.field_type,
                ad_group_asset.status
            FROM ad_group_asset
            WHERE ad_group_asset.status != 'REMOVED'
        """
        if field_type:
            q_ag += f" AND ad_group_asset.field_type = '{field_type.upper()}'"
        for r in ga_service.search(customer_id=customer_id, query=q_ag):
            f_type = r.ad_group_asset.field_type.name if hasattr(r.ad_group_asset.field_type, "name") else str(r.ad_group_asset.field_type)
            a = r.asset
            txt = (a.text_asset.text if a.text_asset else None) or \
                  (a.sitelink_asset.link_text if a.sitelink_asset else None) or \
                  a.name
            img_url = a.image_asset.full_size.url if a.image_asset and a.image_asset.full_size else None
            results.append(AssetAssociationRecord(
                level="AD_GROUP",
                entity_id=str(r.ad_group.id),
                entity_name=r.ad_group.name,
                asset_id=str(a.id),
                asset_name=a.name or None,
                asset_text=txt,
                asset_image_url=img_url,
                field_type=f_type,
                status=r.ad_group_asset.status.name if hasattr(r.ad_group_asset.status, "name") else str(r.ad_group_asset.status)
            ))

    return results

# --- TAB 2: ЭФФЕКТИВНОСТЬ (PERFORMANCE) ---

@handle_google_ads_errors
def get_assets_performance(
    days: int = 30,
    field_type: Optional[str] = None,
    customer_id: str = CUSTOMER_ID
) -> List[AssetPerformanceRecord]:
    """
    Reproduces Google Ads UI Tab 'Эффективность' (Asset Performance).
    Retrieves Impressions, Clicks, CTR %, Cost (EUR), and Google AI Performance Label
    across both In-Ad Assets (ad_group_ad_asset_view) and Extensions (campaign_asset, customer_asset).
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    
    start_date = (date.today() - timedelta(days=days)).isoformat()
    end_date = date.today().isoformat()
    results: List[AssetPerformanceRecord] = []
    
    # 1. In-Ad Assets (Headlines, Descriptions, Images, Logos, Videos)
    where_ad_extra = ""
    if field_type:
        where_ad_extra = f" AND ad_group_ad_asset_view.field_type = '{field_type.upper()}'"
        
    q_ad = f"""
        SELECT
            campaign.name,
            ad_group.name,
            asset.id,
            asset.name,
            asset.type,
            asset.text_asset.text,
            asset.image_asset.full_size.url,
            ad_group_ad_asset_view.field_type,
            ad_group_ad_asset_view.performance_label,
            metrics.impressions,
            metrics.clicks,
            metrics.ctr,
            metrics.cost_micros
        FROM ad_group_ad_asset_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND metrics.impressions > 0
          {where_ad_extra}
        ORDER BY metrics.impressions DESC
        LIMIT 60
    """
    for r in ga_service.search(customer_id=customer_id, query=q_ad):
        v = r.ad_group_ad_asset_view
        m = r.metrics
        a = r.asset
        f_type = v.field_type.name if hasattr(v.field_type, "name") else str(v.field_type)
        perf_label = v.performance_label.name if hasattr(v.performance_label, "name") else str(v.performance_label)
        txt = a.text_asset.text if a.text_asset else (a.name or None)
        img_url = a.image_asset.full_size.url if a.image_asset and a.image_asset.full_size else None
        
        results.append(AssetPerformanceRecord(
            asset_id=str(a.id),
            asset_name=a.name or None,
            asset_text=txt,
            asset_image_url=img_url,
            field_type=f_type,
            level="AD_ASSET",
            campaign_name=r.campaign.name,
            ad_group_name=r.ad_group.name,
            impressions=m.impressions or 0,
            clicks=m.clicks or 0,
            ctr_percent=round((m.ctr or 0.0) * 100, 2),
            cost_eur=round((m.cost_micros or 0) / 1_000_000.0, 2),
            performance_label=perf_label
        ))

    # 2. Campaign Level Assets (Sitelinks, Callouts, Snippets, Promotions)
    where_camp_extra = ""
    if field_type:
        where_camp_extra = f" AND campaign_asset.field_type = '{field_type.upper()}'"
        
    q_camp = f"""
        SELECT
            campaign.name,
            asset.id,
            asset.name,
            asset.type,
            asset.text_asset.text,
            asset.sitelink_asset.link_text,
            asset.callout_asset.callout_text,
            asset.structured_snippet_asset.header,
            asset.image_asset.full_size.url,
            campaign_asset.field_type,
            metrics.impressions,
            metrics.clicks,
            metrics.ctr,
            metrics.cost_micros
        FROM campaign_asset
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND metrics.impressions > 0
          {where_camp_extra}
        ORDER BY metrics.impressions DESC
        LIMIT 40
    """
    for r in ga_service.search(customer_id=customer_id, query=q_camp):
        ca = r.campaign_asset
        m = r.metrics
        a = r.asset
        f_type = ca.field_type.name if hasattr(ca.field_type, "name") else str(ca.field_type)
        txt = (a.text_asset.text if a.text_asset else None) or \
              (a.sitelink_asset.link_text if a.sitelink_asset else None) or \
              (a.callout_asset.callout_text if a.callout_asset else None) or \
              (a.structured_snippet_asset.header if a.structured_snippet_asset else None) or \
              a.name
        img_url = a.image_asset.full_size.url if a.image_asset and a.image_asset.full_size else None
        ctr = round((m.ctr or 0.0) * 100, 2)
        clicks = m.clicks or 0
        # Google exposes no performance_label for extensions; this is our own CTR heuristic
        perf = "HEURISTIC_BEST" if ctr > 5.0 and clicks > 10 else ("HEURISTIC_GOOD" if ctr > 2.0 else "HEURISTIC_LEARNING")
        
        results.append(AssetPerformanceRecord(
            asset_id=str(a.id),
            asset_name=a.name or None,
            asset_text=txt,
            asset_image_url=img_url,
            field_type=f_type,
            level="CAMPAIGN_ASSET",
            campaign_name=r.campaign.name,
            ad_group_name=None,
            impressions=m.impressions or 0,
            clicks=clicks,
            ctr_percent=ctr,
            cost_eur=round((m.cost_micros or 0) / 1_000_000.0, 2),
            performance_label=perf
        ))

    # 3. Customer Level Assets (Price, Business Logo, Business Name)
    where_cust_extra = ""
    if field_type:
        where_cust_extra = f" AND customer_asset.field_type = '{field_type.upper()}'"
        
    q_cust = f"""
        SELECT
            asset.id,
            asset.name,
            asset.type,
            customer_asset.field_type,
            metrics.impressions,
            metrics.clicks,
            metrics.ctr,
            metrics.cost_micros
        FROM customer_asset
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND metrics.impressions > 0
          {where_cust_extra}
        LIMIT 10
    """
    for r in ga_service.search(customer_id=customer_id, query=q_cust):
        ca = r.customer_asset
        m = r.metrics
        a = r.asset
        f_type = ca.field_type.name if hasattr(ca.field_type, "name") else str(ca.field_type)
        ctr = round((m.ctr or 0.0) * 100, 2)
        clicks = m.clicks or 0
        perf = "HEURISTIC_BEST" if ctr > 5.0 and clicks > 10 else ("HEURISTIC_GOOD" if ctr > 1.0 else "HEURISTIC_LEARNING")
        
        results.append(AssetPerformanceRecord(
            asset_id=str(a.id),
            asset_name=a.name or None,
            asset_text=a.name or "Account Asset",
            asset_image_url=None,
            field_type=f_type,
            level="CUSTOMER_ASSET",
            campaign_name="All Campaigns (Account Level)",
            ad_group_name=None,
            impressions=m.impressions or 0,
            clicks=clicks,
            ctr_percent=ctr,
            cost_eur=round((m.cost_micros or 0) / 1_000_000.0, 2),
            performance_label=perf
        ))

    results.sort(key=lambda x: x.impressions, reverse=True)
    return results

# --- TAB 3: РАЗНООБРАЗИЕ ОБЪЕКТОВ (ASSET DIVERSITY & HEALTH) ---

@handle_google_ads_errors
def get_assets_diversity(customer_id: str = CUSTOMER_ID) -> List[CampaignAssetDiversity]:
    """
    Reproduces Google Ads UI Tab 'Разнообразие объектов'.
    Audits active campaigns to ensure complete asset extension variety:
    Images (min 4), Sitelinks (min 4), Snippets, Callouts, Promotions, Price, Logo.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    
    # 1. Fetch active search campaigns
    q_camps = """
        SELECT campaign.id, campaign.name 
        FROM campaign 
        WHERE campaign.status = 'ENABLED' 
          AND campaign.advertising_channel_type = 'SEARCH'
    """
    campaigns = {str(r.campaign.id): r.campaign.name for r in ga_service.search(customer_id=customer_id, query=q_camps)}
    
    # 2. Fetch customer-level assets
    q_cust = "SELECT customer_asset.field_type FROM customer_asset WHERE customer_asset.status != 'REMOVED'"
    cust_assets = set()
    for r in ga_service.search(customer_id=customer_id, query=q_cust):
        cust_assets.add(r.customer_asset.field_type.name if hasattr(r.customer_asset.field_type, "name") else str(r.customer_asset.field_type))
        
    has_cust_price = "PRICE" in cust_assets
    has_cust_logo = "BUSINESS_LOGO" in cust_assets
    has_cust_name = "BUSINESS_NAME" in cust_assets

    # 3. Fetch campaign-level asset links
    q_camp_assets = """
        SELECT campaign.id, campaign_asset.field_type
        FROM campaign_asset
        WHERE campaign_asset.status != 'REMOVED'
    """
    camp_asset_map = {}
    for r in ga_service.search(customer_id=customer_id, query=q_camp_assets):
        cid = str(r.campaign.id)
        if cid not in camp_asset_map:
            camp_asset_map[cid] = []
        camp_asset_map[cid].append(r.campaign_asset.field_type.name if hasattr(r.campaign_asset.field_type, "name") else str(r.campaign_asset.field_type))

    # 4. Fetch ad-group-level image and sitelink counts
    q_ag_assets = """
        SELECT campaign.id, ad_group_asset.field_type
        FROM ad_group_asset
        WHERE ad_group_asset.status != 'REMOVED'
    """
    ag_asset_map = {}
    for r in ga_service.search(customer_id=customer_id, query=q_ag_assets):
        cid = str(r.campaign.id)
        if cid not in ag_asset_map:
            ag_asset_map[cid] = []
        ag_asset_map[cid].append(r.ad_group_asset.field_type.name if hasattr(r.ad_group_asset.field_type, "name") else str(r.ad_group_asset.field_type))

    summaries = []
    for cid, cname in campaigns.items():
        c_types = camp_asset_map.get(cid, [])
        ag_types = ag_asset_map.get(cid, [])
        
        sitelinks_count = c_types.count("SITELINK") + ag_types.count("SITELINK")
        images_count = ag_types.count("IMAGE") + c_types.count("IMAGE")
        has_snippets = "STRUCTURED_SNIPPET" in c_types
        has_callouts = "CALLOUT" in c_types
        has_promo = "PROMOTION" in c_types
        
        missing = []
        score = 0
        if sitelinks_count >= 4:
            score += 25
        elif sitelinks_count > 0:
            score += 15
            missing.append(f"Needs more sitelinks ({sitelinks_count}/4)")
        else:
            missing.append("Missing Sitelinks (min 4)")
            
        if images_count >= 4:
            score += 25
        elif images_count > 0:
            score += 15
            missing.append(f"Needs more images ({images_count}/4)")
        else:
            missing.append("Missing Visual Images (min 4)")
            
        if has_snippets:
            score += 15
        else:
            missing.append("Missing Structured Snippets")
            
        if has_callouts:
            score += 15
        else:
            missing.append("Missing Callouts")
            
        if has_promo:
            score += 10
        else:
            missing.append("Missing Promotion (PROMO10)")
            
        if has_cust_price:
            score += 5
        if has_cust_logo:
            score += 5
            
        summaries.append(CampaignAssetDiversity(
            campaign_id=cid,
            campaign_name=cname,
            has_images=(images_count > 0),
            image_count=images_count,
            has_sitelinks=(sitelinks_count > 0),
            sitelink_count=sitelinks_count,
            has_snippets=has_snippets,
            has_callouts=has_callouts,
            has_promotions=has_promo,
            has_price=has_cust_price,
            has_business_name=has_cust_name,
            has_business_logo=has_cust_logo,
            missing_asset_types=missing,
            diversity_score=min(100, score)
        ))
        
    return summaries

# --- MUTATIONS: LINK & UNLINK ASSETS ---

@handle_google_ads_errors
def link_asset_to_entity(req: LinkAssetRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Binds an asset to Customer (Account), Campaign, or Ad Group.
    """
    client = get_ads_client()
    f_type_enum = getattr(client.enums.AssetFieldTypeEnum, req.field_type.upper())
    
    if req.level == "CUSTOMER":
        service = client.get_service("CustomerAssetService")
        op = client.get_type("CustomerAssetOperation")
        ca = op.create
        ca.asset = client.get_service("AssetService").asset_path(customer_id, req.asset_id)
        ca.field_type = f_type_enum
        service.mutate_customer_assets(customer_id=customer_id, operations=[op])
        return f"Successfully linked Asset {req.asset_id} to Account as {req.field_type}."
        
    elif req.level == "CAMPAIGN":
        service = client.get_service("CampaignAssetService")
        op = client.get_type("CampaignAssetOperation")
        ca = op.create
        ca.campaign = client.get_service("CampaignService").campaign_path(customer_id, req.entity_id)
        ca.asset = client.get_service("AssetService").asset_path(customer_id, req.asset_id)
        ca.field_type = f_type_enum
        service.mutate_campaign_assets(customer_id=customer_id, operations=[op])
        return f"Successfully linked Asset {req.asset_id} to Campaign {req.entity_id} as {req.field_type}."
        
    else: # AD_GROUP
        service = client.get_service("AdGroupAssetService")
        op = client.get_type("AdGroupAssetOperation")
        aga = op.create
        aga.ad_group = client.get_service("AdGroupService").ad_group_path(customer_id, req.entity_id)
        aga.asset = client.get_service("AssetService").asset_path(customer_id, req.asset_id)
        aga.field_type = f_type_enum
        service.mutate_ad_group_assets(customer_id=customer_id, operations=[op])
        return f"Successfully linked Asset {req.asset_id} to AdGroup {req.entity_id} as {req.field_type}."

@handle_google_ads_errors
def unlink_asset_from_entity(req: UnlinkAssetRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Unlinks/removes an asset association from Customer (Account), Campaign, or Ad Group.
    """
    client = get_ads_client()
    
    if req.level == "CUSTOMER":
        service = client.get_service("CustomerAssetService")
        resource_name = service.customer_asset_path(customer_id, req.asset_id, req.field_type.upper())
        op = client.get_type("CustomerAssetOperation")
        op.remove = resource_name
        service.mutate_customer_assets(customer_id=customer_id, operations=[op])
        return f"Successfully unlinked Asset {req.asset_id} from Account ({req.field_type})."
        
    elif req.level == "CAMPAIGN":
        service = client.get_service("CampaignAssetService")
        resource_name = service.campaign_asset_path(customer_id, req.entity_id, req.asset_id, req.field_type.upper())
        op = client.get_type("CampaignAssetOperation")
        op.remove = resource_name
        service.mutate_campaign_assets(customer_id=customer_id, operations=[op])
        return f"Successfully unlinked Asset {req.asset_id} from Campaign {req.entity_id} ({req.field_type})."
        
    else: # AD_GROUP
        service = client.get_service("AdGroupAssetService")
        resource_name = service.ad_group_asset_path(customer_id, req.entity_id, req.asset_id, req.field_type.upper())
        op = client.get_type("AdGroupAssetOperation")
        op.remove = resource_name
        service.mutate_ad_group_assets(customer_id=customer_id, operations=[op])
        return f"Successfully unlinked Asset {req.asset_id} from AdGroup {req.entity_id} ({req.field_type})."

@handle_google_ads_errors
def create_price_asset(req: CreatePriceAssetRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Creates a new Price Asset in Google Ads with the specified offerings.
    """
    client = get_ads_client()
    asset_service = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    a = op.create
    a.name = req.name
    a.type_ = client.enums.AssetTypeEnum.PRICE
    a.price_asset.type_ = getattr(client.enums.PriceExtensionTypeEnum, req.price_type)
    a.price_asset.price_qualifier = getattr(client.enums.PriceExtensionPriceQualifierEnum, req.price_qualifier)
    a.price_asset.language_code = req.language_code

    for off in req.offerings:
        po = client.get_type("PriceOffering")
        po.header = off.header
        po.description = off.description
        po.price.currency_code = "EUR"
        po.price.amount_micros = int(round(off.price_eur * 1_000_000))
        po.unit = client.enums.PriceExtensionPriceUnitEnum.UNSPECIFIED
        po.final_url = off.final_url
        a.price_asset.price_offerings.append(po)

    res = asset_service.mutate_assets(customer_id=customer_id, operations=[op])
    created_id = res.results[0].resource_name.split("/")[-1]
    return f"Successfully created Price Asset {created_id} ('{req.name}') with {len(req.offerings)} offerings."


@handle_google_ads_errors
def sync_price_extension(req: SyncPriceExtensionRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Smart Facade Macro Tool:
    1. Creates a new Price Asset with validated offerings (EUR prices, valid URLs, headers).
    2. Discovers all existing active Price Assets linked to the specified entity (Customer, Campaign, or Ad Group).
    3. Unlinks all existing old Price Assets.
    4. Links the newly created Price Asset to the target entity.
    5. Returns an itemized report of unlinked old assets and the newly active asset.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    asset_service = client.get_service("AssetService")

    # 1. CREATE NEW PRICE ASSET
    op = client.get_type("AssetOperation")
    a = op.create
    a.name = req.name
    a.type_ = client.enums.AssetTypeEnum.PRICE
    a.price_asset.type_ = getattr(client.enums.PriceExtensionTypeEnum, req.price_type)
    a.price_asset.price_qualifier = getattr(client.enums.PriceExtensionPriceQualifierEnum, req.price_qualifier)
    a.price_asset.language_code = req.language_code

    for off in req.offerings:
        po = client.get_type("PriceOffering")
        po.header = off.header
        po.description = off.description
        po.price.currency_code = "EUR"
        po.price.amount_micros = int(round(off.price_eur * 1_000_000))
        po.unit = client.enums.PriceExtensionPriceUnitEnum.UNSPECIFIED
        po.final_url = off.final_url
        a.price_asset.price_offerings.append(po)

    res = asset_service.mutate_assets(customer_id=customer_id, operations=[op])
    new_asset_id = res.results[0].resource_name.split("/")[-1]

    # 2. DISCOVER EXISTING PRICE ASSETS & MUTATE AT TARGET LEVEL
    f_type_enum = client.enums.AssetFieldTypeEnum.PRICE
    unlinked_ids = []

    if req.level == "CUSTOMER":
        q = """
        SELECT customer_asset.asset, customer_asset.resource_name
        FROM customer_asset
        WHERE customer_asset.field_type = 'PRICE' AND customer_asset.status != 'REMOVED'
        """
        rows = list(ga_service.search(customer_id=customer_id, query=q))
        cust_service = client.get_service("CustomerAssetService")
        remove_ops = []
        for r in rows:
            old_id = r.customer_asset.asset.split("/")[-1]
            if old_id != new_asset_id:
                op_rm = client.get_type("CustomerAssetOperation")
                op_rm.remove = r.customer_asset.resource_name
                remove_ops.append(op_rm)
                unlinked_ids.append(old_id)
        if remove_ops:
            cust_service.mutate_customer_assets(customer_id=customer_id, operations=remove_ops)

        op_link = client.get_type("CustomerAssetOperation")
        ca = op_link.create
        ca.asset = asset_service.asset_path(customer_id, new_asset_id)
        ca.field_type = f_type_enum
        cust_service.mutate_customer_assets(customer_id=customer_id, operations=[op_link])

    elif req.level == "CAMPAIGN":
        q = f"""
        SELECT campaign.id, campaign_asset.asset, campaign_asset.resource_name
        FROM campaign_asset
        WHERE campaign.id = '{req.entity_id}' AND campaign_asset.field_type = 'PRICE' AND campaign_asset.status != 'REMOVED'
        """
        rows = list(ga_service.search(customer_id=customer_id, query=q))
        camp_service = client.get_service("CampaignAssetService")
        remove_ops = []
        for r in rows:
            old_id = r.campaign_asset.asset.split("/")[-1]
            if old_id != new_asset_id:
                op_rm = client.get_type("CampaignAssetOperation")
                op_rm.remove = r.campaign_asset.resource_name
                remove_ops.append(op_rm)
                unlinked_ids.append(old_id)
        if remove_ops:
            camp_service.mutate_campaign_assets(customer_id=customer_id, operations=remove_ops)

        op_link = client.get_type("CampaignAssetOperation")
        ca = op_link.create
        ca.campaign = client.get_service("CampaignService").campaign_path(customer_id, req.entity_id)
        ca.asset = asset_service.asset_path(customer_id, new_asset_id)
        ca.field_type = f_type_enum
        camp_service.mutate_campaign_assets(customer_id=customer_id, operations=[op_link])

    else:  # AD_GROUP
        q = f"""
        SELECT ad_group.id, ad_group_asset.asset, ad_group_asset.resource_name
        FROM ad_group_asset
        WHERE ad_group.id = '{req.entity_id}' AND ad_group_asset.field_type = 'PRICE' AND ad_group_asset.status != 'REMOVED'
        """
        rows = list(ga_service.search(customer_id=customer_id, query=q))
        ag_service = client.get_service("AdGroupAssetService")
        remove_ops = []
        for r in rows:
            old_id = r.ad_group_asset.asset.split("/")[-1]
            if old_id != new_asset_id:
                op_rm = client.get_type("AdGroupAssetOperation")
                op_rm.remove = r.ad_group_asset.resource_name
                remove_ops.append(op_rm)
                unlinked_ids.append(old_id)
        if remove_ops:
            ag_service.mutate_ad_group_assets(customer_id=customer_id, operations=remove_ops)

        op_link = client.get_type("AdGroupAssetOperation")
        aga = op_link.create
        aga.ad_group = client.get_service("AdGroupService").ad_group_path(customer_id, req.entity_id)
        aga.asset = asset_service.asset_path(customer_id, new_asset_id)
        aga.field_type = f_type_enum
        ag_service.mutate_ad_group_assets(customer_id=customer_id, operations=[op_link])

    return (
        f"Smart Facade SUCCESS: Price Extension synchronized on {req.level} '{req.entity_id}'. "
        f"Created Asset {new_asset_id} ('{req.name}', {len(req.offerings)} offerings). "
        f"Unlinked {len(unlinked_ids)} old asset(s): {unlinked_ids if unlinked_ids else 'none'}. "
        f"New asset {new_asset_id} is now ACTIVE."
    )


# --- SITELINKS OF THE SITE: PREVIEW (read) AND CREATE + LINK (write) ---

SITELINKS_WANTED = 4  # the site-ads skill: at least 4 sitelinks on the campaign

def _sitelink_key(link_text: str, description1: str, description2: str, final_url: str):
    """What makes two sitelink assets the same: the three texts and the address, compared without case and trailing slash."""
    return (link_text.strip().casefold(), description1.strip().casefold(),
            description2.strip().casefold(), final_url.strip().rstrip("/").casefold())

def _plan_sitelinks(client, customer_id: str, req: CreateSitelinkAssetsRequest):
    """
    Reads the account and says what create_sitelink_assets would do, changing nothing. Per sitelink one action:
    `create` (new asset + link), `link_existing` (an identical asset exists, only the link is missing) or
    `already_linked`. Returns (plan, None), or (None, ServiceResult) when the campaign does not exist.
    """
    ga_service = client.get_service("GoogleAdsService")
    campaign_rows = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT campaign.id, campaign.name, campaign.status
        FROM campaign
        WHERE campaign.id = '{req.campaign_id}' AND campaign.status != 'REMOVED'
    """))
    if not campaign_rows:
        return None, ServiceResult(
            success=False,
            error=ApiError(code="CAMPAIGN_NOT_FOUND", message=f"Campaign {req.campaign_id} does not exist in this account.",
                           trigger=req.campaign_id, field_path=["campaign_id"]),
            telemetry=Telemetry(latency_ms=0.0))
    campaign = campaign_rows[0].campaign

    existing = {}  # key of the four texts -> asset id; an identical asset is reused, never duplicated
    for row in ga_service.search(customer_id=customer_id, query="""
        SELECT asset.id, asset.final_urls, asset.sitelink_asset.link_text,
               asset.sitelink_asset.description1, asset.sitelink_asset.description2
        FROM asset
        WHERE asset.type = 'SITELINK'
    """):
        urls = list(row.asset.final_urls)
        if urls:
            sl = row.asset.sitelink_asset
            existing.setdefault(_sitelink_key(sl.link_text, sl.description1, sl.description2, urls[0]), str(row.asset.id))

    linked_ids = {row.campaign_asset.asset.split("/")[-1] for row in ga_service.search(customer_id=customer_id, query=f"""
        SELECT campaign.id, campaign_asset.asset
        FROM campaign_asset
        WHERE campaign.id = '{req.campaign_id}' AND campaign_asset.field_type = 'SITELINK' AND campaign_asset.status != 'REMOVED'
    """)}

    items, to_link = [], 0
    for s in req.sitelinks:
        asset_id = existing.get(_sitelink_key(s.link_text, s.description1, s.description2, s.final_url))
        action = "already_linked" if asset_id in linked_ids and asset_id else "link_existing" if asset_id else "create"
        to_link += action != "already_linked"
        items.append({"link_text": s.link_text, "final_url": s.final_url, "action": action, "asset_id": asset_id})
    after = len(linked_ids) + to_link
    return {
        "campaign_id": req.campaign_id,
        "campaign_name": campaign.name,
        "campaign_status": getattr(campaign.status, "name", str(campaign.status)),
        "sitelinks_on_campaign_now": len(linked_ids),
        "sitelinks_on_campaign_after": after,
        "warnings": [] if after >= SITELINKS_WANTED else [f"The campaign would have {after} sitelinks; the site rules want at least {SITELINKS_WANTED}."],
        "items": items,
    }, None

@handle_google_ads_errors
def preview_sitelink_assets(req: CreateSitelinkAssetsRequest, customer_id: str = CUSTOMER_ID):
    """Read-only: what create_sitelink_assets would create and link for this campaign. Nothing is changed."""
    plan, failure = _plan_sitelinks(get_ads_client(), customer_id, req)
    if failure:
        return failure
    plan["changes_nothing"] = True
    return json.dumps(plan, ensure_ascii=False, indent=2)

@handle_google_ads_errors
def create_sitelink_assets(req: CreateSitelinkAssetsRequest, customer_id: str = CUSTOMER_ID):
    """
    Creates the sitelink assets that do not exist yet (one atomic request) and links every sitelink that is not linked
    yet to the campaign (a second atomic request). Identical existing assets are reused and linked sitelinks are skipped,
    so a repeat after a failure between the two requests finishes the job instead of making duplicates.
    """
    client = get_ads_client()
    plan, failure = _plan_sitelinks(client, customer_id, req)
    if failure:
        return failure
    asset_service = client.get_service("AssetService")
    by_text = {s.link_text: s for s in req.sitelinks}

    to_create = [item for item in plan["items"] if item["action"] == "create"]
    if to_create:
        operations = []
        for item in to_create:
            s = by_text[item["link_text"]]
            op = client.get_type("AssetOperation")
            asset = op.create
            asset.sitelink_asset.link_text = s.link_text
            asset.sitelink_asset.description1 = s.description1
            asset.sitelink_asset.description2 = s.description2
            asset.final_urls.append(s.final_url)
            operations.append(op)
        response = asset_service.mutate_assets(customer_id=customer_id, operations=operations)
        for item, result in zip(to_create, response.results):
            item["asset_id"] = result.resource_name.split("/")[-1]

    to_link = [item for item in plan["items"] if item["action"] != "already_linked"]
    if to_link:
        campaign_path = client.get_service("CampaignService").campaign_path(customer_id, req.campaign_id)
        operations = []
        for item in to_link:
            op = client.get_type("CampaignAssetOperation")
            link = op.create
            link.campaign = campaign_path
            link.asset = asset_service.asset_path(customer_id, item["asset_id"])
            link.field_type = client.enums.AssetFieldTypeEnum.SITELINK
            operations.append(op)
        client.get_service("CampaignAssetService").mutate_campaign_assets(customer_id=customer_id, operations=operations)

    created = [i["asset_id"] for i in to_create]
    reused = [i["asset_id"] for i in plan["items"] if i["action"] == "link_existing"]
    skipped = [i["asset_id"] for i in plan["items"] if i["action"] == "already_linked"]
    return (
        f"Sitelinks on Campaign {req.campaign_id} ('{plan['campaign_name']}'): created {len(created)} new asset(s) {created or ''}, "
        f"linked {len(to_link)} (reused existing {reused or 'none'}), already linked {len(skipped)} {skipped or ''}. "
        f"The campaign now has {plan['sitelinks_on_campaign_after']} sitelinks."
    )


def _require_campaign(ga_service, customer_id: str, campaign_id: str):
    """The live campaign row, or a typed failure when the id is not in this account."""
    rows = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT campaign.id, campaign.name, campaign.status
        FROM campaign
        WHERE campaign.id = '{campaign_id}' AND campaign.status != 'REMOVED'
    """))
    if not rows:
        return None, ServiceResult(
            success=False,
            error=ApiError(code="CAMPAIGN_NOT_FOUND", message=f"Campaign {campaign_id} does not exist in this account.",
                           trigger=campaign_id, field_path=["campaign_id"]),
            telemetry=Telemetry(latency_ms=0.0))
    return rows[0].campaign, None


def _link_assets(client, customer_id: str, campaign_id: str, asset_ids, field_type):
    """One CampaignAssetService request that links every given asset to the campaign."""
    asset_service = client.get_service("AssetService")
    campaign_path = client.get_service("CampaignService").campaign_path(customer_id, campaign_id)
    operations = []
    for asset_id in asset_ids:
        op = client.get_type("CampaignAssetOperation")
        link = op.create
        link.campaign = campaign_path
        link.asset = asset_service.asset_path(customer_id, asset_id)
        link.field_type = field_type
        operations.append(op)
    client.get_service("CampaignAssetService").mutate_campaign_assets(customer_id=customer_id, operations=operations)


@handle_google_ads_errors
def create_callout_assets(req: CreateCalloutAssetsRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Creates the callout assets that do not exist yet (one request) and links every callout that is not linked yet
    to the campaign (a second request). An existing callout with the same text is reused. A repeat writes nothing.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    campaign, failure = _require_campaign(ga_service, customer_id, req.campaign_id)
    if failure:
        return failure

    existing = {}
    for row in ga_service.search(customer_id=customer_id, query="""
        SELECT asset.id, asset.callout_asset.callout_text
        FROM asset
        WHERE asset.type = 'CALLOUT'
    """):
        text = getattr(getattr(row.asset, "callout_asset", None), "callout_text", None)
        if text:
            existing.setdefault(text.strip().casefold(), str(row.asset.id))

    linked_ids = {row.campaign_asset.asset.split("/")[-1] for row in ga_service.search(customer_id=customer_id, query=f"""
        SELECT campaign.id, campaign_asset.asset
        FROM campaign_asset
        WHERE campaign.id = '{req.campaign_id}'
          AND campaign_asset.field_type = 'CALLOUT' AND campaign_asset.status != 'REMOVED'
    """)}

    to_create, to_link, reused, skipped = [], [], [], []
    for text in req.callouts:
        asset_id = existing.get(text.casefold())
        if asset_id and asset_id in linked_ids:
            skipped.append(asset_id)
        elif asset_id:
            reused.append(asset_id)
            to_link.append(asset_id)
        else:
            to_create.append(text)

    created = []
    if to_create:
        asset_service = client.get_service("AssetService")
        operations = []
        for text in to_create:
            op = client.get_type("AssetOperation")
            op.create.callout_asset.callout_text = text
            operations.append(op)
        response = asset_service.mutate_assets(customer_id=customer_id, operations=operations)
        for text, result in zip(to_create, response.results):
            asset_id = result.resource_name.split("/")[-1]
            created.append(asset_id)
            existing[text.casefold()] = asset_id
            to_link.append(asset_id)

    if to_link:
        _link_assets(client, customer_id, req.campaign_id, to_link, client.enums.AssetFieldTypeEnum.CALLOUT)

    return (
        f"Callouts on Campaign {req.campaign_id} ('{campaign.name}'): created {len(created)} new asset(s) {created or ''}, "
        f"linked {len(to_link)} (reused existing {reused or 'none'}), already linked {len(skipped)} {skipped or ''}. "
        f"The campaign now has {len(linked_ids) + len(to_link)} callouts."
    )


def _snippet_values(raw) -> frozenset:
    return frozenset(str(value).strip().casefold() for value in list(raw or []) if str(value).strip())


@handle_google_ads_errors
def create_structured_snippet_asset(req: CreateStructuredSnippetAssetRequest, customer_id: str = CUSTOMER_ID) -> str:
    """
    Creates one structured snippet when no asset has the same header and the same set of values, then links it to the
    campaign. An identical asset is reused. The same header already linked with different values is refused: Google
    serves one snippet per header, and a second link would be a different asset, not an edit of the first.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    campaign, failure = _require_campaign(ga_service, customer_id, req.campaign_id)
    if failure:
        return failure

    wanted = _snippet_values(req.values)
    reuse_id = None
    for row in ga_service.search(customer_id=customer_id, query="""
        SELECT asset.id, asset.structured_snippet_asset.header, asset.structured_snippet_asset.values
        FROM asset
        WHERE asset.type = 'STRUCTURED_SNIPPET'
    """):
        snippet = getattr(row.asset, "structured_snippet_asset", None)
        if snippet and getattr(snippet, "header", None) == req.header and _snippet_values(snippet.values) == wanted:
            reuse_id = str(row.asset.id)
            break

    linked = []
    for row in ga_service.search(customer_id=customer_id, query=f"""
        SELECT campaign.id, campaign_asset.asset, asset.structured_snippet_asset.header, asset.structured_snippet_asset.values
        FROM campaign_asset
        WHERE campaign.id = '{req.campaign_id}'
          AND campaign_asset.field_type = 'STRUCTURED_SNIPPET' AND campaign_asset.status != 'REMOVED'
    """):
        snippet = getattr(row.asset, "structured_snippet_asset", None)
        linked.append((
            row.campaign_asset.asset.split("/")[-1],
            getattr(snippet, "header", None),
            _snippet_values(getattr(snippet, "values", None)),
        ))

    same_header = [item for item in linked if item[1] == req.header]
    if same_header:
        asset_id, _header, values = same_header[0]
        if values == wanted:
            return (
                f"Structured snippet on Campaign {req.campaign_id} ('{campaign.name}'): header '{req.header}' already linked "
                f"[{asset_id}]. Nothing was created."
            )
        return ServiceResult(
            success=False,
            error=ApiError(
                code="HEADER_ALREADY_ON_CAMPAIGN",
                message=(f"Campaign {req.campaign_id} already has a structured snippet with header '{req.header}' "
                         f"and different values (asset {asset_id}). Unlink it before attaching another."),
                trigger=req.header, field_path=["header"]),
            telemetry=Telemetry(latency_ms=0.0))

    created = []
    asset_id = reuse_id
    if not asset_id:
        op = client.get_type("AssetOperation")
        snippet = op.create.structured_snippet_asset
        snippet.header = req.header
        snippet.values.extend(req.values)
        response = client.get_service("AssetService").mutate_assets(customer_id=customer_id, operations=[op])
        asset_id = response.results[0].resource_name.split("/")[-1]
        created.append(asset_id)

    _link_assets(client, customer_id, req.campaign_id, [asset_id], client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET)
    reused = [] if created else [asset_id]
    return (
        f"Structured snippet on Campaign {req.campaign_id} ('{campaign.name}'): header '{req.header}', "
        f"created {len(created)} new asset(s) {created or ''}, reused existing {reused or 'none'}, linked [{asset_id}]."
    )


@handle_google_ads_errors
def get_campaign_assets_overview(campaign_id: str, customer_id: str = CUSTOMER_ID) -> str:
    """
    Inspects all Level 4 Assets (Objects) linked to a Campaign and its Ad Groups
    (Images, Sitelinks, Callouts, Structured Snippets, Promotions, Prices).
    Returns a comprehensive status matrix showing active and missing asset types.
    """
    client = get_ads_client()
    ga = client.get_service("GoogleAdsService")
    
    # 1. Query Campaign Assets
    q_camp = f"""
    SELECT campaign.id, campaign_asset.asset, campaign_asset.field_type, campaign_asset.status, asset.name, asset.type
    FROM campaign_asset
    WHERE campaign.id = '{campaign_id}' AND campaign_asset.status = 'ENABLED'
    """
    camp_assets = list(ga.search(customer_id=customer_id, query=q_camp))
    
    # 2. Query Ad Group Assets
    q_ag = f"""
    SELECT campaign.id, ad_group.name, ad_group_asset.asset, ad_group_asset.field_type, ad_group_asset.status, asset.name, asset.type
    FROM ad_group_asset
    WHERE campaign.id = '{campaign_id}' AND ad_group_asset.status = 'ENABLED'
    """
    ag_assets = list(ga.search(customer_id=customer_id, query=q_ag))
    
    output = []
    output.append("=" * 65)
    output.append(f"📦 LEVEL 4 ASSETS AUDIT FOR CAMPAIGN {campaign_id}")
    output.append("=" * 65)
    
    # Campaign Level Assets Summary
    output.append(f"\n🏛️ CAMPAIGN-LEVEL ASSETS ({len(camp_assets)} active):")
    if camp_assets:
        for ca in camp_assets:
            output.append(f"  • [{ca.asset.type.name}] Field: {ca.campaign_asset.field_type.name} | Name: '{ca.asset.name}'")
    else:
        output.append("  ⚠️ Zero campaign-level assets attached!")
        
    # Ad Group Level Assets Summary
    output.append(f"\n📂 AD GROUP-LEVEL ASSETS ({len(ag_assets)} active):")
    if ag_assets:
        by_ag = {}
        for aa in ag_assets:
            by_ag.setdefault(aa.ad_group.name, []).append(f"[{aa.asset.type.name}] {aa.asset.name}")
        for ag_name, items in by_ag.items():
            output.append(f"  📁 {ag_name} ({len(items)} assets):")
            for it in items[:4]:
                output.append(f"     - {it}")
            if len(items) > 4:
                output.append(f"     ... (+{len(items)-4} more)")
    else:
        output.append("  ⚠️ Zero ad-group level assets attached!")
        
    # Missing Assets Health Check
    types_present = set(ca.asset.type.name for ca in camp_assets) | set(aa.asset.type.name for aa in ag_assets)
    checklist = {
        "IMAGE": "Изображения (Square 1:1 & Landscape 1.91:1)",
        "SITELINK": "Дополнительные ссылки (Sitelinks)",
        "CALLOUT": "Уточнения (Callouts - Greenguard, Free Shipping)",
        "PROMOTION": "Промоакция (Promotion PROMO10)",
        "STRUCTURED_SNIPPET": "Структурированные описания (Snippets)"
    }
    output.append("\n" + "-" * 65)
    output.append("📋 ASSET COMPLETION HEALTH CHECK:")
    for t, desc in checklist.items():
        status = "✅ ACTIVE" if t in types_present else "❌ MISSING"
        output.append(f"  {status:<12} | {desc}")
    output.append("=" * 65)
    
    return "\n".join(output)
