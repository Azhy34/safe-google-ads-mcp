import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from shield import error_shield, cloud_sink
from shield.telemetry import redact_secrets
from shield.error_shield import MUTATION_FUNCTIONS
from shield.models_conversion import (
    EnhancedConversionsStatusResponse, ConversionActionRecord,
    ClickConversionItem, UploadClickConversionResponse
)
from shield.models_currency import DemandGenCurrencyFloorResponse, CurrencyFloorRecord
from shield.models_billing import BillingSetupRecord, InvoiceRecord, InvoicesResponse
from shield.models_criterion import SyncSharedNegativeListRequest, SharedNegativeListResponse
from shield.models_merchant import MerchantCenterLinkAuditResponse, MerchantCenterLinkRecord
from tools.currency_tools import BASELINE_DEMAND_GEN_FLOORS, get_demand_gen_currency_floor
from tools.conversion_tools import (
    check_enhanced_conversions_status, upload_click_conversions,
    _normalize_and_hash, _normalize_email, _normalize_phone
)
from tools.billing_tools import get_google_ads_invoices
from tools.negative_tools import sync_shared_negative_list
from tools.merchant_tools import check_merchant_center_link

class AuditLogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        patcher = mock.patch.object(error_shield, "AUDIT_FILE", self.audit)
        patcher.start()
        self.addCleanup(patcher.stop)

        cloud = mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None})
        cloud.start()
        self.addCleanup(cloud.stop)

        preflight_env = mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"})
        preflight_env.start()
        self.addCleanup(preflight_env.stop)

        self.addCleanup(self.tmp.cleanup)

