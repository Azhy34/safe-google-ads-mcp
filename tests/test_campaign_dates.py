"""
Offline tests for set_campaign_dates / update_campaign_dates: Pydantic validation,
dry_run preview, explicit update_mask, read-back verification, clear_end_date sentinel,
NOT_APPLIED / CAMPAIGN_NOT_FOUND handling, and telemetry/audit logging.
All Google Ads services are mocked; audit output goes to a temporary file.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_campaign_dates.py" -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
import policy_env  # noqa: F401  (pins the fictional test policy before anything reads it)

from shield import cloud_sink, error_shield
from shield.error_shield import MUTATION_FUNCTIONS, build_request
from shield.models_campaign import CampaignDatesUpdateRequest, NO_END_DATE
from tools.campaign_tools import update_campaign_dates
from tools.eval_tools import LEVER_GROUPS

CAMPAIGN = "2000000001"
FUTURE_END = "2030-11-30"
FUTURE_START = "2030-11-15"


class AuditLogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        for patcher in (
            mock.patch.object(error_shield, "AUDIT_FILE", self.audit),
            mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
            mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def log_entries(self):
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines() if line.strip()]


class CampaignDatesContractTests(unittest.TestCase):
    def test_valid_requests_and_registries(self):
        req1 = CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, end_date=f" {FUTURE_END} ")
        self.assertEqual(req1.end_date, FUTURE_END)
        req2 = CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, clear_end_date=True)
        self.assertTrue(req2.clear_end_date)
        req3 = CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, start_date=FUTURE_START, end_date=FUTURE_END)
        self.assertEqual((req3.start_date, req3.end_date), (FUTURE_START, FUTURE_END))
        self.assertIn("update_campaign_dates", MUTATION_FUNCTIONS)
        self.assertIn("update_campaign_dates", LEVER_GROUPS["status"])

    def test_rejects_invalid_dates_missing_fields_and_conflicts(self):
        # Missing all date arguments
        with self.assertRaises(ValidationError):
            CampaignDatesUpdateRequest(campaign_id=CAMPAIGN)
        # Mutually exclusive end_date and clear_end_date
        with self.assertRaises(ValidationError):
            CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, end_date=FUTURE_END, clear_end_date=True)
        # Past end_date
        with self.assertRaises(ValidationError):
            CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, end_date="2020-01-01")
        # Exceeding Google Ads maximum 2037-12-30
        with self.assertRaises(ValidationError):
            CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, end_date="2038-01-01")
        # end_date earlier than start_date
        with self.assertRaises(ValidationError):
            CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, start_date=FUTURE_END, end_date=FUTURE_START)
        # Invalid date format
        with self.assertRaises(ValidationError):
            CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, end_date="30-11-2030")
        # Non-numeric campaign_id
        with self.assertRaises(ValidationError):
            CampaignDatesUpdateRequest(campaign_id="abc", end_date=FUTURE_END)


class FakeCampaignDatesAds:
    def __init__(self, exists=True, initial_start="2026-08-01 00:00:00", initial_end="2037-12-30 23:59:59", apply_changes=True):
        self.exists = exists
        self.start_dt = initial_start
        self.end_dt = initial_end
        self.apply_changes = apply_changes
        self.mutate_ops = []

        self.client = mock.MagicMock()
        self.ga = mock.MagicMock()
        self.ga.search.side_effect = self._search
        self.camp_svc = mock.MagicMock()
        self.camp_svc.campaign_path.side_effect = lambda cid, camp_id: f"customers/{cid}/campaigns/{camp_id}"
        self.camp_svc.mutate_campaigns.side_effect = self._mutate_campaigns
        self.client.get_type.side_effect = self._get_type
        self.client.get_service.side_effect = lambda name: {
            "GoogleAdsService": self.ga,
            "CampaignService": self.camp_svc,
        }.get(name, mock.MagicMock())

    def _get_type(self, name):
        if name == "CampaignOperation":
            return SimpleNamespace(
                update=SimpleNamespace(resource_name="", start_date_time="", end_date_time=""),
                update_mask=SimpleNamespace(paths=[]),
            )
        return mock.MagicMock()

    def _mutate_campaigns(self, customer_id, operations):
        self.mutate_ops.extend(operations)
        if self.apply_changes:
            op = operations[0]
            if "start_date_time" in op.update_mask.paths:
                self.start_dt = op.update.start_date_time
            if "end_date_time" in op.update_mask.paths:
                self.end_dt = op.update.end_date_time
        return mock.MagicMock()

    def _search(self, customer_id, query):
        if not self.exists:
            return []
        return [
            SimpleNamespace(
                campaign=SimpleNamespace(
                    id=int(CAMPAIGN),
                    name="ExampleShop Search Campaign",
                    status=SimpleNamespace(name="PAUSED"),
                    start_date_time=self.start_dt,
                    end_date_time=self.end_dt,
                )
            )
        ]

    def patched(self):
        return mock.patch("tools.campaign_tools.get_ads_client", return_value=self.client)


class CampaignDatesToolTests(AuditLogCase):
    def test_dry_run_previews_without_mutating(self):
        ads = FakeCampaignDatesAds()
        with ads.patched():
            result = update_campaign_dates(
                CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, end_date=FUTURE_END, dry_run=True),
                customer_id="1112223334",
            )
        self.assertTrue(result.success)
        self.assertIn("DRY RUN:", result.data)
        self.assertIn(f"{FUTURE_END} 23:59:59", result.data)
        ads.camp_svc.mutate_campaigns.assert_not_called()
        entry = self.log_entries()[-1]
        self.assertEqual((entry["function"], entry["action_type"], entry["success"]), ("update_campaign_dates", "MUTATION", True))

    def test_updates_end_date_with_explicit_mask_reads_back_and_logs_telemetry(self):
        ads = FakeCampaignDatesAds()
        with ads.patched():
            result = update_campaign_dates(
                CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, end_date=FUTURE_END),
                customer_id="1112223334",
            )
        self.assertTrue(result.success)
        self.assertIn(f"end_date_time={FUTURE_END} 23:59:59", result.data)
        op = ads.mutate_ops[0]
        self.assertEqual(op.update_mask.paths, ["end_date_time"])
        self.assertEqual(op.update.end_date_time, f"{FUTURE_END} 23:59:59")
        self.assertIsNotNone(result.telemetry.trace_id)
        self.assertIsNotNone(result.telemetry.span_id)
        entry = self.log_entries()[-1]
        self.assertEqual(
            (entry["function"], entry["action_type"], entry["severity"], entry["success"]),
            ("update_campaign_dates", "MUTATION", "NOTICE", True),
        )
        self.assertIn(FUTURE_END, entry["result"])

    def test_clear_end_date_sets_google_sentinel_and_reads_back(self):
        ads = FakeCampaignDatesAds(initial_end=f"{FUTURE_END} 23:59:59")
        with ads.patched():
            result = update_campaign_dates(
                CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, clear_end_date=True),
                customer_id="1112223334",
            )
        self.assertTrue(result.success)
        self.assertIn(f"end_date_time={NO_END_DATE} 23:59:59", result.data)
        self.assertEqual(ads.mutate_ops[0].update_mask.paths, ["end_date_time"])

    def test_read_back_mismatch_returns_not_applied(self):
        ads = FakeCampaignDatesAds(apply_changes=False)
        with ads.patched():
            result = update_campaign_dates(
                CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, end_date=FUTURE_END),
                customer_id="1112223334",
            )
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "NOT_APPLIED")

    def test_unknown_campaign_refuses_without_mutating(self):
        ads = FakeCampaignDatesAds(exists=False)
        with ads.patched():
            result = update_campaign_dates(
                CampaignDatesUpdateRequest(campaign_id=CAMPAIGN, end_date=FUTURE_END),
                customer_id="1112223334",
            )
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "CAMPAIGN_NOT_FOUND")
        ads.camp_svc.mutate_campaigns.assert_not_called()

    def test_server_registration_and_shield_rejection(self):
        import server
        tool = server.mcp._tool_manager._tools["set_campaign_dates"]
        self.assertTrue({"campaign_id", "end_date", "start_date", "clear_end_date", "dry_run"}
                        <= set(tool.parameters["properties"]))
        with mock.patch("tools.campaign_tools.get_ads_client", side_effect=AssertionError("Google must not be called")):
            answer = json.loads(server.set_campaign_dates(campaign_id=CAMPAIGN, end_date="2020-01-01"))
        self.assertFalse(answer["success"])
        self.assertEqual(answer["error"]["code"], "REQUEST_REJECTED")
        entry = self.log_entries()[-1]
        self.assertEqual((entry["function"], entry["error_code"]), ("update_campaign_dates", "REQUEST_REJECTED"))


if __name__ == "__main__":
    unittest.main()
