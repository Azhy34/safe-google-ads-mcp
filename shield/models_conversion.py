from typing import List, Optional, Literal
from pydantic import BaseModel, Field

class ConversionActionRecord(BaseModel):
    id: str = Field(..., description="Conversion Action ID")
    name: str = Field(..., description="Name of the conversion action (e.g. Purchase, Add to cart)")
    category: str = Field(..., description="Category (e.g. PURCHASE, ADD_TO_CART, PAGE_VIEW)")
    type: str = Field(..., description="Type (e.g. WEBPAGE, GOOGLE_ANALYTICS_4_CUSTOM, UPLOAD_CALLS)")
    status: str = Field(..., description="Status (ENABLED, PAUSED, HIDDEN)")
    origin: str = Field(..., description="Origin (WEBSITE, GOOGLE_ANALYTICS, etc.)")
    primary_for_goal: bool = Field(default=False, description="Whether this conversion is primary for bidding optimization")
    include_in_conversions_metric: bool = Field(default=False, description="Whether included in 'Conversions' column")
    click_through_lookback_window_days: Optional[int] = Field(None, description="Click-through window in days")

class EnhancedConversionsStatusResponse(BaseModel):
    customer_id: str = Field(..., description="Google Ads Customer ID")
    conversion_tracking_id: Optional[str] = Field(None, description="Google Conversion Tracking ID")
    conversion_tracking_status: str = Field(..., description="Status of conversion tracking")
    enhanced_conversions_enabled: bool = Field(default=False, description="Whether enhanced conversions is enabled")
    enablement_setting: Optional[str] = Field(None, description="Raw v26 enablement setting or legacy flag status")
    accepted_customer_data_terms: bool = Field(default=False, description="Whether user accepted Customer Data Terms for Enhanced Conversions")
    conversion_actions_count: int = Field(default=0, description="Total active/defined conversion actions")
    primary_actions: List[ConversionActionRecord] = Field(default_factory=list, description="Primary conversion actions used for bidding")
    secondary_actions: List[ConversionActionRecord] = Field(default_factory=list, description="Secondary/observational conversion actions")
    audit_findings: List[str] = Field(default_factory=list, description="Audited recommendations, warnings, or action points")

class ClickConversionItem(BaseModel):
    conversion_action_id: str = Field(..., description="Conversion Action ID or full resource name (e.g. '12345678' or 'customers/123/conversionActions/456')")
    conversion_date_time: str = Field(..., description="Conversion timestamp formatted as 'yyyy-mm-dd hh:mm:ss+|-hh:mm' (e.g. '2026-10-25 14:00:00+02:00')")
    conversion_value: Optional[float] = Field(None, description="Monetary value of the conversion in the specified currency")
    currency_code: str = Field(default="EUR", description="ISO 4217 three-letter currency code (e.g. 'EUR', 'USD')")
    order_id: Optional[str] = Field(None, description="Unique transaction/order ID for server-side deduplication (e.g. Stripe checkout ID or order number)")
    gclid: Optional[str] = Field(None, description="Google Click ID from landing page URL query parameter (?gclid=...)")
    gbraid: Optional[str] = Field(None, description="GBRAID identifier for iOS web conversions (?gbraid=...)")
    wbraid: Optional[str] = Field(None, description="WBRAID identifier for iOS app-to-web conversions (?wbraid=...)")
    email: Optional[str] = Field(None, description="Customer email address (plain text will be automatically normalized with Gmail dot/+ rules and SHA256-hashed, or pre-hashed 64-char hex string)")
    phone: Optional[str] = Field(None, description="Customer phone in E.164 format (plain text will be normalized to E.164 and SHA256-hashed, or pre-hashed 64-char hex string)")
    ad_user_data_consent: str = Field(default="GRANTED", description="Consent status for ad user data (GRANTED or DENIED, required under EU DMA)")
    ad_personalization_consent: str = Field(default="GRANTED", description="Consent status for ad personalization (GRANTED or DENIED, required under EU DMA)")

    def model_post_init(self, __context):
        if not (self.gclid or self.gbraid or self.wbraid or self.email or self.phone):
            raise ValueError("At least one tracking identifier must be provided: gclid, gbraid, wbraid, email, or phone.")

class UploadConversionResultItem(BaseModel):
    order_id: Optional[str] = Field(None, description="Order/transaction ID associated with conversion")
    conversion_action: str = Field(..., description="Conversion action identifier/path")
    conversion_date_time: str = Field(..., description="Conversion timestamp recorded")
    status: str = Field(..., description="Upload status ('SUCCESS' or 'FAILED')")
    error_message: Optional[str] = Field(None, description="Error message if conversion failed validation")

class UploadClickConversionResponse(BaseModel):
    customer_id: str = Field(..., description="Google Ads Customer ID")
    uploaded_count: int = Field(default=0, description="Number of successfully uploaded conversions")
    failed_count: int = Field(default=0, description="Number of conversions that failed validation")
    partial_failure_errors: List[str] = Field(default_factory=list, description="List of partial failure errors returned by Google Ads API")
    results: List[UploadConversionResultItem] = Field(default_factory=list, description="Per-conversion upload outcome items")

class SetConversionActionGoalRequest(BaseModel):
    conversion_action_id: str = Field(..., pattern=r"^\d+$", description="Conversion Action ID (numeric string, e.g. '7677791017')")
    primary_for_goal: bool = Field(..., description="True to make Primary (bidding optimization), False for Secondary (observation only)")
    status: Optional[Literal["ENABLED", "PAUSED", "HIDDEN", "REMOVED"]] = Field(None, description="Optional status update for the action")
    include_in_conversions_metric: Optional[bool] = Field(None, description="Whether to include in 'Conversions' column")

class ConversionActionGoalUpdateResponse(BaseModel):
    customer_id: str
    conversion_action_id: str
    resource_name: str
    primary_for_goal: bool
    status: str
    message: str