class TestV26Tools(AuditLogCase):
    def test_conversion_action_record_model(self):
        ca = ConversionActionRecord(
            id="12345",
            name="Purchase",
            category="PURCHASE",
            type="WEBPAGE",
            status="ENABLED",
            origin="WEBSITE",
            primary_for_goal=True,
            include_in_conversions_metric=True,
            click_through_lookback_window_days=30
        )
        self.assertEqual(ca.id, "12345")
        self.assertEqual(ca.name, "Purchase")
        self.assertTrue(ca.primary_for_goal)
        self.assertEqual(ca.click_through_lookback_window_days, 30)

    def test_enhanced_conversions_response_model(self):
        resp = EnhancedConversionsStatusResponse(
            customer_id="1234567890",
            conversion_tracking_id="987654321",
            conversion_tracking_status="ACTIVELY_TRACKING",
            enhanced_conversions_enabled=True,
            enablement_setting="ENABLED",
            accepted_customer_data_terms=True,
            conversion_actions_count=1,
            primary_actions=[
                ConversionActionRecord(
                    id="1",
                    name="Purchase",
                    category="PURCHASE",
                    type="WEBPAGE",
                    status="ENABLED",
                    origin="WEBSITE",
                    primary_for_goal=True,
                    include_in_conversions_metric=True
                )
            ],
            secondary_actions=[],
            audit_findings=["All healthy"]
        )
        self.assertTrue(resp.enhanced_conversions_enabled)
        self.assertTrue(resp.accepted_customer_data_terms)
        self.assertEqual(len(resp.primary_actions), 1)

    def test_currency_floor_model(self):
        rec = CurrencyFloorRecord(
            currency_code="EUR",
            currency_name="Euro",
            currency_symbol="€",
            demand_gen_budget_floor_micros=5000000,
            demand_gen_budget_floor_units=5.00,
            billable_unit_micros=10000,
            source="API_V26_CURRENCY_CONSTANT"
        )
        self.assertEqual(rec.currency_code, "EUR")
        self.assertEqual(rec.demand_gen_budget_floor_units, 5.00)
        self.assertEqual(rec.demand_gen_budget_floor_micros, 5_000_000)

    def test_baseline_floors(self):
        self.assertEqual(BASELINE_DEMAND_GEN_FLOORS["EUR"], 5.00)
        self.assertEqual(BASELINE_DEMAND_GEN_FLOORS["USD"], 5.00)
        self.assertEqual(BASELINE_DEMAND_GEN_FLOORS["PLN"], 20.00)

    @mock.patch("tools.currency_tools.get_ads_client")
    def test_get_demand_gen_currency_floor_execution(self, mock_client_getter):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        mock_client_getter.return_value = client

        # Mock row
        row = mock.MagicMock()
        row.currency_constant.code = "EUR"
        row.currency_constant.name = "Euro"
        row.currency_constant.symbol = "€"
        row.currency_constant.billable_unit_micros = 10000
        del row.currency_constant.demand_gen_budget_floor_micros
        ga_service.search.return_value = [row]

        result = get_demand_gen_currency_floor(currency_code="EUR", customer_id="1234567890")
        self.assertTrue(result.success, f"Failed: {result.error}")
        self.assertEqual(result.data.currency_record.currency_code, "EUR")
        self.assertEqual(result.data.currency_record.demand_gen_budget_floor_units, 5.00)

    @mock.patch("tools.conversion_tools.get_ads_client")
    def test_check_enhanced_conversions_status_execution(self, mock_client_getter):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        mock_client_getter.return_value = client

        # 1st call for customer
        cust_row = mock.MagicMock()
        cust_row.customer.conversion_tracking_setting.conversion_tracking_id = 999111
        cust_row.customer.conversion_tracking_setting.conversion_tracking_status.name = "ACTIVELY_TRACKING"
        cust_row.customer.conversion_tracking_setting.accepted_customer_data_terms = True
        cust_row.customer.conversion_tracking_setting.enhanced_conversions_for_leads_enabled = True

        # 2nd call for conversion actions
        ca_row = mock.MagicMock()
        ca_row.conversion_action.id = 555
        ca_row.conversion_action.name = "Kauf"
        ca_row.conversion_action.category.name = "PURCHASE"
        ca_row.conversion_action.type.name = "WEBPAGE"
        ca_row.conversion_action.status.name = "ENABLED"
        ca_row.conversion_action.origin.name = "WEBSITE"
        ca_row.conversion_action.primary_for_goal = True
        ca_row.conversion_action.include_in_conversions_metric = True
        ca_row.conversion_action.click_through_lookback_window_days = 30

        ga_service.search.side_effect = [[cust_row], [ca_row]]

        result = check_enhanced_conversions_status(customer_id="1234567890")
        self.assertTrue(result.success, f"Failed: {result.error}")
        self.assertTrue(result.data.accepted_customer_data_terms)
        self.assertEqual(len(result.data.primary_actions), 1)
        self.assertEqual(result.data.primary_actions[0].name, "Kauf")

    def test_normalize_and_hash(self):
        # Plain email
        h1 = _normalize_and_hash(" Test.User@Example.com ")
        self.assertEqual(len(h1), 64)
        h2 = _normalize_and_hash(h1)
        self.assertEqual(h1, h2, "Pre-hashed 64-char hex string must not be re-hashed")

        # Gmail normalization rules: dots and + removal
        h_gmail_clean = _normalize_email("johndoe@gmail.com")
        h_gmail_dots = _normalize_email(" John.Doe+promo123@gmail.com ")
        self.assertEqual(h_gmail_clean, h_gmail_dots, "Gmail dot and + subaddressing must be normalized identically")

        # Googlemail normalization rules
        h_gml_clean = _normalize_email("johndoe@googlemail.com")
        h_gml_dots = _normalize_email("john.doe@googlemail.com")
        self.assertEqual(h_gml_clean, h_gml_dots, "Googlemail dots must be normalized identically")

        # Phone E.164 normalization
        h_phone_e164 = _normalize_phone("+4917683018796")
        h_phone_spaces = _normalize_phone(" (0176) 830-18796 ")
        self.assertEqual(len(h_phone_spaces), 64)
        self.assertEqual(h_phone_spaces, h_phone_e164, "German national (0176) number must normalize to +4917683018796")
        self.assertEqual(_normalize_phone("+49 (0) 176 830 18796"), h_phone_e164, "Trunk (0) must be stripped")
        self.assertEqual(_normalize_phone("0049 176 830 18796"), h_phone_e164, "Leading 00 must be normalized to +")

        # Identifier requirement validation
        with self.assertRaises(ValueError):
            ClickConversionItem(
                conversion_action_id="123",
                conversion_date_time="2026-10-25 14:00:00+02:00"
            )

        # Telemetry PII redaction test
        log_payload = {
            "email": "buyer@test.de",
            "phone": "+4917683018796",
            "message": "User with email buyer@test.de called support"
        }
        redacted = redact_secrets(log_payload)
        self.assertEqual(redacted["email"], "[REDACTED_PII]")
        self.assertEqual(redacted["phone"], "[REDACTED_PII]")
        self.assertIn("[REDACTED_EMAIL]", redacted["message"])

        # create_search_campaign must be in MUTATION_FUNCTIONS
        self.assertIn("create_search_campaign", MUTATION_FUNCTIONS)

    @mock.patch("tools.conversion_tools.get_ads_client")
    def test_upload_click_conversions_execution(self, mock_client_getter):
        client = mock.MagicMock()
        conv_service = mock.MagicMock()
        client.get_service.return_value = conv_service
        mock_client_getter.return_value = client

        # Strict Enum setup matching real Google Ads client
        client.enums.ConsentStatusEnum = mock.MagicMock(spec=["GRANTED", "DENIED"])
        client.enums.ConsentStatusEnum.GRANTED = 2
        client.enums.ConsentStatusEnum.DENIED = 3

        # Mock proto creation
        mock_click_conv = mock.MagicMock()
        mock_consent = mock.MagicMock()
        def mock_get_type(name):
            if name == "Consent":
                return mock_consent
            if name == "ClickConversion":
                return mock_click_conv
            return mock.MagicMock()
        client.get_type.side_effect = mock_get_type

        # Mock response
        mock_resp = mock.MagicMock()
        mock_resp.partial_failure_error.code = 0
        mock_res_item = mock.MagicMock()
        mock_res_item.conversion_action = "customers/123/conversionActions/777"
        mock_res_item.conversion_date_time = "2026-10-25 14:00:00+02:00"
        mock_res_item.order_id = "ORD-TEST-99"
        mock_resp.results = [mock_res_item]
        conv_service.upload_click_conversions.return_value = mock_resp

        item = ClickConversionItem(
            conversion_action_id="777",
            conversion_date_time="2026-10-25 14:00:00+02:00",
            conversion_value=199.50,
            currency_code="EUR",
            order_id="ORD-TEST-99",
            gclid="Cj0KCQjws...",
            email="buyer@test.de",
            phone="+4917683018796"
        )

        result = upload_click_conversions(conversions=[item], customer_id="1234567890")
        self.assertTrue(result.success, f"Failed: {result.error}")
        self.assertEqual(result.data.uploaded_count, 1)
        self.assertEqual(result.data.failed_count, 0)
        self.assertEqual(len(result.data.results), 1)
        self.assertEqual(result.data.results[0].status, "SUCCESS")
        self.assertEqual(result.data.results[0].order_id, "ORD-TEST-99")
        # Ensure consent is properly linked to ClickConversion and values are passed
        self.assertEqual(mock_click_conv.consent, mock_consent)
        self.assertEqual(mock_consent.ad_user_data, 2)
        self.assertEqual(mock_consent.ad_personalization, 2)

    @mock.patch("tools.billing_tools.get_ads_client")
    def test_get_google_ads_invoices_execution(self, mock_client_getter):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        inv_service = mock.MagicMock()
        client.get_service.side_effect = lambda s: ga_service if s == "GoogleAdsService" else inv_service
        mock_client_getter.return_value = client

        # 1. Mock BillingSetup query
        bs_row = mock.MagicMock()
        bs_row.billing_setup.id = "98765"
        bs_row.billing_setup.status.name = "APPROVED"
        bs_row.billing_setup.payments_account_info.payments_account_id = "PAY-ACC-11"
        bs_row.billing_setup.payments_account_info.payments_account_name = "E-Commerce Store"
        ga_service.search.return_value = [bs_row]

        # 2. Mock Enum with strict spec to prevent swallowing regressions
        client.enums.MonthOfYearEnum = mock.MagicMock(spec=["OCTOBER"])
        client.enums.MonthOfYearEnum.OCTOBER = 11

        # 3. Mock Invoices
        mock_inv_resp = mock.MagicMock()
        mock_inv = mock.MagicMock()
        mock_inv.id = "INV-2026-10-001"
        mock_inv.type_.name = "INVOICE"
        mock_inv.issue_date = "2026-10-01"
        mock_inv.due_date = "2026-10-15"
        mock_inv.subtotal_amount_micros = 100_000_000
        mock_inv.tax_amount_micros = 19_000_000
        mock_inv.total_amount_micros = 119_000_000
        mock_inv.currency_code = "EUR"
        mock_inv.pdf_url = "https://google.com/invoices/INV-2026-10-001.pdf"
        mock_inv.service_date_range.start_date = "2026-09-01"
        mock_inv.service_date_range.end_date = "2026-09-30"
        mock_inv_resp.invoices = [mock_inv]
        inv_service.list_invoices.return_value = mock_inv_resp

        result = get_google_ads_invoices(year="2026", month="OCTOBER", customer_id="1234567890")
        self.assertTrue(result.success, f"Failed: {result.error}")
        self.assertEqual(len(result.data.billing_setups), 1)
        self.assertEqual(result.data.billing_setups[0].id, "98765")
        self.assertEqual(len(result.data.invoices), 1)
        self.assertEqual(result.data.invoices[0].id, "INV-2026-10-001")
        self.assertEqual(result.data.invoices[0].subtotal_eur, 100.0)
        self.assertEqual(result.data.invoices[0].tax_eur, 19.0)
        self.assertEqual(result.data.invoices[0].total_eur, 119.0)
        self.assertEqual(result.data.total_invoiced_eur, 119.0)
        self.assertEqual(result.data.total_tax_eur, 19.0)
        self.assertIn("119.00 EUR", result.data.notes[-1])

    def test_sync_shared_negative_list_contract(self):
        # 1. list_name cannot be empty
        with self.assertRaises(ValueError):
            SyncSharedNegativeListRequest(list_name="")

        # 2. invalid match_type rejected
        with self.assertRaises(ValueError):
            SyncSharedNegativeListRequest(list_name="Test Shield", match_type="INVALID_TYPE")

        # 3. Must be registered as MUTATION
        self.assertIn("sync_shared_negative_list", MUTATION_FUNCTIONS)

    @mock.patch("tools.negative_tools.get_ads_client")
    def test_sync_shared_negative_list_create_and_sync(self, mock_client_getter):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        shared_set_service = mock.MagicMock()
        shared_crit_service = mock.MagicMock()
        css_service = mock.MagicMock()

        def service_router(name):
            if name == "GoogleAdsService":
                return ga_service
            if name == "SharedSetService":
                return shared_set_service
            if name == "SharedCriterionService":
                return shared_crit_service
            if name == "CampaignSharedSetService":
                return css_service
            return mock.MagicMock()

        client.get_service.side_effect = service_router
        mock_client_getter.return_value = client

        # Strict Enum specs
        client.enums.SharedSetTypeEnum = mock.MagicMock(spec=["NEGATIVE_KEYWORDS"])
        client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS = 2
        client.enums.KeywordMatchTypeEnum = mock.MagicMock(spec=["EXACT", "PHRASE", "BROAD"])
        client.enums.KeywordMatchTypeEnum.BROAD = 2

        # 1. First search: no existing shared set
        ga_service.search.side_effect = [
            [],  # shared_set search -> not found
            []   # campaign_shared_set search -> no existing attachments
        ]

        # 2. Mock creation of shared set
        mock_create_set_res = mock.MagicMock()
        mock_create_set_res.resource_name = "customers/1234567890/sharedSets/99001"
        mock_set_resp = mock.MagicMock()
        mock_set_resp.results = [mock_create_set_res]
        shared_set_service.mutate_shared_sets.return_value = mock_set_resp

        # 3. Mock criterion mutation
        mock_crit_resp = mock.MagicMock()
        shared_crit_service.mutate_shared_criteria.return_value = mock_crit_resp

        # 4. Mock campaign shared set mutation
        mock_css_resp = mock.MagicMock()
        css_service.mutate_campaign_shared_sets.return_value = mock_css_resp

        req = SyncSharedNegativeListRequest(
            list_name="E-Commerce Universal Negatives",
            keywords=["obi", "hornbach"],
            match_type="BROAD",
            campaign_ids=["24015242821"]
        )

        result = sync_shared_negative_list(req=req, customer_id="1234567890")
        self.assertTrue(result.success, f"Failed: {result.error}")
        self.assertEqual(result.data.shared_set_id, "99001")
        self.assertEqual(result.data.shared_set_name, "E-Commerce Universal Negatives")
        self.assertEqual(result.data.action_taken, "CREATED_AND_SYNCED")
        self.assertEqual(result.data.added_keywords_count, 2)
        self.assertEqual(result.data.attached_campaign_ids, ["24015242821"])
        self.assertEqual(result.data.already_attached_campaign_ids, [])

        # Verify mutate calls
        shared_set_service.mutate_shared_sets.assert_called_once()
        shared_crit_service.mutate_shared_criteria.assert_called_once()
        css_service.mutate_campaign_shared_sets.assert_called_once()

    @mock.patch("tools.negative_tools.get_ads_client")
    def test_sync_shared_negative_list_idempotent_skip_duplicates(self, mock_client_getter):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        shared_set_service = mock.MagicMock()
        shared_crit_service = mock.MagicMock()
        css_service = mock.MagicMock()

        client.get_service.side_effect = lambda name: {
            "GoogleAdsService": ga_service,
            "SharedSetService": shared_set_service,
            "SharedCriterionService": shared_crit_service,
            "CampaignSharedSetService": css_service
        }[name]
        mock_client_getter.return_value = client

        # Strict Enum specs
        client.enums.SharedSetTypeEnum = mock.MagicMock(spec=["NEGATIVE_KEYWORDS"])
        client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS = 2
        client.enums.KeywordMatchTypeEnum = mock.MagicMock(spec=["EXACT", "PHRASE", "BROAD"])
        client.enums.KeywordMatchTypeEnum.BROAD = 2

        # 1. Existing shared set row
        set_row = mock.MagicMock()
        set_row.shared_set.id = 88002
        set_row.shared_set.name = "E-Commerce Universal Negatives"

        # 2. Existing criterion row ("obi" already exists with BROAD match)
        crit_row = mock.MagicMock()
        crit_row.shared_criterion.criterion_id = 7711
        crit_row.shared_criterion.keyword.text = "obi"
        crit_row.shared_criterion.keyword.match_type.name = "BROAD"

        # 3. Existing campaign shared set row (campaign 24015242821 already attached)
        css_row = mock.MagicMock()
        css_row.campaign_shared_set.campaign = "customers/1234567890/campaigns/24015242821"

        ga_service.search.side_effect = [
            [set_row],   # shared_set search
            [crit_row],  # shared_criterion search
            [css_row],   # campaign_shared_set search
            [set_row],   # 2nd test run: shared_set search
            [crit_row],  # 2nd test run: shared_criterion search
            [css_row]    # 2nd test run: campaign_shared_set search
        ]

        # Test A: Request has "obi" as BROAD (duplicate) and "ikea" as BROAD (new)
        req = SyncSharedNegativeListRequest(
            list_name="E-Commerce Universal Negatives",
            keywords=["obi", "ikea"],
            match_type="BROAD",
            campaign_ids=["24015242821"]
        )

        result = sync_shared_negative_list(req=req, customer_id="1234567890")
        self.assertTrue(result.success, f"Failed: {result.error}")
        self.assertEqual(result.data.shared_set_id, "88002")
        self.assertEqual(result.data.action_taken, "SYNCED_EXISTING")
        self.assertEqual(result.data.added_keywords_count, 1)  # only "ikea" added
        self.assertEqual(result.data.already_attached_campaign_ids, ["24015242821"])
        self.assertEqual(result.data.attached_campaign_ids, [])

        # Test B: Request has "obi" as EXACT (different match type: MUST be added, not skipped!)
        client.enums.KeywordMatchTypeEnum.EXACT = 4
        req_exact = SyncSharedNegativeListRequest(
            list_name="E-Commerce Universal Negatives",
            keywords=["obi"],
            match_type="EXACT",
            campaign_ids=["24015242821"]
        )
        result_exact = sync_shared_negative_list(req=req_exact, customer_id="1234567890")
        self.assertTrue(result_exact.success, f"Failed: {result_exact.error}")
        self.assertEqual(result_exact.data.added_keywords_count, 1, "Different match_type must not be skipped as duplicate")

        # shared_set_service.mutate_shared_sets should NOT be called (set already existed)
        shared_set_service.mutate_shared_sets.assert_not_called()
        # css_service.mutate_campaign_shared_sets should NOT be called (campaign already attached)
        css_service.mutate_campaign_shared_sets.assert_not_called()
        # shared_crit_service should be called twice (once for ikea BROAD, once for obi EXACT)
        self.assertEqual(shared_crit_service.mutate_shared_criteria.call_count, 2)

    @mock.patch("tools.merchant_tools.get_ads_client")
    def test_check_merchant_center_link_active(self, mock_client_getter):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        mock_client_getter.return_value = client

        # 1. ProductLink row: strict spec matching Google Ads API v23 protobuf
        mock_mc = mock.MagicMock(spec=["merchant_center_id"])
        mock_mc.merchant_center_id = 9876543210
        mock_type = mock.MagicMock(spec=["name"])
        mock_type.name = "MERCHANT_CENTER"
        mock_pl = mock.MagicMock(spec=["product_link_id", "type_", "merchant_center"])
        mock_pl.product_link_id = "112233"
        mock_pl.type_ = mock_type
        mock_pl.merchant_center = mock_mc
        pl_row = mock.MagicMock(spec=["product_link"])
        pl_row.product_link = mock_pl

        # 2. ProductLinkInvitation row: empty
        ga_service.search.side_effect = [
            [pl_row],  # product_link search
            []         # product_link_invitation search
        ]

        result = check_merchant_center_link(merchant_id="9876543210", customer_id="1234567890")
        self.assertTrue(result.success, f"Failed: {result.error}")
        self.assertTrue(result.data.is_linked)
        self.assertEqual(len(result.data.linked_accounts), 1)
        self.assertEqual(result.data.linked_accounts[0].product_link_id, "112233")
        self.assertEqual(result.data.linked_accounts[0].merchant_center_id, "9876543210")
        self.assertIn("успешно связан", result.data.audit_findings[0])

    @mock.patch("tools.merchant_tools.get_ads_client")
    def test_check_merchant_center_link_pending_and_missing(self, mock_client_getter):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        mock_client_getter.return_value = client

        # Scenario 1: Pending invitation (strict spec with real v23 PENDING_APPROVAL status)
        mock_mci = mock.MagicMock(spec=["merchant_center_id"])
        mock_mci.merchant_center_id = 9876543210
        mock_type = mock.MagicMock(spec=["name"])
        mock_type.name = "MERCHANT_CENTER"
        mock_status = mock.MagicMock(spec=["name"])
        mock_status.name = "PENDING_APPROVAL"
        mock_pli = mock.MagicMock(spec=["product_link_invitation_id", "status", "type_", "merchant_center"])
        mock_pli.product_link_invitation_id = "998877"
        mock_pli.status = mock_status
        mock_pli.type_ = mock_type
        mock_pli.merchant_center = mock_mci
        inv_row = mock.MagicMock(spec=["product_link_invitation"])
        inv_row.product_link_invitation = mock_pli

        ga_service.search.side_effect = [
            [],         # product_link search (not yet linked)
            [inv_row]   # product_link_invitation search (invitation pending)
        ]

        result = check_merchant_center_link(merchant_id="9876543210", customer_id="1234567890")
        self.assertTrue(result.success)
        self.assertFalse(result.data.is_linked)
        self.assertEqual(len(result.data.pending_invitations), 1)
        self.assertEqual(result.data.pending_invitations[0].status, "PENDING_APPROVAL")
        self.assertIn("ожидающее приглашение", result.data.audit_findings[0])

        # Scenario 2: Completely missing
        ga_service.search.side_effect = [[], []]
        result2 = check_merchant_center_link(merchant_id="9876543210", customer_id="1234567890")
        self.assertTrue(result2.success)
        self.assertFalse(result2.data.is_linked)
        self.assertIn("НЕ связан", result2.data.audit_findings[0])

if __name__ == "__main__":
    unittest.main()



