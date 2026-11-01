"""
set_campaign_location: target a country in an existing campaign, or stop targeting it.

Google has no 'pause' for a location criterion that the API documents for every campaign type, so switching a country off
removes its criterion and switching it on adds it again; the campaign, ads, keywords and statistics stay as they are.
Safeguards: only the countries the policy allows (geo.allowed_target_ids), never the last targeted country (a campaign without any location
targets everywhere), and the answer is what Google holds after the change, not what was asked.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client
from core.config import CUSTOMER_ID
from shield.error_shield import ApiError, ServiceResult, Telemetry, handle_google_ads_errors
from shield.models_location import CampaignLocationRequest, country_name


def _refusal(code: str, message: str, trigger: str, field: str) -> ServiceResult:
    return ServiceResult(
        success=False,
        error=ApiError(code=code, message=message, trigger=trigger, field_path=[field]),
        telemetry=Telemetry(latency_ms=0.0))


def _targeted(ga_service, customer_id: str, campaign_id: str) -> dict:
    """{geo constant id: criterion resource name} of the positive (targeted) locations of the campaign."""
    rows = ga_service.search(customer_id=customer_id, query=f"""
        SELECT campaign_criterion.resource_name, campaign_criterion.location.geo_target_constant, campaign_criterion.negative
        FROM campaign_criterion
        WHERE campaign.id = {campaign_id} AND campaign_criterion.type = 'LOCATION' AND campaign_criterion.status != 'REMOVED'
    """)
    found = {}
    for row in rows:
        criterion = row.campaign_criterion
        if criterion.negative:
            continue
        found[str(criterion.location.geo_target_constant).split("/")[-1]] = criterion.resource_name
    return found


def _names(ids) -> str:
    return ", ".join(f"{country_name(i)} ({i})" for i in sorted(ids)) or "none"


@handle_google_ads_errors
def update_campaign_location(req: CampaignLocationRequest, customer_id: str = CUSTOMER_ID) -> str:
    """Adds or removes one country criterion of a campaign; see the module text for the safeguards."""
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    service = client.get_service("CampaignCriterionService")

    campaigns = list(ga_service.search(customer_id=customer_id, query=f"""
        SELECT campaign.id, campaign.name FROM campaign WHERE campaign.id = {req.campaign_id} AND campaign.status != 'REMOVED' LIMIT 1
    """))
    if not campaigns:
        return _refusal("CAMPAIGN_NOT_FOUND", f"Campaign {req.campaign_id} does not exist in this account.", req.campaign_id, "campaign_id")
    label = f"'{campaigns[0].campaign.name}' ({req.campaign_id})"
    country = f"{country_name(req.geo_target_id)} ({req.geo_target_id})"
    key = str(req.geo_target_id)

    before = _targeted(ga_service, customer_id, req.campaign_id)
    if req.enabled and key in before:
        return f"{country} is already targeted in {label}; nothing changed. Targeted now: {_names(before)}."
    if not req.enabled and key not in before:
        return f"{country} is not targeted in {label}; nothing changed. Targeted now: {_names(before)}."
    if not req.enabled and len(before) <= 1:
        return _refusal(
            "LAST_TARGETED_LOCATION",
            f"SAFEGUARD: {country} is the only targeted country of {label}; without any location the campaign would show everywhere. "
            "Pause the campaign (toggle_campaign_status) if that is the intent.", req.campaign_id, "geo_target_id")

    operation = client.get_type("CampaignCriterionOperation")
    if req.enabled:
        criterion = operation.create
        criterion.campaign = f"customers/{customer_id}/campaigns/{req.campaign_id}"
        criterion.location.geo_target_constant = f"geoTargetConstants/{req.geo_target_id}"
    else:
        operation.remove = before[key]
    service.mutate_campaign_criteria(customer_id=customer_id, operations=[operation])

    after = _targeted(ga_service, customer_id, req.campaign_id)
    if (key in after) != req.enabled:
        return _refusal("NOT_APPLIED", f"Google accepted the change but {country} is {'still not' if req.enabled else 'still'} targeted in {label}. "
                        f"Targeted now: {_names(after)}.", req.campaign_id, "geo_target_id")
    return (f"{label}: {country} {'now targeted' if req.enabled else 'no longer targeted'}. "
            f"Targeted before: {_names(before)}; now: {_names(after)}.")
