"""Request model of set_campaign_location: switch one country on or off inside an existing campaign."""
from pydantic import BaseModel, Field, field_validator

from core import policy

# Names for the messages only; any positive geo target ID is accepted unless the policy limits them (geo.allowed_target_ids)
COUNTRY_NAMES = {2276: "Germany", 2040: "Austria", 2756: "Switzerland", 2250: "France", 2724: "Spain", 2380: "Italy",
                 2528: "Netherlands", 2056: "Belgium", 2616: "Poland", 2826: "United Kingdom", 2840: "United States"}


def country_name(geo_id) -> str:
    return COUNTRY_NAMES.get(int(geo_id), f"geo {geo_id}")


class CampaignLocationRequest(BaseModel):
    campaign_id: str = Field(..., pattern=r"^\d+$", description="Campaign ID (from get_campaigns)")
    geo_target_id: int = Field(..., gt=0, description="Geo target constant ID of the country, e.g. 2276 = Germany, 2040 = Austria; limited to geo.allowed_target_ids of the policy when it lists any")
    enabled: bool = Field(..., description="True: target the country (add it if missing). False: stop targeting it (remove it from the campaign)")

    @field_validator("geo_target_id")
    @classmethod
    def _allowed_country(cls, value: int) -> int:
        allowed = policy.get("geo.allowed_target_ids", [])
        if allowed and value not in allowed:
            raise ValueError(f"geo_target_id must be one of {allowed} (policy geo.allowed_target_ids)")
        return value
