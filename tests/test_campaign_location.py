"""
set_campaign_location: one country on or off inside an existing campaign (for example when a country gets its own campaign).

The client is the real GoogleAdsClient with fake credentials (nothing leaves the machine), so the operation is Google's own type.
Only the services are mocked. The audit trail goes to a temporary file.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_campaign_location.py" -v
"""
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

from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials

from shield import cloud_sink, error_shield
from shield.error_shield import MUTATION_FUNCTIONS
from shield.models_location import CampaignLocationRequest
from tools.eval_tools import LEVER_GROUPS
from tools.location_tools import update_campaign_location

SITE = "2000000001"


def criterion(geo, negative=False):
    return SimpleNamespace(campaign_criterion=SimpleNamespace(
        resource_name=f"customers/1/campaignCriteria/{SITE}~{geo}", negative=negative,
        location=SimpleNamespace(geo_target_constant=f"geoTargetConstants/{geo}")))


class Fake:
    """The campaign's location criteria; the mutate really adds or removes them unless apply is False."""

    def __init__(self, targeted=(2276, 2040), excluded=(), apply=True, exists=True):
        self.targeted, self.excluded, self.apply, self.exists = list(targeted), list(excluded), apply, exists
        self.client = GoogleAdsClient(credentials=Credentials(token="t"), developer_token="d", use_proto_plus=True)
        self.service = mock.MagicMock()
        self.service.mutate_campaign_criteria.side_effect = self._mutate
        self.ga = mock.MagicMock()
        self.ga.search.side_effect = self._search
        self.client.get_service = lambda name: self.service if name == "CampaignCriterionService" else self.ga

    def _mutate(self, customer_id, operations):
        op = operations[0]
        if self.apply:
            if op._pb.WhichOneof("operation") == "remove":
                gone = int(op.remove.split("~")[-1])
                self.targeted = [g for g in self.targeted if g != gone]
            else:
                self.targeted.append(int(op.create.location.geo_target_constant.split("/")[-1]))
        return mock.MagicMock()

    def _search(self, customer_id, query):
        if "FROM campaign_criterion" in query:
            return [criterion(g) for g in self.targeted] + [criterion(g, negative=True) for g in self.excluded]
        if "FROM campaign WHERE" in query:
            return [SimpleNamespace(campaign=SimpleNamespace(id=int(SITE), name="ExampleShop Search DE - Site"))] if self.exists else []
        raise AssertionError(f"unexpected query: {query}")


class LocationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(error_shield, "AUDIT_FILE", Path(tmp.name) / "audit.jsonl"),
                        mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
                        mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tool(self, fake, geo, enabled):
        req = CampaignLocationRequest(campaign_id=SITE, geo_target_id=geo, enabled=enabled)
        with mock.patch("tools.location_tools.get_ads_client", return_value=fake.client):
            return update_campaign_location(req, customer_id="1")

    def test_austria_is_removed_and_germany_stays(self):
        fake = Fake()
        result = self.run_tool(fake, 2040, False)
        operation = fake.service.mutate_campaign_criteria.call_args.kwargs["operations"][0]
        self.assertEqual(operation.remove, f"customers/1/campaignCriteria/{SITE}~2040")
        self.assertTrue(result.success)
        self.assertIn("Austria (2040) no longer targeted", result.data)
        self.assertIn("now: Germany (2276)", result.data)
        self.assertEqual(fake.targeted, [2276])

    def test_austria_comes_back_with_a_create_operation(self):
        fake = Fake(targeted=(2276,))
        result = self.run_tool(fake, 2040, True)
        operation = fake.service.mutate_campaign_criteria.call_args.kwargs["operations"][0]
        self.assertEqual(operation.create.campaign, f"customers/1/campaigns/{SITE}")
        self.assertEqual(operation.create.location.geo_target_constant, "geoTargetConstants/2040")
        self.assertTrue(result.success)
        self.assertEqual(sorted(fake.targeted), [2040, 2276])

    def test_the_last_country_is_never_removed(self):
        fake = Fake(targeted=(2276,))
        result = self.run_tool(fake, 2276, False)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "LAST_TARGETED_LOCATION")
        fake.service.mutate_campaign_criteria.assert_not_called()

    def test_a_country_already_in_the_wanted_state_changes_nothing(self):
        fake = Fake(targeted=(2276,))
        self.assertIn("not targeted", self.run_tool(fake, 2040, False).data)
        fake2 = Fake()
        self.assertIn("already targeted", self.run_tool(fake2, 2276, True).data)
        fake.service.mutate_campaign_criteria.assert_not_called()
        fake2.service.mutate_campaign_criteria.assert_not_called()

    def test_an_excluded_country_does_not_count_as_targeted(self):
        fake = Fake(targeted=(2276,), excluded=(2040,))
        result = self.run_tool(fake, 2040, False)
        self.assertIn("not targeted", result.data)
        fake.service.mutate_campaign_criteria.assert_not_called()

    def test_a_change_google_did_not_apply_is_a_failure(self):
        fake = Fake(apply=False)
        result = self.run_tool(fake, 2040, False)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "NOT_APPLIED")

    def test_an_unknown_campaign_is_a_typed_refusal(self):
        result = self.run_tool(Fake(exists=False), 2040, False)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "CAMPAIGN_NOT_FOUND")


class ContractTests(unittest.TestCase):
    def test_only_our_two_countries_and_numeric_ids_pass(self):
        CampaignLocationRequest(campaign_id=SITE, geo_target_id=2040, enabled=False)
        for bad in ({"geo_target_id": 2840}, {"geo_target_id": 1}, {"campaign_id": "1 OR 1=1"}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                CampaignLocationRequest(**{"campaign_id": SITE, "geo_target_id": 2276, "enabled": True, **bad})

    def test_the_tool_is_a_mutation_a_targeting_lever_and_registered(self):
        self.assertIn("update_campaign_location", MUTATION_FUNCTIONS)
        self.assertIn("update_campaign_location", LEVER_GROUPS["targeting"])
        source = (PKG / "server.py").read_text(encoding="utf-8")
        self.assertIn("def set_campaign_location(campaign_id: str, geo_target_id: int, enabled: bool)", source)
        self.assertIn('build_request("update_campaign_location", CampaignLocationRequest', source)


if __name__ == "__main__":
    unittest.main()
