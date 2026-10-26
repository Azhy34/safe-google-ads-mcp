from typing import List, Optional, Literal
from pydantic import BaseModel, Field

class CountryLocationTrafficRecord(BaseModel):
    country_criterion_id: str = Field(..., description="Geo target constant ID of the physical country")
    country_code: str = Field(..., description="Two-letter country code (e.g. DE, AT, US, TR)")
    country_name: str = Field(..., description="Country name (e.g. Germany, Austria, United States)")
    status: Literal["TARGETED", "LEAK"] = Field(..., description="TARGETED if within shop delivery zone, else LEAK")
    impressions: int = Field(default=0, description="Total impressions")
    clicks: int = Field(default=0, description="Total clicks")
    cost_eur: float = Field(default=0.0, description="Total spend in EUR")
    conversions: float = Field(default=0.0, description="Total conversions recorded")
    ctr_percent: float = Field(default=0.0, description="Click-through rate %")

class CampaignGeoLeakRecord(BaseModel):
    campaign_id: str = Field(..., description="Campaign ID")
    campaign_name: str = Field(..., description="Campaign Name")
    leaked_clicks: int = Field(default=0, description="Clicks from outside target countries")
    leaked_cost_eur: float = Field(default=0.0, description="Budget wasted outside target countries")
    leaked_countries: List[str] = Field(default_factory=list, description="List of foreign countries that clicked ads")

class GeoPhysicalAuditResponse(BaseModel):
    period_days: int = Field(..., description="Audit lookback window in days")
    total_spend_eur: float = Field(..., description="Total spend across all audited campaigns")
    target_geo_spend_eur: float = Field(..., description="Spend delivered inside target countries (DE, AT, FR, ES, BE, NL)")
    leaked_spend_eur: float = Field(..., description="Spend leaked to foreign physical locations")
    leak_percentage: float = Field(..., description="Share of budget leaked outside target countries (%)")
    countries: List[CountryLocationTrafficRecord] = Field(default_factory=list, description="Traffic and spend breakdown by physical country")
    campaign_leaks: List[CampaignGeoLeakRecord] = Field(default_factory=list, description="Campaigns with detected foreign traffic leakage")
    findings: List[str] = Field(default_factory=list, description="Actionable recommendations and location option fixes")
