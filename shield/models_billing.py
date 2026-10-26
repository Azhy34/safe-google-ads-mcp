from typing import List, Optional
from pydantic import BaseModel, Field

class BillingSetupRecord(BaseModel):
    id: str = Field(..., description="Billing Setup ID")
    status: str = Field(..., description="Status (e.g. APPROVED, PENDING, CANCELLED)")
    payments_account_id: Optional[str] = Field(None, description="Payments Account ID")
    payments_account_name: Optional[str] = Field(None, description="Payments Account Name")

class InvoiceRecord(BaseModel):
    id: str = Field(..., description="Invoice ID / Number")
    type: str = Field(default="INVOICE", description="Type of document (e.g. INVOICE, CREDIT_MEMO)")
    billing_setup_id: Optional[str] = Field(None, description="Associated Billing Setup ID")
    issue_date: str = Field(..., description="Date invoice was issued (yyyy-mm-dd)")
    due_date: Optional[str] = Field(None, description="Due date (yyyy-mm-dd)")
    service_start_date: Optional[str] = Field(None, description="Start date of invoiced ad service")
    service_end_date: Optional[str] = Field(None, description="End date of invoiced ad service")
    currency_code: str = Field(default="EUR", description="Currency code (e.g. EUR)")
    subtotal_eur: float = Field(default=0.0, description="Net spend before taxes in EUR")
    tax_eur: float = Field(default=0.0, description="Tax / VAT amount in EUR")
    total_eur: float = Field(default=0.0, description="Total amount due/paid in EUR")
    pdf_url: Optional[str] = Field(None, description="Direct download URL for the invoice PDF")

class InvoicesResponse(BaseModel):
    customer_id: str = Field(..., description="Google Ads Customer ID")
    billing_setups: List[BillingSetupRecord] = Field(default_factory=list, description="Active or configured billing setups")
    invoices: List[InvoiceRecord] = Field(default_factory=list, description="List of official invoices retrieved")
    total_invoiced_eur: float = Field(default=0.0, description="Total amount across all listed invoices")
    total_tax_eur: float = Field(default=0.0, description="Total VAT / tax across all listed invoices")
    notes: List[str] = Field(default_factory=list, description="Billing status, VAT notes, or accounting reconciliation guidance")
