from typing import List, Optional
from pydantic import BaseModel, Field

class CurrencyFloorRecord(BaseModel):
    currency_code: str = Field(..., description="ISO 4217 Currency Code (e.g. EUR, USD, GBP, PLN)")
    currency_name: str = Field(..., description="Currency Name (e.g. Euro, US Dollar)")
    currency_symbol: Optional[str] = Field(None, description="Currency Symbol (e.g. €, $)")
    demand_gen_budget_floor_micros: int = Field(..., description="Demand Gen minimum daily budget in micros")
    demand_gen_budget_floor_units: float = Field(..., description="Demand Gen minimum daily budget in currency units (e.g. 5.00 EUR)")
    billable_unit_micros: Optional[int] = Field(None, description="Smallest billable unit in micros")
    source: str = Field(default="API_CURRENCY_CONSTANT", description="Source of the floor information")

class DemandGenCurrencyFloorResponse(BaseModel):
    requested_currency: str = Field(..., description="Requested currency code")
    currency_record: Optional[CurrencyFloorRecord] = Field(None, description="Details for the requested currency")
    is_supported: bool = Field(default=True, description="Whether currency is supported by Google Ads")
    notes: List[str] = Field(default_factory=list, description="Operational notes and guidelines for budget configuration")
