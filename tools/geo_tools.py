import sys
from pathlib import Path
from typing import Dict, List, Optional

# Add root of google-ads to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client
from core.config import CUSTOMER_ID
from shield.error_shield import handle_google_ads_errors
from shield.models_geo import (
    GeoPhysicalAuditResponse, CountryLocationTrafficRecord, CampaignGeoLeakRecord
)

KNOWN_GEO_CONSTANTS = {
    "2276": ("DE", "Germany"),
    "2040": ("AT", "Austria"),
    "2250": ("FR", "France"),
    "2724": ("ES", "Spain"),
    "2056": ("BE", "Belgium"),
    "2528": ("NL", "Netherlands"),
    "2756": ("CH", "Switzerland"),
    "2840": ("US", "United States"),
    "2826": ("GB", "United Kingdom"),
    "2792": ("TR", "Turkey"),
    "2616": ("PL", "Poland"),
    "2380": ("IT", "Italy"),
    "2356": ("IN", "India"),
    "2076": ("BR", "Brazil"),
    "2124": ("CA", "Canada"),
    "2036": ("AU", "Australia"),
    "2643": ("RU", "Russia"),
    "2804": ("UA", "Ukraine"),
    "2642": ("RO", "Romania"),
    "2100": ("BG", "Bulgaria")
}

# E-Commerce active delivery target countries
TARGET_COUNTRY_CODES = {"DE", "AT", "FR", "ES", "BE", "NL"}

@handle_google_ads_errors
def audit_geo_physical_locations(
    days: int = 30,
    customer_id: str = CUSTOMER_ID
) -> GeoPhysicalAuditResponse:
    """
    Audits physical user locations from user_location_view to detect budget wasted
    on users outside target countries (e.g. clicks from US, TR, IN via 'Presence or Interest' default).
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")

    date_filter = "LAST_30_DAYS"
    if days <= 7:
        date_filter = "LAST_7_DAYS"
    elif days <= 14:
        date_filter = "LAST_14_DAYS"
    else:
        date_filter = "LAST_30_DAYS"

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            user_location_view.country_criterion_id,
            user_location_view.targeting_location,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM user_location_view
        WHERE segments.date DURING {date_filter}
          AND metrics.impressions > 0
    """

    response = ga_service.search(customer_id=customer_id, query=query)

    country_aggregates: Dict[str, Dict] = {}
    campaign_leaks: Dict[str, Dict] = {}
    total_cost_micros = 0
    target_cost_micros = 0
    leaked_cost_micros = 0

    for row in response:
        c = row.campaign
        c_id = str(c.id)
        c_name = str(c.name)
        ulv = row.user_location_view
        crit_id = str(ulv.country_criterion_id) if ulv.country_criterion_id else "UNKNOWN"
        m = row.metrics

        cost = m.cost_micros or 0
        clicks = m.clicks or 0
        impressions = m.impressions or 0
        conversions = float(m.conversions) if m.conversions else 0.0

        total_cost_micros += cost

        code, name = KNOWN_GEO_CONSTANTS.get(crit_id, ("??", f"Location ID {crit_id}"))
        is_target = code in TARGET_COUNTRY_CODES

        if is_target:
            target_cost_micros += cost
        else:
            leaked_cost_micros += cost
            if c_id not in campaign_leaks:
                campaign_leaks[c_id] = {
                    "campaign_id": c_id,
                    "campaign_name": c_name,
                    "leaked_clicks": 0,
                    "leaked_cost_micros": 0,
                    "countries": set()
                }
            campaign_leaks[c_id]["leaked_clicks"] += clicks
            campaign_leaks[c_id]["leaked_cost_micros"] += cost
            campaign_leaks[c_id]["countries"].add(f"{name} ({code})")

        if crit_id not in country_aggregates:
            country_aggregates[crit_id] = {
                "country_criterion_id": crit_id,
                "country_code": code,
                "country_name": name,
                "status": "TARGETED" if is_target else "LEAK",
                "impressions": 0,
                "clicks": 0,
                "cost_micros": 0,
                "conversions": 0.0
            }

        country_aggregates[crit_id]["impressions"] += impressions
        country_aggregates[crit_id]["clicks"] += clicks
        country_aggregates[crit_id]["cost_micros"] += cost
        country_aggregates[crit_id]["conversions"] += conversions

    countries_list: List[CountryLocationTrafficRecord] = []
    for agg in country_aggregates.values():
        cost_eur = round(agg["cost_micros"] / 1_000_000.0, 2)
        ctr = round((agg["clicks"] / agg["impressions"] * 100.0), 2) if agg["impressions"] > 0 else 0.0
        countries_list.append(CountryLocationTrafficRecord(
            country_criterion_id=agg["country_criterion_id"],
            country_code=agg["country_code"],
            country_name=agg["country_name"],
            status=agg["status"],
            impressions=agg["impressions"],
            clicks=agg["clicks"],
            cost_eur=cost_eur,
            conversions=round(agg["conversions"], 2),
            ctr_percent=ctr
        ))

    # Sort countries by spend descending
    countries_list.sort(key=lambda x: x.cost_eur, reverse=True)

    leaks_list: List[CampaignGeoLeakRecord] = []
    for cl in campaign_leaks.values():
        leaks_list.append(CampaignGeoLeakRecord(
            campaign_id=cl["campaign_id"],
            campaign_name=cl["campaign_name"],
            leaked_clicks=cl["leaked_clicks"],
            leaked_cost_eur=round(cl["leaked_cost_micros"] / 1_000_000.0, 2),
            leaked_countries=sorted(list(cl["countries"]))
        ))
    leaks_list.sort(key=lambda x: x.leaked_cost_eur, reverse=True)

    total_eur = round(total_cost_micros / 1_000_000.0, 2)
    target_eur = round(target_cost_micros / 1_000_000.0, 2)
    leaked_eur = round(leaked_cost_micros / 1_000_000.0, 2)
    leak_pct = round((leaked_eur / total_eur * 100.0), 1) if total_eur > 0 else 0.0

    findings = []
    if leak_pct > 0:
        findings.append(f"⚠️ Обнаружена утечка бюджета: {leaked_eur:.2f} € ({leak_pct}%) потрачено на пользователей вне стран доставки.")
        top_leaks = [f"{c.country_name} ({c.cost_eur} €)" for c in countries_list if c.status == "LEAK"][:3]
        if top_leaks:
            findings.append(f"Основные страны утечки: {', '.join(top_leaks)}.")
        findings.append("💡 Рекомендация: в настройках кампаний переключить 'Location options' (Варианты местоположений) с 'Presence or Interest' строго на 'Presence: People in or regularly in your targeted locations'.")
    else:
        findings.append("✅ 100% расходов и кликов приходятся строго на целевые страны доставки (DE, AT, FR, ES, BE, NL). Утечек не обнаружено.")

    return GeoPhysicalAuditResponse(
        period_days=days,
        total_spend_eur=total_eur,
        target_geo_spend_eur=target_eur,
        leaked_spend_eur=leaked_eur,
        leak_percentage=leak_pct,
        countries=countries_list,
        campaign_leaks=leaks_list,
        findings=findings
    )
