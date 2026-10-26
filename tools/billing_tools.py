import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Add root of google-ads to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client
from core.config import CUSTOMER_ID
from shield.error_shield import handle_google_ads_errors
from shield.models_billing import BillingSetupRecord, InvoiceRecord, InvoicesResponse

MONTH_MAP = {
    "1": "JANUARY", "01": "JANUARY", "JAN": "JANUARY", "JANUARY": "JANUARY",
    "2": "FEBRUARY", "02": "FEBRUARY", "FEB": "FEBRUARY", "FEBRUARY": "FEBRUARY",
    "3": "MARCH", "03": "MARCH", "MAR": "MARCH", "MARCH": "MARCH",
    "4": "APRIL", "04": "APRIL", "APR": "APRIL", "APRIL": "APRIL",
    "5": "MAY", "05": "MAY", "MAY": "MAY",
    "6": "JUNE", "06": "JUNE", "JUN": "JUNE", "JUNE": "JUNE",
    "7": "JULY", "07": "JULY", "JUL": "JULY", "JULY": "JULY",
    "8": "AUGUST", "08": "AUGUST", "AUG": "AUGUST", "AUGUST": "AUGUST",
    "9": "SEPTEMBER", "09": "SEPTEMBER", "SEP": "SEPTEMBER", "SEPTEMBER": "SEPTEMBER",
    "10": "OCTOBER", "OCT": "OCTOBER", "OCTOBER": "OCTOBER",
    "11": "NOVEMBER", "NOV": "NOVEMBER", "NOVEMBER": "NOVEMBER",
    "12": "DECEMBER", "DEC": "DECEMBER", "DECEMBER": "DECEMBER"
}

@handle_google_ads_errors
def get_google_ads_invoices(
    year: Optional[str] = None,
    month: Optional[str] = None,
    customer_id: str = CUSTOMER_ID
) -> InvoicesResponse:
    """
    Retrieves official Google Ads billing invoices and credit memos via InvoiceService and BillingSetup,
    including subtotal, VAT/tax, total EUR, and direct PDF download links for accounting & tax compliance.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")
    invoice_service = client.get_service("InvoiceService")

    current_year = str(datetime.now().year)
    target_year = str(year) if year else current_year

    # 1. Fetch Billing Setups
    billing_query = """
        SELECT
            billing_setup.id,
            billing_setup.status,
            billing_setup.payments_account_info.payments_account_id,
            billing_setup.payments_account_info.payments_account_name
        FROM billing_setup
        ORDER BY billing_setup.id DESC
    """
    
    billing_setups: List[BillingSetupRecord] = []
    active_setup_ids: List[str] = []

    try:
        b_response = ga_service.search(customer_id=customer_id, query=billing_query)
        for row in b_response:
            bs = row.billing_setup
            st = bs.status.name if hasattr(bs.status, "name") else str(bs.status)
            paid = getattr(bs.payments_account_info, "payments_account_id", None)
            paname = getattr(bs.payments_account_info, "payments_account_name", None)
            
            record = BillingSetupRecord(
                id=str(bs.id),
                status=st,
                payments_account_id=str(paid) if paid else None,
                payments_account_name=str(paname) if paname else None
            )
            billing_setups.append(record)
            if st in ("APPROVED", "PENDING", "UNKNOWN"):
                active_setup_ids.append(str(bs.id))
    except Exception as e:
        billing_setups = []

    notes: List[str] = []
    if not billing_setups:
        notes.append("ℹ️ Настройки биллинга (BillingSetup) не найдены через поисковый запрос или доступ ограничен.")

    # Determine months to query
    months_to_query: List[str] = []
    if month:
        normalized_month = MONTH_MAP.get(str(month).upper().strip())
        if normalized_month:
            months_to_query = [normalized_month]
        else:
            notes.append(f"⚠️ Неизвестный месяц '{month}'. Будут проверены актуальные месяцы.")
            months_to_query = [datetime.now().strftime("%B").upper()]
    else:
        max_month = datetime.now().month if target_year == current_year else 12
        all_months = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
                      "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
        months_to_query = all_months[:max_month]

    invoices: List[InvoiceRecord] = []
    total_invoiced = 0.0
    total_tax = 0.0

    # Query invoices per billing setup
    for bs_id in (active_setup_ids or [b.id for b in billing_setups]):
        billing_resource = f"customers/{customer_id}/billingSetups/{bs_id}"
        for m in months_to_query:
            try:
                month_enum = getattr(client.enums.MonthOfYearEnum, m, None)
                if not month_enum:
                    continue

                inv_resp = invoice_service.list_invoices(
                    customer_id=customer_id,
                    billing_setup=billing_resource,
                    issue_year=target_year,
                    issue_month=month_enum
                )
                
                for inv in getattr(inv_resp, "invoices", []):
                    inv_type = inv.type_.name if hasattr(inv, "type_") and hasattr(inv.type_, "name") else (inv.type.name if hasattr(inv, "type") and hasattr(inv.type, "name") else str(getattr(inv, "type", "INVOICE")))
                    subtotal = (inv.subtotal_amount_micros or 0) / 1_000_000.0
                    tax = (inv.tax_amount_micros or 0) / 1_000_000.0
                    total = (inv.total_amount_micros or 0) / 1_000_000.0

                    start_d = getattr(inv.service_date_range, "start_date", None)
                    end_d = getattr(inv.service_date_range, "end_date", None)

                    rec = InvoiceRecord(
                        id=str(inv.id),
                        type=str(inv_type),
                        billing_setup_id=str(bs_id),
                        issue_date=str(inv.issue_date),
                        due_date=str(inv.due_date) if inv.due_date else None,
                        service_start_date=str(start_d) if start_d else None,
                        service_end_date=str(end_d) if end_d else None,
                        currency_code=str(getattr(inv, "currency_code", "EUR")),
                        subtotal_eur=round(subtotal, 2),
                        tax_eur=round(tax, 2),
                        total_eur=round(total, 2),
                        pdf_url=str(inv.pdf_url) if getattr(inv, "pdf_url", None) else None
                    )
                    invoices.append(rec)
                    total_invoiced += total
                    total_tax += tax
            except Exception as e:
                err_msg = str(e)
                if "NOT_FOUND" not in err_msg and "INVALID_ARGUMENT" not in err_msg:
                    notes.append(f"Запрос инвойсов для {m} {target_year}: {err_msg[:120]}")

    if invoices:
        notes.append(f"✅ Найдено {len(invoices)} счетов-фактур на общую сумму {total_invoiced:.2f} EUR (НДС: {total_tax:.2f} EUR).")
    else:
        notes.append(f"ℹ️ Счета-фактуры за {target_year} г. не найдены через InvoiceService (аккаунт может быть на автоматических платежах с карты/счета).")

    return InvoicesResponse(
        customer_id=customer_id,
        billing_setups=billing_setups,
        invoices=invoices,
        total_invoiced_eur=round(total_invoiced, 2),
        total_tax_eur=round(total_tax, 2),
        notes=notes
    )
