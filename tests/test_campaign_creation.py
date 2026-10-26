"""
Offline unit & contract tests for the create_search_campaign tool.
Runs fully offline with mocked Google Ads API protobuf services and redirected audit trail.
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
from shield.error_shield import build_request
from shield.models_campaign import CreateSearchCampaignRequest, CreateCampaignResponse
from tools.campaign_tools import create_search_campaign


class AuditLogCase(unittest.TestCase):
    """Redirects the audit trail to a temporary file and disables live cloud sink."""

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

    def log_entries(self):
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines() if line.strip()]


class CreateSearchCampaignContractTests(unittest.TestCase):
    """Pydantic V2 data contracts validation tests."""

    def test_valid_request_with_defaults(self):
        req = CreateSearchCampaignRequest(
            name="E-Commerce Search DE+AT - Category",
            ad_group_name="Organic Cotton Hoodie - Landing Category"
        )
        self.assertEqual(req.name, "E-Commerce Search DE+AT - Category")
        self.assertEqual(req.budget_eur, 3.0)
        self.assertEqual(req.cpc_ceiling_eur, 0.55)
        self.assertEqual(req.status, "PAUSED")
        self.assertEqual(req.geo_target_constants, [2276, 2040])
        self.assertEqual(req.language_constants, [1001])
        self.assertEqual(req.keywords, [])

    def test_custom_language_ab_test_request(self):
        req = CreateSearchCampaignRequest(
            name="E-Commerce Search DE+AT - Category EN",
            ad_group_name="Woodland Wallpaper - Expats",
            language_constants=[1001, 1000],
            keywords=["[woodland wallpaper]", "\"nursery wallpaper\""]
        )
        self.assertEqual(req.language_constants, [1001, 1000])
        self.assertEqual(len(req.keywords), 2)

    def test_invalid_budget_bounds(self):
        with self.assertRaises(ValidationError):
            CreateSearchCampaignRequest(name="Test", ad_group_name="AG", budget_eur=0.5)
        with self.assertRaises(ValidationError):
            CreateSearchCampaignRequest(name="Test", ad_group_name="AG", budget_eur=101.0)

    def test_invalid_cpc_ceiling_bounds(self):
        with self.assertRaises(ValidationError):
            CreateSearchCampaignRequest(name="Test", ad_group_name="AG", cpc_ceiling_eur=0.05)
        with self.assertRaises(ValidationError):
            CreateSearchCampaignRequest(name="Test", ad_group_name="AG", cpc_ceiling_eur=5.0)


class CreateSearchCampaignExecutionTests(AuditLogCase):
    """Execution logic tests with mocked Google Ads API services."""

    @mock.patch("tools.campaign_tools.get_ads_client")
    def test_successful_campaign_creation_flow(self, mock_client_getter):
        mock_client = mock.MagicMock()
        mock_client_getter.return_value = mock_client

        # Mock Budget Service
        mock_budget_service = mock.MagicMock()
        mock_budget_res = mock.MagicMock()
        mock_budget_item = mock.MagicMock()
        mock_budget_item.resource_name = "customers/1234567890/campaignBudgets/999111"
        mock_budget_res.results = [mock_budget_item]
        mock_budget_service.mutate_campaign_budgets.return_value = mock_budget_res

        # Mock Campaign Service
        mock_campaign_service = mock.MagicMock()
        mock_campaign_res = mock.MagicMock()
        mock_campaign_item = mock.MagicMock()
        mock_campaign_item.resource_name = "customers/1234567890/campaigns/999222"
        mock_campaign_res.results = [mock_campaign_item]
        mock_campaign_service.mutate_campaigns.return_value = mock_campaign_res

        # Mock Criteria Service
        mock_crit_service = mock.MagicMock()

        # Mock Ad Group Service
        mock_ag_service = mock.MagicMock()
        mock_ag_res = mock.MagicMock()
        mock_ag_item = mock.MagicMock()
        mock_ag_item.resource_name = "customers/1234567890/adGroups/999333"
        mock_ag_res.results = [mock_ag_item]
        mock_ag_service.mutate_ad_groups.return_value = mock_ag_res

        # Mock Ad Group Criteria Service
        mock_agc_service = mock.MagicMock()

        def get_service_side_effect(service_name):
            services = {
                "CampaignBudgetService": mock_budget_service,
                "CampaignService": mock_campaign_service,
                "CampaignCriterionService": mock_crit_service,
                "AdGroupService": mock_ag_service,
                "AdGroupCriterionService": mock_agc_service,
            }
            return services[service_name]

        mock_client.get_service.side_effect = get_service_side_effect

        req = CreateSearchCampaignRequest(
            name="E-Commerce Search DE+AT - Category",
            ad_group_name="Organic Cotton Hoodie - Landing Category",
            budget_eur=3.0,
            cpc_ceiling_eur=0.55,
            status="PAUSED",
            keywords=["[fototapete wald kinderzimmer]", "\"tapete kinderzimmer wald\""]
        )

        res = create_search_campaign(req)

        self.assertTrue(res.success)
        self.assertEqual(res.data.campaign_id, "999222")
        self.assertEqual(res.data.budget_id, "999111")
        self.assertEqual(res.data.ad_group_id, "999333")
        self.assertEqual(res.data.status, "PAUSED")
        self.assertEqual(res.data.cpc_ceiling_eur, 0.55)
        self.assertEqual(res.data.keywords_count, 2)

        # Verify Budget mutation was called
        mock_budget_service.mutate_campaign_budgets.assert_called_once()
        # Verify Campaign mutation was called
        mock_campaign_service.mutate_campaigns.assert_called_once()
        # Verify Criteria mutation was called (2 locations + 1 language + 14 schedules = 17 ops)
        crit_ops = mock_crit_service.mutate_campaign_criteria.call_args[1]["operations"]
        self.assertEqual(len(crit_ops), 17)
        # Verify Ad Group was created
        mock_ag_service.mutate_ad_groups.assert_called_once()
        # Verify 2 keywords were added
        mock_agc_service.mutate_ad_group_criteria.assert_called_once()


if __name__ == "__main__":
    unittest.main()
