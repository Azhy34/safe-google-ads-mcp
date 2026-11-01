"""
set_ad_group_status: the status of a whole ad group, with the guards that would have saved a half-built group that went live.

The Google Ads services are mocked, the operation type is the real one, so the update mask is the one Google gets.
The audit trail goes to a temporary file.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_ad_group_status.py" -v
"""
import importlib
import os
import pkgutil
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

import google.ads.googleads as googleads
from shield import cloud_sink, error_shield
from shield.error_shield import MUTATION_FUNCTIONS
from shield.models_adgroup import AdGroupStatusUpdateRequest
from tools.adgroup_tools import update_ad_group_status
from tools.eval_tools import LEVER_GROUPS

VERSION = sorted(m.name for m in pkgutil.iter_modules(googleads.__path__) if m.name.startswith("v"))[-1]
AdGroupOperation = importlib.import_module(f"google.ads.googleads.{VERSION}.services.types.ad_group_service").AdGroupOperation
AdGroupStatus = importlib.import_module(f"google.ads.googleads.{VERSION}.enums.types.ad_group_status").AdGroupStatusEnum.AdGroupStatus

GROUP = "201741241196"


class FakeGoogleAds:
    """The slice of the account the tool reads; mutate_ad_groups changes the group status unless apply is False."""

    def __init__(self, status="PAUSED", enabled_groups=2, keywords=14, ads=1, images=3, apply=True, exists=True):
        self.status, self.enabled_groups, self.keywords, self.ads, self.images = status, enabled_groups, keywords, ads, images
        self.apply, self.exists = apply, exists
        self.queries = []
        self.ag_service = mock.MagicMock()
        self.ag_service.ad_group_path.side_effect = lambda cid, aid: f"customers/{cid}/adGroups/{aid}"
        self.ag_service.mutate_ad_groups.side_effect = self._mutate
        self.ga_service = mock.MagicMock()
        self.ga_service.search.side_effect = self._search
        self.client = mock.MagicMock()
        self.client.get_type.side_effect = lambda name: AdGroupOperation()
        self.client.enums = SimpleNamespace(AdGroupStatusEnum=AdGroupStatus)
        self.client.get_service.side_effect = lambda name: self.ag_service if name == "AdGroupService" else self.ga_service

    def _mutate(self, customer_id, operations):
        if self.apply:
            self.status = AdGroupStatus(operations[0].update.status).name
        return mock.MagicMock()

    def _search(self, customer_id, query):
        self.queries.append(query)
        # The live API refuses WHERE ad_group.id on these resources unless ad_group.id is selected too
        # (query_error.EXPECTED_REFERENCED_FIELD_IN_SELECT_CLAUSE, hit on 31.10.2026 enabling a real group)
        head = query.split("FROM")[0]
        watched = ("FROM ad_group_criterion" in query or "FROM ad_group_asset" in query or "FROM ad_group_ad" in query)
        if watched and "WHERE ad_group.id" in query and "ad_group.id" not in head:
            raise AssertionError(f"live API would refuse: ad_group.id must be in SELECT: {query.strip()[:80]}")
        if "SELECT ad_group.status FROM" in query:
            return [SimpleNamespace(ad_group=SimpleNamespace(status=SimpleNamespace(name=self.status)))]
        if "ad_group.name" in query:
            if not self.exists:
                return []
            return [SimpleNamespace(
                ad_group=SimpleNamespace(id=int(GROUP), name="Category Alpha - Landing Category", status=SimpleNamespace(name=self.status)),
                campaign=SimpleNamespace(id=2000000001, name="ExampleShop Search DE - Site"))]
        if "campaign.id =" in query:
            return [object()] * self.enabled_groups
        if "FROM ad_group_criterion" in query:
            return [object()] * self.keywords
        if "FROM ad_group_ad" in query:
            return [object()] * self.ads
        if "FROM ad_group_asset" in query:
            return [object()] * self.images
        raise AssertionError(f"unexpected query: {query}")


class AdGroupStatusTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(error_shield, "AUDIT_FILE", Path(tmp.name) / "audit.jsonl"),
                        mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
                        mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tool(self, fake, enabled):
        req = AdGroupStatusUpdateRequest(ad_group_id=GROUP, enabled=enabled)
        with mock.patch("tools.adgroup_tools.get_ads_client", return_value=fake.client):
            return update_ad_group_status(req, customer_id="1")

    def test_enabling_a_ready_group_names_status_in_the_mask_and_reads_it_back(self):
        fake = FakeGoogleAds(status="PAUSED")
        result = self.run_tool(fake, enabled=True)
        operation = fake.ag_service.mutate_ad_groups.call_args.kwargs["operations"][0]
        self.assertEqual(list(operation.update_mask.paths), ["status"])
        self.assertEqual(operation.update.resource_name, f"customers/1/adGroups/{GROUP}")
        self.assertEqual(AdGroupStatus(operation.update.status).name, "ENABLED")
        self.assertTrue(result.success)
        self.assertIn("PAUSED -> ENABLED", result.data)
        self.assertNotIn("WARNING", result.data)

    def test_a_group_without_own_images_is_enabled_with_a_warning(self):
        fake = FakeGoogleAds(status="PAUSED", images=0)
        result = self.run_tool(fake, enabled=True)
        self.assertTrue(result.success)
        self.assertIn("WARNING", result.data)
        self.assertIn("no images of its own", result.data)

    def test_a_group_without_keywords_is_not_enabled(self):
        fake = FakeGoogleAds(status="PAUSED", keywords=0)
        result = self.run_tool(fake, enabled=True)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "AD_GROUP_NOT_READY")
        self.assertIn("keyword", result.error.message)
        fake.ag_service.mutate_ad_groups.assert_not_called()

    def test_a_group_without_a_responsive_search_ad_is_not_enabled(self):
        fake = FakeGoogleAds(status="PAUSED", ads=0)
        result = self.run_tool(fake, enabled=True)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "AD_GROUP_NOT_READY")
        self.assertIn("Responsive Search Ad", result.error.message)
        fake.ag_service.mutate_ad_groups.assert_not_called()

    def test_the_last_enabled_group_of_a_campaign_is_not_paused(self):
        fake = FakeGoogleAds(status="ENABLED", enabled_groups=1)
        result = self.run_tool(fake, enabled=False)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "LAST_ENABLED_AD_GROUP")
        fake.ag_service.mutate_ad_groups.assert_not_called()

    def test_pausing_one_of_several_groups_works(self):
        fake = FakeGoogleAds(status="ENABLED", enabled_groups=4)
        result = self.run_tool(fake, enabled=False)
        operation = fake.ag_service.mutate_ad_groups.call_args.kwargs["operations"][0]
        self.assertEqual(list(operation.update_mask.paths), ["status"])
        self.assertTrue(result.success)
        self.assertIn("ENABLED -> PAUSED", result.data)

    def test_a_group_already_in_the_wanted_status_changes_nothing(self):
        fake = FakeGoogleAds(status="PAUSED")
        result = self.run_tool(fake, enabled=False)
        self.assertTrue(result.success)
        self.assertIn("already PAUSED", result.data)
        fake.ag_service.mutate_ad_groups.assert_not_called()

    def test_an_unknown_group_is_a_typed_refusal(self):
        fake = FakeGoogleAds(exists=False)
        result = self.run_tool(fake, enabled=True)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "AD_GROUP_NOT_FOUND")

    def test_a_change_google_did_not_apply_is_a_failure(self):
        fake = FakeGoogleAds(status="PAUSED", apply=False)
        result = self.run_tool(fake, enabled=True)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "NOT_APPLIED")
        self.assertIn("PAUSED", result.error.message)


class ContractTests(unittest.TestCase):

    def test_the_request_wants_a_numeric_id(self):
        AdGroupStatusUpdateRequest(ad_group_id=GROUP, enabled=True)
        with self.assertRaises(ValidationError):
            AdGroupStatusUpdateRequest(ad_group_id="1 OR 1=1", enabled=True)

    def test_the_tool_is_a_mutation_and_a_status_lever_and_is_registered(self):
        self.assertIn("update_ad_group_status", MUTATION_FUNCTIONS)
        self.assertIn("update_ad_group_status", LEVER_GROUPS["status"])
        source = (PKG / "server.py").read_text(encoding="utf-8")
        self.assertIn("def set_ad_group_status(ad_group_id: str, enabled: bool)", source)
        self.assertIn('build_request("update_ad_group_status", AdGroupStatusUpdateRequest', source)


if __name__ == "__main__":
    unittest.main()
