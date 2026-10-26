"""
Offline tests for create_callout_assets and create_structured_snippet_asset.
The Google Ads services are mocked. The audit trail goes to a temporary file.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_extension_assets.py" -v
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

from shield import cloud_sink, error_shield
from shield.error_shield import MUTATION_FUNCTIONS, build_request
from shield.models_asset import CreateCalloutAssetsRequest, CreateStructuredSnippetAssetRequest
from tools.asset_tools import create_callout_assets, create_structured_snippet_asset

CAMPAIGN = "24386071357"
CALLOUTS = ["Kostenloser Versand", "Paste the Wall", "Muster für 16,50 €", "Hergestellt in der EU"]
SNIPPET_VALUES = ["Wald", "Dschungel", "Weltraum"]


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


class ContractTests(unittest.TestCase):
    def test_callouts_reject_length_forbidden_words_and_duplicates(self):
        CreateCalloutAssetsRequest(campaign_id=CAMPAIGN, callouts=["  Versand  ", "Muster"])
        with self.assertRaises(ValidationError):
            CreateCalloutAssetsRequest(campaign_id=CAMPAIGN, callouts=["x" * 26, "Muster"])
        with self.assertRaises(ValidationError):
            CreateCalloutAssetsRequest(campaign_id=CAMPAIGN, callouts=["forbidden_term", "Muster"])
        with self.assertRaises(ValidationError):
            CreateCalloutAssetsRequest(campaign_id=CAMPAIGN, callouts=["Wald", "wald"])
        with self.assertRaises(ValidationError):
            CreateCalloutAssetsRequest(campaign_id=CAMPAIGN, callouts=["Nur eins"])
        self.assertIn("create_callout_assets", MUTATION_FUNCTIONS)

    def test_snippet_header_is_the_official_google_string(self):
        req = CreateStructuredSnippetAssetRequest(campaign_id=CAMPAIGN, header=" Typen ", values=SNIPPET_VALUES)
        self.assertEqual(req.header, "Typen")
        for bad in ("Motive", "Reiseziele", "Versicherungsschutz"):
            with self.assertRaises(ValidationError):
                CreateStructuredSnippetAssetRequest(campaign_id=CAMPAIGN, header=bad, values=SNIPPET_VALUES)
        with self.assertRaises(ValidationError):
            CreateStructuredSnippetAssetRequest(campaign_id=CAMPAIGN, header="Typen", values=["Wald", "Dschungel"])
        with self.assertRaises(ValidationError):
            CreateStructuredSnippetAssetRequest(campaign_id=CAMPAIGN, header="Typen", values=["peel and stick", "Wald", "Meer"])
        self.assertIn("create_structured_snippet_asset", MUTATION_FUNCTIONS)

    def test_a_bad_callout_is_rejected_before_google(self):
        _req, rejected = build_request("create_callout_assets", CreateCalloutAssetsRequest, action_type="MUTATION",
                                       campaign_id=CAMPAIGN, callouts=["forbidden_term", "Muster"])
        self.assertIsNone(_req)
        self.assertIn("REQUEST_REJECTED", rejected)


def row_campaign():
    return SimpleNamespace(campaign=SimpleNamespace(id=int(CAMPAIGN), name="E-Commerce Search DE+AT", status=SimpleNamespace(name="PAUSED")))


def row_callout(asset_id, text):
    return SimpleNamespace(asset=SimpleNamespace(id=asset_id, callout_asset=SimpleNamespace(callout_text=text)))


def row_snippet(asset_id, header, values):
    return SimpleNamespace(asset=SimpleNamespace(
        id=asset_id, structured_snippet_asset=SimpleNamespace(header=header, values=values)))


def row_linked(asset_id, header=None, values=None):
    snippet = SimpleNamespace(header=header, values=values or [])
    return SimpleNamespace(
        campaign_asset=SimpleNamespace(asset=f"customers/1234567890/assets/{asset_id}"),
        asset=SimpleNamespace(structured_snippet_asset=snippet),
    )


class FakeAds:
    def __init__(self, campaigns=None, assets=(), linked=(), field_names=("CALLOUT", "STRUCTURED_SNIPPET")):
        self.campaigns = [row_campaign()] if campaigns is None else campaigns
        self.assets, self.linked = list(assets), list(linked)
        self.client = mock.MagicMock()
        self.ga = mock.MagicMock()
        self.ga.search.side_effect = self._search
        self.assets_svc = mock.MagicMock()
        self.campaign_assets_svc = mock.MagicMock()
        self.assets_svc.asset_path.side_effect = lambda cid, aid: f"customers/{cid}/assets/{aid}"
        self.assets_svc.mutate_assets.side_effect = lambda customer_id, operations: mock.MagicMock(
            results=[mock.MagicMock(resource_name=f"customers/{customer_id}/assets/9000{i}") for i in range(len(operations))])
        enum = mock.MagicMock(spec=list(field_names))
        for name in field_names:
            setattr(enum, name, name)
        self.client.enums.AssetFieldTypeEnum = enum
        self.client.get_service.side_effect = lambda name: {
            "GoogleAdsService": self.ga, "AssetService": self.assets_svc, "CampaignAssetService": self.campaign_assets_svc,
        }.get(name, mock.MagicMock())

    def _search(self, customer_id, query):
        if "FROM campaign_asset" in query:
            return self.linked
        if "FROM asset" in query:
            return self.assets
        return self.campaigns

    def patched(self):
        return mock.patch("tools.asset_tools.get_ads_client", return_value=self.client)


class CalloutTests(AuditLogCase):
    def create(self, ads, callouts=None):
        with ads.patched():
            return create_callout_assets(CreateCalloutAssetsRequest(campaign_id=CAMPAIGN, callouts=callouts or CALLOUTS), customer_id="1234567890")

    def test_creates_and_links_in_two_requests_with_the_callout_field_type(self):
        ads = FakeAds()
        result = self.create(ads)
        self.assertTrue(result.success)
        self.assertEqual(len(ads.assets_svc.mutate_assets.call_args.kwargs["operations"]), 4)
        link_ops = ads.campaign_assets_svc.mutate_campaign_assets.call_args.kwargs["operations"]
        self.assertEqual(len(link_ops), 4)
        self.assertEqual(link_ops[0].create.field_type, "CALLOUT")
        self.assertIn("created 4 new asset(s)", result.data)
        entry = self.log_entries()[-1]
        self.assertEqual((entry["function"], entry["action_type"], entry["success"]), ("create_callout_assets", "MUTATION", True))

    def test_reuses_an_existing_text_and_skips_one_already_linked(self):
        ads = FakeAds(assets=[row_callout(111, CALLOUTS[0]), row_callout(222, CALLOUTS[1])], linked=[row_linked(222)])
        result = self.create(ads)
        self.assertTrue(result.success)
        self.assertEqual(len(ads.assets_svc.mutate_assets.call_args.kwargs["operations"]), 2)
        self.assertEqual(len(ads.campaign_assets_svc.mutate_campaign_assets.call_args.kwargs["operations"]), 3)
        self.assertIn("reused existing ['111']", result.data)
        self.assertIn("already linked 1 ['222']", result.data)

    def test_a_repeat_writes_nothing(self):
        ads = FakeAds(assets=[row_callout(100 + i, text) for i, text in enumerate(CALLOUTS)],
                      linked=[row_linked(100 + i) for i in range(len(CALLOUTS))])
        result = self.create(ads)
        self.assertTrue(result.success)
        ads.assets_svc.mutate_assets.assert_not_called()
        ads.campaign_assets_svc.mutate_campaign_assets.assert_not_called()

    def test_unknown_campaign_writes_nothing(self):
        ads = FakeAds(campaigns=[])
        result = self.create(ads)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "CAMPAIGN_NOT_FOUND")
        ads.assets_svc.mutate_assets.assert_not_called()


class SnippetTests(AuditLogCase):
    def create(self, ads, header="Typen", values=None):
        with ads.patched():
            return create_structured_snippet_asset(
                CreateStructuredSnippetAssetRequest(campaign_id=CAMPAIGN, header=header, values=values or SNIPPET_VALUES),
                customer_id="1234567890")

    def test_creates_one_snippet_and_links_it(self):
        ads = FakeAds()
        result = self.create(ads)
        self.assertTrue(result.success)
        ads.assets_svc.mutate_assets.assert_called_once()
        op = ads.assets_svc.mutate_assets.call_args.kwargs["operations"][0]
        self.assertEqual(op.create.structured_snippet_asset.header, "Typen")
        link_ops = ads.campaign_assets_svc.mutate_campaign_assets.call_args.kwargs["operations"]
        self.assertEqual(link_ops[0].create.field_type, "STRUCTURED_SNIPPET")
        self.assertIn("created 1 new asset(s)", result.data)

    def test_reuses_the_same_header_and_value_set_ignoring_order_and_case(self):
        ads = FakeAds(assets=[row_snippet(333, "Typen", ["weltraum", "Wald", "Dschungel"])])
        result = self.create(ads)
        self.assertTrue(result.success)
        ads.assets_svc.mutate_assets.assert_not_called()
        ads.campaign_assets_svc.mutate_campaign_assets.assert_called_once()
        self.assertIn("reused existing ['333']", result.data)

    def test_an_already_linked_identical_snippet_writes_nothing(self):
        ads = FakeAds(assets=[row_snippet(333, "Typen", SNIPPET_VALUES)], linked=[row_linked(333, "Typen", SNIPPET_VALUES)])
        result = self.create(ads)
        self.assertTrue(result.success)
        ads.assets_svc.mutate_assets.assert_not_called()
        ads.campaign_assets_svc.mutate_campaign_assets.assert_not_called()
        self.assertIn("already linked [333]", result.data)

    def test_a_different_value_set_for_the_same_header_writes_nothing(self):
        ads = FakeAds(linked=[row_linked(333, "Typen", ["Meer", "Berge", "Wiese"])])
        result = self.create(ads)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "HEADER_ALREADY_ON_CAMPAIGN")
        ads.assets_svc.mutate_assets.assert_not_called()
        ads.campaign_assets_svc.mutate_campaign_assets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
