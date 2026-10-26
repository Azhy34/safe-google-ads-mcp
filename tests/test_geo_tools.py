import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from shield import error_shield, cloud_sink
from shield.models_geo import (
    CountryLocationTrafficRecord, CampaignGeoLeakRecord, GeoPhysicalAuditResponse
)
from tools.geo_tools import audit_geo_physical_locations, KNOWN_GEO_CONSTANTS

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

class TestGeoTools(AuditLogCase):
    def test_geo_models(self):
        rec = CountryLocationTrafficRecord(
            country_criterion_id="2276",
            country_code="DE",
            country_name="Germany",
            status="TARGETED",
            impressions=1000,
            clicks=50,
            cost_eur=25.50,
            conversions=2.0,
            ctr_percent=5.0
        )
        self.assertEqual(rec.country_code, "DE")
        self.assertEqual(rec.status, "TARGETED")
        self.assertEqual(rec.cost_eur, 25.50)

    @mock.patch("tools.geo_tools.get_ads_client")
    def test_audit_geo_clean_traffic(self, mock_client_getter):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        mock_client_getter.return_value = client

        row_de = mock.MagicMock()
        row_de.campaign.id = 111
        row_de.campaign.name = "Core High Intent Search"
        row_de.user_location_view.country_criterion_id = 2276  # Germany
        row_de.metrics.impressions = 200
        row_de.metrics.clicks = 10
        row_de.metrics.cost_micros = 5000000  # 5.00 EUR
        row_de.metrics.conversions = 1.0

        ga_service.search.return_value = [row_de]

        result = audit_geo_physical_locations(days=30, customer_id="1234567890")
        self.assertTrue(result.success)
        self.assertEqual(result.data.total_spend_eur, 5.00)
        self.assertEqual(result.data.leaked_spend_eur, 0.0)
        self.assertEqual(result.data.leak_percentage, 0.0)
        self.assertEqual(len(result.data.campaign_leaks), 0)

    @mock.patch("tools.geo_tools.get_ads_client")
    def test_audit_geo_detects_foreign_leak(self, mock_client_getter):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        mock_client_getter.return_value = client

        # 1 target row (DE)
        row_de = mock.MagicMock()
        row_de.campaign.id = 111
        row_de.campaign.name = "Core High Intent Search"
        row_de.user_location_view.country_criterion_id = 2276
        row_de.metrics.impressions = 100
        row_de.metrics.clicks = 5
        row_de.metrics.cost_micros = 2500000  # 2.50 EUR
        row_de.metrics.conversions = 0.0

        # 1 leak row (US)
        row_us = mock.MagicMock()
        row_us.campaign.id = 111
        row_us.campaign.name = "Core High Intent Search"
        row_us.user_location_view.country_criterion_id = 2840  # United States
        row_us.metrics.impressions = 50
        row_us.metrics.clicks = 3
        row_us.metrics.cost_micros = 1500000  # 1.50 EUR
        row_us.metrics.conversions = 0.0

        ga_service.search.return_value = [row_de, row_us]

        result = audit_geo_physical_locations(days=30, customer_id="1234567890")
        self.assertTrue(result.success)
        self.assertEqual(result.data.total_spend_eur, 4.00)
        self.assertEqual(result.data.target_geo_spend_eur, 2.50)
        self.assertEqual(result.data.leaked_spend_eur, 1.50)
        self.assertEqual(result.data.leak_percentage, 37.5)
        self.assertEqual(len(result.data.campaign_leaks), 1)
        self.assertEqual(result.data.campaign_leaks[0].campaign_name, "Core High Intent Search")
        self.assertIn("United States (US)", result.data.campaign_leaks[0].leaked_countries)

if __name__ == "__main__":
    unittest.main()
