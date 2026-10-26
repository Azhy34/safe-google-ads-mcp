import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client
from core.config import CUSTOMER_ID
from shield.error_shield import handle_google_ads_errors
from shield.models_merchant import (
    MerchantCenterLinkRecord, MerchantCenterInvitationRecord, MerchantCenterLinkAuditResponse
)

DEFAULT_MERCHANT_ID = None

@handle_google_ads_errors
def check_merchant_center_link(
    merchant_id: Optional[str] = DEFAULT_MERCHANT_ID,
    customer_id: str = CUSTOMER_ID
) -> MerchantCenterLinkAuditResponse:
    """
    Audits the Google Merchant Center product link status and link invitations
    in Google Ads via ProductLink and ProductLinkInvitation resources.
    """
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")

    target_id = str(merchant_id).strip() if merchant_id else None

    # 1. Query active product links
    pl_query = """
        SELECT
            product_link.product_link_id,
            product_link.type,
            product_link.merchant_center.merchant_center_id
        FROM product_link
    """
    pl_response = ga_service.search(customer_id=customer_id, query=pl_query)

    linked_accounts: List[MerchantCenterLinkRecord] = []
    is_linked = False

    for row in pl_response:
        pl = row.product_link
        type_str = pl.type_.name if hasattr(pl.type_, "name") else str(pl.type_)
        mc_id = None
        if hasattr(pl, "merchant_center") and pl.merchant_center:
            mc_id = str(getattr(pl.merchant_center, "merchant_center_id", ""))

        record = MerchantCenterLinkRecord(
            product_link_id=str(pl.product_link_id),
            type=type_str,
            merchant_center_id=mc_id or "UNKNOWN",
            status="ACTIVE"
        )
        linked_accounts.append(record)
        if target_id and mc_id == target_id:
            is_linked = True

    # 2. Query product link invitations
    pli_query = """
        SELECT
            product_link_invitation.product_link_invitation_id,
            product_link_invitation.status,
            product_link_invitation.type,
            product_link_invitation.merchant_center.merchant_center_id
        FROM product_link_invitation
    """
    pli_response = ga_service.search(customer_id=customer_id, query=pli_query)

    invitations: List[MerchantCenterInvitationRecord] = []
    for row in pli_response:
        pli = row.product_link_invitation
        st = pli.status.name if hasattr(pli.status, "name") else str(pli.status)
        t_str = pli.type_.name if hasattr(pli.type_, "name") else str(pli.type_)
        mc_id = None
        if hasattr(pli, "merchant_center") and pli.merchant_center:
            mc_id = str(getattr(pli.merchant_center, "merchant_center_id", ""))

        inv_record = MerchantCenterInvitationRecord(
            invitation_id=str(pli.product_link_invitation_id),
            merchant_center_id=mc_id or "UNKNOWN",
            type=t_str,
            status=st
        )
        invitations.append(inv_record)

    # 3. Build diagnostic findings
    findings = []
    if target_id:
        if is_linked:
            findings.append(f"✅ Google Merchant Center ID '{target_id}' успешно связан с аккаунтом Google Ads '{customer_id}' (ProductLink ACTIVE). Фид товаров готов к передаче в кампании.")
        else:
            pending = [inv for inv in invitations if inv.merchant_center_id == target_id and inv.status in ("REQUESTED", "PENDING_APPROVAL")]
            if pending:
                findings.append(f"⚠️ Для Google Merchant Center ID '{target_id}' обнаружено ожидающее приглашение (Invitation ID: {pending[0].invitation_id}, статус: {pending[0].status}). Требуется подтвердить связь в интерфейсе Google Ads или Merchant Center.")
            else:
                findings.append(f"❌ Google Merchant Center ID '{target_id}' НЕ связан с аккаунтом Google Ads '{customer_id}'. Shopping и кампании с фидом не смогут использовать каталог товаров.")
    else:
        if linked_accounts:
            findings.append(f"ℹ️ Найдено {len(linked_accounts)} активных связанных аккаунтов ProductLink.")
        else:
            findings.append("⚠️ В аккаунте Google Ads нет активных связей ProductLink.")

    return MerchantCenterLinkAuditResponse(
        customer_id=customer_id,
        target_merchant_id=target_id,
        is_linked=is_linked,
        linked_accounts=linked_accounts,
        pending_invitations=invitations,
        audit_findings=findings
    )
