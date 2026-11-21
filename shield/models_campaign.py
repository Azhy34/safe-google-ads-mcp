from datetime import date
from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

from core import policy

NO_END_DATE = "2037-12-30"
MAX_END_DATE = date(2037, 12, 30)

class CampaignBudgetRecord(BaseModel):
    resource_name: Optional[str] = Field(None, description="Resource name of the budget")
    amount_micros: int = Field(..., description="Daily budget in micros (e.g. 3000000 = 3.00 EUR)")
    amount_eur: float = Field(..., description="Daily budget in EUR (calculated from micros)")
    delivery_method: str = Field(default="STANDARD", description="Budget delivery method")

class GoogleCampaignRecord(BaseModel):
    id: str = Field(..., description="Unique Google Ads Campaign ID")
    resource_name: Optional[str] = Field(None, description="Campaign resource name")
    name: str = Field(..., description="Campaign name (e.g. Brand Search DE - Category Alpha)")
    status: Literal["ENABLED", "PAUSED", "REMOVED"] = Field(..., description="Campaign serving status")
    advertising_channel_type: str = Field(default="SEARCH", description="Channel type: SEARCH, PERFORMANCE_MAX, DISPLAY")
    bidding_strategy_type: str = Field(default="TARGET_SPEND", description="Strategy: TARGET_SPEND, MANUAL_CPC, MAXIMIZE_CONVERSIONS")
    budget: Optional[CampaignBudgetRecord] = Field(None, description="Associated daily budget")
    cpc_ceiling_eur: Optional[float] = Field(None, description="Maximize Clicks (TARGET_SPEND) max CPC ceiling in EUR; None if not set or not TARGET_SPEND")

class CampaignStatusUpdateRequest(BaseModel):
    campaign_id: str = Field(..., description="ID of the campaign to modify")
    enabled: bool = Field(..., description="True to ENABLE, False to PAUSE")

class CampaignBudgetUpdateRequest(BaseModel):
    campaign_id: str = Field(..., description="ID of the campaign to modify")
    budget_eur: float = Field(..., ge=1.0, le=100.0, description="New daily budget in EUR (between 1.00 and 100.00)")

class CampaignCeilingStepRequest(BaseModel):
    campaign_id: str = Field(..., description="ID of the Maximize Clicks (TARGET_SPEND) campaign")
    direction: Literal["+0.05", "-0.05"] = Field("+0.05", description="Step the CPC ceiling up or down by 0.05 EUR")

class CampaignTargetingRecord(BaseModel):
    campaign_id: str = Field(..., description="Campaign ID")
    campaign_name: str = Field(..., description="Campaign Name")
    target_google_search: bool = Field(default=True, description="Google Search Network")
    target_search_network: bool = Field(default=False, description="Search Partners")
    target_content_network: bool = Field(default=False, description="Display Network (Must be False for clean search)")
    targeted_locations: list[str] = Field(default_factory=list, description="Targeted geo constants (e.g. Germany 2276, Austria 2040)")
    excluded_locations: list[str] = Field(default_factory=list, description="Excluded geo constants")
    targeted_languages: list[str] = Field(default_factory=list, description="Targeted language constants (e.g. German 1001)")
    ad_schedules: list[str] = Field(default_factory=list, description="Dayparting ad schedules (e.g. 08:00 - 24:00)")
    device_modifiers: list[str] = Field(default_factory=list, description="Device bid adjustments")

class ChangeEventRecord(BaseModel):
    changed_at: str = Field(..., description="Change time in account time zone")
    client_type: str = Field(..., description="Who made it: GOOGLE_ADS_WEB_CLIENT (UI), GOOGLE_ADS_API, GOOGLE_ADS_AUTOMATED_RULE, ...")
    user_email: Optional[str] = Field(None, description="Google account that made the change")
    resource_type: str = Field(..., description="CAMPAIGN, AD_GROUP, CAMPAIGN_CRITERION, AD_GROUP_AD, CAMPAIGN_BUDGET, ...")
    operation: str = Field(..., description="CREATE, UPDATE or REMOVE")
    campaign_name: Optional[str] = Field(None, description="Campaign the change belongs to")
    resource_name: str = Field(..., description="Changed resource")
    keyword: Optional[str] = Field(None, description="Keyword text for keyword criteria, marked (negative) for negatives")
    changes: list[str] = Field(default_factory=list, description="Changed fields as 'path: old -> new'")

