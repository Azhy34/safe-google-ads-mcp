from typing import List, Optional, Literal
from pydantic import BaseModel, Field

class MerchantCenterLinkRecord(BaseModel):
    product_link_id: str = Field(..., description="Unique product link identifier")
    type: str = Field(default="MERCHANT_CENTER", description="Product link type (e.g. MERCHANT_CENTER)")
    merchant_center_id: str = Field(..., description="Merchant Center Account ID")
    status: Literal["ACTIVE", "PENDING", "UNKNOWN"] = Field(default="ACTIVE", description="Link status")

class MerchantCenterInvitationRecord(BaseModel):
    invitation_id: str = Field(..., description="Product link invitation ID")
    merchant_center_id: str = Field(..., description="Merchant Center Account ID")
    type: str = Field(default="MERCHANT_CENTER", description="Product link type")
    status: str = Field(..., description="Invitation status (REQUESTED, PENDING_APPROVAL, ACCEPTED, REVOKED, REJECTED, EXPIRED)")

class MerchantCenterLinkAuditResponse(BaseModel):
    customer_id: str = Field(..., description="Google Ads Customer ID")
    target_merchant_id: Optional[str] = Field(None, description="Expected / queried Merchant Center Account ID")
    is_linked: bool = Field(..., description="True if target Merchant Center is linked and active")
    linked_accounts: List[MerchantCenterLinkRecord] = Field(default_factory=list, description="Active product links")
    pending_invitations: List[MerchantCenterInvitationRecord] = Field(default_factory=list, description="Product link invitations")
    audit_findings: List[str] = Field(default_factory=list, description="Actionable diagnostic findings")
