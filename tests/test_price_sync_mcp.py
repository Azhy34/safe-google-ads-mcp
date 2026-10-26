"""
Offline unit & contract tests for the Google Ads Smart Facade MCP tool: sync_price_extension.
Runs fully offline with mocked Google Ads API protobuf services and redirected audit trail.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -v
"""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from pydantic import ValidationError

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from shield import error_shield, cloud_sink
from shield.error_shield import build_request, MUTATION_FUNCTIONS
from shield.models_asset import (
    SyncPriceExtensionRequest,
    PriceOfferingItem,
    CreatePriceAssetRequest
)
from tools.asset_tools import sync_price_extension


class AuditLogCase(unittest.TestCase):
    """Redirects the audit trail to a temporary file and disables live cloud sink."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        patcher = mock.patch.object(error_shield, "AUDIT_FILE", self.audit)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Disable real cloud sink emission during tests
        cloud = mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None})
        cloud.start()
        self.addCleanup(cloud.stop)

        # Ensure preflight check requirement doesn't block offline unit tests
        preflight_env = mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"})
        preflight_env.start()
        self.addCleanup(preflight_env.stop)

        self.addCleanup(self.tmp.cleanup)

    def log_entries(self):
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines() if line.strip()]


class SyncPriceExtensionContractTests(unittest.TestCase):
    """Pydantic V2 data contracts validation tests."""

    def test_valid_request(self):
        req = SyncPriceExtensionRequest(
            entity_id="24015242821",
            level="CAMPAIGN",
            name="Woodland Preise Test",
            offerings=[
                PriceOfferingItem(header="150x250 cm", description="Vliestapete", price_eur=149.00, final_url="https://example.com/1"),
                PriceOfferingItem(header="200x250 cm", description="Vliestapete", price_eur=199.00, final_url="https://example.com/2"),
                PriceOfferingItem(header="250x250 cm", description="Vliestapete", price_eur=249.00, final_url="https://example.com/3"),
            ]
        )
        self.assertEqual(req.entity_id, "24015242821")
        self.assertEqual(req.level, "CAMPAIGN")
        self.assertEqual(len(req.offerings), 3)
        self.assertEqual(req.price_type, "PRODUCT_TIERS")
        self.assertEqual(req.price_qualifier, "FROM")
        self.assertEqual(req.language_code, "de")

    def test_rejects_under_3_offerings(self):
        with self.assertRaises(ValidationError) as ctx:
            SyncPriceExtensionRequest(
                entity_id="24015242821",
                name="Too few offerings",
                offerings=[
                    PriceOfferingItem(header="150x250 cm", description="Vliestapete", price_eur=149.00, final_url="https://example.com/1"),
                    PriceOfferingItem(header="200x250 cm", description="Vliestapete", price_eur=199.00, final_url="https://example.com/2"),
                ]
            )
        errors = ctx.exception.errors()
        self.assertTrue(any("offerings" in str(err["loc"]) for err in errors))

    def test_rejects_over_8_offerings(self):
        items = [
            PriceOfferingItem(header=f"Tier {i}", description="Vliestapete", price_eur=100.0 + i, final_url="https://example.com")
            for i in range(9)
        ]
        with self.assertRaises(ValidationError) as ctx:
            SyncPriceExtensionRequest(
                entity_id="24015242821",
                name="Too many offerings",
                offerings=items
            )
        errors = ctx.exception.errors()
        self.assertTrue(any("offerings" in str(err["loc"]) for err in errors))

    def test_rejects_invalid_level(self):
        with self.assertRaises(ValidationError):
            SyncPriceExtensionRequest(
                entity_id="24015242821",
                level="INVALID_LEVEL",  # only CUSTOMER, CAMPAIGN, AD_GROUP allowed
                name="Invalid level",
                offerings=[
                    PriceOfferingItem(header="1", description="desc", price_eur=10.0, final_url="https://example.com"),
                    PriceOfferingItem(header="2", description="desc", price_eur=20.0, final_url="https://example.com"),
                    PriceOfferingItem(header="3", description="desc", price_eur=30.0, final_url="https://example.com"),
                ]
            )


class ShieldRejectionTests(AuditLogCase):
    """Validates build_request intercept and audit trail rejection logging."""

    def test_build_request_rejection_logs_mutation(self):
        req, rejected_json = build_request(
            "sync_price_extension",
            SyncPriceExtensionRequest,
            action_type="MUTATION",
            entity_id="24015242821",
            level="CAMPAIGN",
            name="Empty offerings test",
            offerings=[]
        )
        self.assertIsNone(req)
        self.assertIsNotNone(rejected_json)
        
        data = json.loads(rejected_json)
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "REQUEST_REJECTED")
        self.assertIn("offerings", data["error"]["field_path"])

        # Check local audit trail
        logs = self.log_entries()
        self.assertEqual(len(logs), 1)
        entry = logs[0]
        self.assertEqual(entry["function"], "sync_price_extension")
        self.assertEqual(entry["action_type"], "MUTATION")
        self.assertEqual(entry["error_code"], "REQUEST_REJECTED")
        self.assertIn("offerings", entry["field_path"])

    def test_mutation_functions_contains_price_tools(self):
        self.assertIn("sync_price_extension", MUTATION_FUNCTIONS)
        self.assertIn("create_price_asset", MUTATION_FUNCTIONS)


class SmartFacadeLogicTests(AuditLogCase):
    """Mocks Google Ads API services to test atomic create, discover, unlink, and link flow."""

    def _setup_mock_client(self):
        client = mock.MagicMock()

        # Enums
        client.enums.AssetTypeEnum.PRICE = "PRICE"
        client.enums.PriceExtensionTypeEnum.PRODUCT_TIERS = "PRODUCT_TIERS"
        client.enums.PriceExtensionPriceQualifierEnum.FROM = "FROM"
        client.enums.PriceExtensionPriceUnitEnum.UNSPECIFIED = "UNSPECIFIED"
        client.enums.AssetFieldTypeEnum.PRICE = "PRICE"

        # Types
        def get_type_mock(type_name):
            obj = mock.MagicMock()
            if type_name == "AssetOperation":
                obj.create = mock.MagicMock()
                obj.create.price_asset.price_offerings = []
            elif type_name == "PriceOffering":
                obj.price = mock.MagicMock()
            elif type_name in ("CampaignAssetOperation", "AdGroupAssetOperation", "CustomerAssetOperation"):
                obj.create = mock.MagicMock()
                obj.remove = None
            return obj

        client.get_type.side_effect = get_type_mock

        # Services
        asset_service = mock.MagicMock()
        asset_service.asset_path.side_effect = lambda cid, aid: f"customers/{cid}/assets/{aid}"
        asset_service.mutate_assets.return_value = mock.MagicMock(
            results=[mock.MagicMock(resource_name="customers/1234567890/assets/NEW_ASSET_777")]
        )

        ga_service = mock.MagicMock()

        camp_service = mock.MagicMock()
        camp_service.campaign_path.side_effect = lambda cid, c_id: f"customers/{cid}/campaigns/{c_id}"

        ag_service = mock.MagicMock()
        ag_service.ad_group_path.side_effect = lambda cid, ag_id: f"customers/{cid}/adGroups/{ag_id}"

        cust_service = mock.MagicMock()

        def get_service_mock(svc_name):
            if svc_name == "AssetService":
                return asset_service
            elif svc_name == "GoogleAdsService":
                return ga_service
            elif svc_name == "CampaignAssetService":
                return camp_service
            elif svc_name == "AdGroupAssetService":
                return ag_service
            elif svc_name == "CustomerAssetService":
                return cust_service
            return mock.MagicMock()

        client.get_service.side_effect = get_service_mock
        return client, ga_service, asset_service, camp_service, ag_service, cust_service

    def test_sync_price_extension_campaign_level(self):
        client, ga_service, asset_service, camp_service, _, _ = self._setup_mock_client()

        # Simulate 2 existing active Price Assets on Campaign
        row1 = mock.MagicMock()
        row1.campaign_asset.asset = "customers/1234567890/assets/OLD_ASSET_111"
        row1.campaign_asset.resource_name = "customers/1234567890/campaignAssets/24015~OLD_ASSET_111~PRICE"

        row2 = mock.MagicMock()
        row2.campaign_asset.asset = "customers/1234567890/assets/OLD_ASSET_222"
        row2.campaign_asset.resource_name = "customers/1234567890/campaignAssets/24015~OLD_ASSET_222~PRICE"

        ga_service.search.return_value = [row1, row2]

        req = SyncPriceExtensionRequest(
            entity_id="24015242821",
            level="CAMPAIGN",
            name="Woodland New Prices",
            offerings=[
                PriceOfferingItem(header="150x250 cm", description="Vliestapete", price_eur=149.00, final_url="https://example.com/1"),
                PriceOfferingItem(header="200x250 cm", description="Vliestapete", price_eur=199.00, final_url="https://example.com/2"),
                PriceOfferingItem(header="250x250 cm", description="Vliestapete", price_eur=249.00, final_url="https://example.com/3"),
            ]
        )

        with mock.patch("tools.asset_tools.get_ads_client", return_value=client):
            result = sync_price_extension(req, customer_id="1234567890")

        # Unwrap ServiceResult if wrapped
        result_text = result.data if hasattr(result, "data") else result

        # Verify creation
        asset_service.mutate_assets.assert_called_once()
        self.assertIn("Smart Facade SUCCESS", result_text)
        self.assertIn("Created Asset NEW_ASSET_777", result_text)
        self.assertIn("Unlinked 2 old asset(s)", result_text)
        self.assertIn("OLD_ASSET_111", result_text)
        self.assertIn("OLD_ASSET_222", result_text)

        # Verify mutate_campaign_assets was called twice: once for removal, once for linking
        self.assertEqual(camp_service.mutate_campaign_assets.call_count, 2)
        remove_call_ops = camp_service.mutate_campaign_assets.call_args_list[0][1]["operations"]
        self.assertEqual(len(remove_call_ops), 2)
        link_call_ops = camp_service.mutate_campaign_assets.call_args_list[1][1]["operations"]
        self.assertEqual(len(link_call_ops), 1)

    def test_sync_price_extension_ad_group_level(self):
        client, ga_service, asset_service, _, ag_service, _ = self._setup_mock_client()

        # Zero existing assets on AdGroup
        ga_service.search.return_value = []

        req = SyncPriceExtensionRequest(
            entity_id="197094455654",
            level="AD_GROUP",
            name="Jungle AdGroup Prices",
            offerings=[
                PriceOfferingItem(header="Tier 1", description="desc", price_eur=110.0, final_url="https://example.com/1"),
                PriceOfferingItem(header="Tier 2", description="desc", price_eur=120.0, final_url="https://example.com/2"),
                PriceOfferingItem(header="Tier 3", description="desc", price_eur=130.0, final_url="https://example.com/3"),
            ]
        )

        with mock.patch("tools.asset_tools.get_ads_client", return_value=client):
            result = sync_price_extension(req, customer_id="1234567890")

        result_text = result.data if hasattr(result, "data") else result
        self.assertIn("Created Asset NEW_ASSET_777", result_text)
        self.assertIn("Unlinked 0 old asset(s)", result_text)
        
        # Only link operation called (no remove operation needed)
        self.assertEqual(ag_service.mutate_ad_group_assets.call_count, 1)

    def test_sync_price_extension_customer_level(self):
        client, ga_service, asset_service, _, _, cust_service = self._setup_mock_client()

        # 1 old customer asset
        row = mock.MagicMock()
        row.customer_asset.asset = "customers/1234567890/assets/OLD_CUST_999"
        row.customer_asset.resource_name = "customers/1234567890/customerAssets/OLD_CUST_999~PRICE"
        ga_service.search.return_value = [row]

        req = SyncPriceExtensionRequest(
            entity_id="1234567890",
            level="CUSTOMER",
            name="Global Account Prices",
            offerings=[
                PriceOfferingItem(header="Tier 1", description="desc", price_eur=100.0, final_url="https://example.com/1"),
                PriceOfferingItem(header="Tier 2", description="desc", price_eur=200.0, final_url="https://example.com/2"),
                PriceOfferingItem(header="Tier 3", description="desc", price_eur=300.0, final_url="https://example.com/3"),
            ]
        )

        with mock.patch("tools.asset_tools.get_ads_client", return_value=client):
            result = sync_price_extension(req, customer_id="1234567890")

        result_text = result.data if hasattr(result, "data") else result
        self.assertIn("Unlinked 1 old asset(s)", result_text)
        self.assertIn("OLD_CUST_999", result_text)
        self.assertEqual(cust_service.mutate_customer_assets.call_count, 2)


class ServerRegistrationConsistencyTests(unittest.TestCase):
    """Ensures server.py registers the tool and schema is present in MCP catalog."""

    def test_server_registers_sync_price_extension(self):
        import server
        tools = server.mcp._tool_manager._tools
        self.assertIn("sync_price_extension", tools)
        self.assertIn("create_price_asset", tools)
        self.assertIn("create_search_campaign", tools)
        self.assertIn("preview_sitelink_assets", tools)
        self.assertIn("create_sitelink_assets", tools)
        self.assertIn("check_enhanced_conversions_status", tools)
        self.assertIn("get_demand_gen_currency_floor", tools)
        self.assertIn("audit_geo_physical_locations", tools)
        self.assertIn("upload_click_conversion", tools)
        self.assertIn("get_google_ads_invoices", tools)
        self.assertIn("sync_shared_negative_list", tools)
        self.assertIn("check_merchant_center_link", tools)
        self.assertIn("create_callout_assets", tools)
        self.assertIn("create_structured_snippet_asset", tools)
        self.assertEqual(len(tools), 53)

    def test_schema_file_exists_and_matches(self):
        schema_path = Path("C:/Users/Mikhail/.gemini/antigravity-ide/mcp/google-ads-mcp/sync_price_extension.json")
        self.assertTrue(schema_path.exists(), "Schema file sync_price_extension.json must exist in MCP directory")
        
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["name"], "sync_price_extension")
        props = schema["parameters"]["properties"]
        self.assertIn("entity_id", props)
        self.assertIn("name", props)
        self.assertIn("offerings", props)
        self.assertIn("level", props)
        self.assertEqual(props["level"]["default"], "CAMPAIGN")
        self.assertEqual(schema["parameters"]["required"], ["entity_id", "name", "offerings"])


if __name__ == "__main__":
    unittest.main()
