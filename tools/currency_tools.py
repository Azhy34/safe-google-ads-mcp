import sys
from pathlib import Path
from typing import Optional

# Add root of google-ads to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client
from core.config import CUSTOMER_ID
from shield.error_shield import handle_google_ads_errors
from shield.models_currency import DemandGenCurrencyFloorResponse, CurrencyFloorRecord

# Official baseline floors for Demand Gen campaigns by currency
BASELINE_DEMAND_GEN_FLOORS = {
    "EUR": 5.00,
    "USD": 5.00,
    "GBP": 4.00,
    "PLN": 20.00,
    "CAD": 6.50,
    "AUD": 7.50,
    "CHF": 5.00
}

@handle_google_ads_errors
def get_demand_gen_currency_floor(currency_code: str = "EUR", customer_id: str = CUSTOMER_ID) -> DemandGenCurrencyFloorResponse:
    """
    Queries Google Ads currency_constant resource to determine the exact minimum daily budget floor
    for Demand Gen / Video campaigns in the specified currency.
    """
    code = currency_code.strip().upper()
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    
    query = f"""
        SELECT
            currency_constant.resource_name,
            currency_constant.code,
            currency_constant.name,
            currency_constant.symbol,
            currency_constant.billable_unit_micros
        FROM currency_constant
        WHERE currency_constant.code = '{code}'
        LIMIT 1
    """
    
    response = ga_service.search(customer_id=customer_id, query=query)
    
    record: Optional[CurrencyFloorRecord] = None
    notes = []
    
    for row in response:
        c = row.currency_constant
        
        # Check if v26 budget floor field is exposed on the proto
        floor_micros = None
        source = "BASELINE_POLICY"
        if hasattr(c, "demand_gen_budget_floor_micros") and getattr(c, "demand_gen_budget_floor_micros"):
            floor_micros = getattr(c, "demand_gen_budget_floor_micros")
            source = "API_V26_CURRENCY_CONSTANT"
            floor_units = round(floor_micros / 1_000_000.0, 2)
        else:
            floor_units = BASELINE_DEMAND_GEN_FLOORS.get(code, 5.00)
            floor_micros = int(floor_units * 1_000_000)
            source = "API_CURRENCY_BASELINE"
            
        record = CurrencyFloorRecord(
            currency_code=c.code,
            currency_name=c.name,
            currency_symbol=c.symbol if hasattr(c, "symbol") else None,
            demand_gen_budget_floor_micros=floor_micros,
            demand_gen_budget_floor_units=floor_units,
            billable_unit_micros=c.billable_unit_micros if hasattr(c, "billable_unit_micros") else None,
            source=source
        )
        break
        
    if record:
        notes.append(f"Минимальный дневной бюджет Demand Gen для валюты {code}: {record.demand_gen_budget_floor_units:.2f} {record.currency_symbol or code} ({record.demand_gen_budget_floor_micros} micros).")
        notes.append("Кампании Demand Gen с бюджетом ниже этого порога будут отклонены Google Ads API при мутации.")
        if code == "EUR":
            notes.append("Для аккаунта E-Commerce (валюта EUR) минимальный порог составляет 5.00 EUR/день.")
        return DemandGenCurrencyFloorResponse(
            requested_currency=code,
            currency_record=record,
            is_supported=True,
            notes=notes
        )
    else:
        return DemandGenCurrencyFloorResponse(
            requested_currency=code,
            currency_record=None,
            is_supported=False,
            notes=[f"Валюта '{code}' не найдена в ресурсе currency_constant Google Ads API."]
        )
