"""
set_conversion_action_goal: switching a conversion action to Secondary must reach Google and be read back.

30.10.2026 the tool built its update mask with protobuf_helpers.field_mask(None, ...), which leaves out a field set to
its default: primary_for_goal=False went to Google as an update of nothing, and the tool answered "successfully updated"
while all actions stayed Primary. The operation here is the real Google Ads type, so the mask is the one Google gets.
"""
import importlib
import os
import pkgutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
import policy_env  # noqa: F401  (pins the fictional test policy before anything reads it)

import google.ads.googleads as googleads
from shield import error_shield, cloud_sink
from shield.models_conversion import SetConversionActionGoalRequest
from tools.conversion_tools import set_conversion_action_goal

VERSION = sorted(m.name for m in pkgutil.iter_modules(googleads.__path__) if m.name.startswith("v"))[-1]
ConversionActionOperation = importlib.import_module(
    f"google.ads.googleads.{VERSION}.services.types.conversion_action_service").ConversionActionOperation


class ConversionGoalTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(error_shield, "AUDIT_FILE", Path(tmp.name) / "audit.jsonl"),
                        mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
                        mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, primary_after):
        """A client whose ConversionActionOperation is the real type and whose read-back says primary_after."""
        client = mock.MagicMock()
        client.get_type.side_effect = lambda name: ConversionActionOperation()
        ca_service = mock.MagicMock()
        ca_service.conversion_action_path.side_effect = lambda cid, aid: f"customers/{cid}/conversionActions/{aid}"
        ca_service.mutate_conversion_actions.return_value = mock.MagicMock(
            results=[mock.MagicMock(resource_name="customers/1/conversionActions/7873272910")])
        row = mock.MagicMock()
        row.conversion_action.primary_for_goal = primary_after
        row.conversion_action.status.name = "ENABLED"
        ga_service = mock.MagicMock()
        ga_service.search.return_value = [row]
        client.get_service.side_effect = lambda name: ca_service if name == "ConversionActionService" else ga_service
        return client, ca_service

    def run_tool(self, client, primary):
        req = SetConversionActionGoalRequest(conversion_action_id="7873272910", primary_for_goal=primary)
        with mock.patch("tools.conversion_tools.get_ads_client", return_value=client):
            return set_conversion_action_goal(req, customer_id="1")

    def test_secondary_is_in_the_update_mask(self):
        client, ca_service = self.client(primary_after=False)
        result = self.run_tool(client, primary=False)
        operation = ca_service.mutate_conversion_actions.call_args.kwargs["operations"][0]
        self.assertIn("primary_for_goal", list(operation.update_mask.paths))
        self.assertTrue(result.success)
        self.assertFalse(result.data.primary_for_goal)

    def test_an_update_google_did_not_apply_is_a_failure(self):
        client, _ = self.client(primary_after=True)
        result = self.run_tool(client, primary=False)
        self.assertFalse(result.success)
        self.assertIn("NOT_APPLIED", result.error.message)


if __name__ == "__main__":
    unittest.main()