class IdArguments(BaseModel):
    """
    IDs a read tool puts into its GAQL query. Only digits: a name or text in place of an ID used to reach Google as a
    query (query_error.BAD_NUMBER, 05.10.2026) and could change the query itself.
    """
    campaign_id: Optional[str] = Field(None, pattern=r"^\d{1,20}$", description="Numeric campaign ID, e.g. 1234567890 (get_campaigns lists them)")
    ad_group_id: Optional[str] = Field(None, pattern=r"^\d{1,20}$", description="Numeric ad group ID, e.g. 1234567890 (get_ad_groups lists them)")

class CreateSearchCampaignRequest(BaseModel):
    name: str = Field(..., description="Unique campaign name, e.g. Brand Search DE - Category Alpha")
    budget_eur: float = Field(default=3.0, ge=1.0, le=50.0, description="Daily budget in EUR (1.00 - 50.00)")
    cpc_ceiling_eur: float = Field(default=0.55, ge=0.10, le=3.00, description="Maximize Clicks CPC ceiling in EUR (0.10 - 3.00)")
    status: Literal["PAUSED", "ENABLED"] = Field(default="PAUSED", description="Initial serving status: strictly PAUSED for new site campaigns")
    geo_target_constants: list[int] = Field(default_factory=lambda: list(policy.get("ads.default_geo", [])), min_length=1, description="Targeted geo IDs, e.g. 2276=Germany, 2040=Austria (default: ads.default_geo of the policy)")
    language_constants: list[int] = Field(default_factory=lambda: list(policy.get("ads.default_language", [])), min_length=1, description="Targeted language IDs, e.g. 1001=German, 1000=English (default: ads.default_language of the policy)")
    ad_group_name: str = Field(..., description="Name for the initial ad group")
    keywords: list[str] = Field(default_factory=list, description="Initial keywords in [exact] or \"phrase\" match syntax")

class CreateCampaignResponse(BaseModel):
    campaign_id: str = Field(..., description="Created Campaign ID")
    campaign_resource_name: str = Field(..., description="Campaign resource name")
    budget_id: str = Field(..., description="Created Budget ID")
    ad_group_id: str = Field(..., description="Created Ad Group ID")
    status: str = Field(..., description="Campaign status (e.g. PAUSED)")
    cpc_ceiling_eur: float = Field(..., description="Configured CPC ceiling in EUR")
    keywords_count: int = Field(..., description="Number of keywords added")


class CampaignDatesUpdateRequest(BaseModel):
    campaign_id: str = Field(..., pattern=r"^\d{1,20}$", description="Numeric ID of the campaign to modify")
    end_date: Optional[str] = Field(
        default=None,
        description="Last serving day in ISO YYYY-MM-DD (stored as 'YYYY-MM-DD 23:59:59' in account timezone; max 2037-12-30)",
    )
    start_date: Optional[str] = Field(
        default=None,
        description="Optional start day in ISO YYYY-MM-DD (stored as 'YYYY-MM-DD 00:00:00'; only editable before campaign starts)",
    )
    clear_end_date: bool = Field(
        default=False,
        description="True to remove the end date (sets Google sentinel 2037-12-30 23:59:59). Mutually exclusive with end_date",
    )
    dry_run: bool = Field(default=False, description="True = validate and preview without writing to Google Ads")

    @field_validator("end_date", "start_date", mode="before")
    @classmethod
    def _parse_iso_date(cls, value, info):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{info.field_name} must be an ISO date string YYYY-MM-DD")
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return date.fromisoformat(cleaned).isoformat()
        except ValueError as exc:
            raise ValueError(f"{info.field_name} must be an ISO date YYYY-MM-DD, got '{value}'") from exc

    @model_validator(mode="after")
    def _validate_dates(self):
        if self.end_date is None and self.start_date is None and not self.clear_end_date:
            raise ValueError("Provide at least one of end_date, start_date, or clear_end_date=True")
        if self.end_date is not None and self.clear_end_date:
            raise ValueError("end_date and clear_end_date=True are mutually exclusive")
        if self.end_date is not None:
            parsed_end = date.fromisoformat(self.end_date)
            if parsed_end < date.today():
                raise ValueError(f"end_date '{self.end_date}' cannot be in the past")
            if parsed_end > MAX_END_DATE:
                raise ValueError(f"end_date '{self.end_date}' cannot exceed Google Ads maximum '{NO_END_DATE}'")
        if self.start_date is not None and self.end_date is not None and self.end_date < self.start_date:
            raise ValueError(f"end_date '{self.end_date}' cannot be earlier than start_date '{self.start_date}'")
        return self

