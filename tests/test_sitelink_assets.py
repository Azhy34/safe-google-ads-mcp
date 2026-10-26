"""
Offline tests for the sitelink tools of the site: preview_sitelink_assets (read) and create_sitelink_assets (write).
Runs without the network: the Google Ads services are mocked, the audit trail goes to a temporary file.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_sitelink_assets.py" -v
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
from shield.models_asset import CreateSitelinkAssetsRequest, SitelinkItem
from tools.asset_tools import create_sitelink_assets, preview_sitelink_assets

CAMPAIGN = "24386071357"
BASE = "https://example.com"


def link(text="Organic Cotton Hoodie", url=f"{BASE}/motive/fototapete-wald", d1="Waldtiere fürs Kinderzimmer", d2="Kostenloser Versand DE & AT"):
    return {"link_text": text, "description1": d1, "description2": d2, "final_url": url}


def four():
    return [
        link(),
        link("Ergonomic Office Chair", f"{BASE}/motive/tapete-dschungel", "Safari & Dschungel Motive", "Direkt vom Hersteller"),
        link("Weltraum Tapete", f"{BASE}/motive/weltraum-tapete", "Planeten & Sonnensystem", "Muster DIN A4 für 16,50 €"),
        link("Alle Motive ansehen", f"{BASE}/motive", "Fototapeten nach Thema", "Vlies, Paste-the-Wall"),
    ]


class AuditLogCase(unittest.TestCase):
    """Audit trail to a temporary file, no cloud sink, preflight gate off."""

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
    def test_valid_request(self):
        req = CreateSitelinkAssetsRequest(campaign_id=CAMPAIGN, sitelinks=four())
        self.assertEqual(len(req.sitelinks), 4)

    def test_www_host_is_accepted_and_texts_are_stripped(self):
        item = SitelinkItem(**link("  Organic Cotton Hoodie  ", "https://www.example.com/motive/fototapete-wald"))
        self.assertEqual(item.link_text, "Organic Cotton Hoodie")

    def test_text_limits(self):
        with self.assertRaises(ValidationError):
            SitelinkItem(**link(text="x" * 26))
        with self.assertRaises(ValidationError):
            SitelinkItem(**link(d1="x" * 36))
        with self.assertRaises(ValidationError):
            SitelinkItem(**link(d2=""))
        SitelinkItem(**link(text="x" * 25, d1="y" * 35, d2="z" * 35))  # exactly at the limit

    def test_address_must_be_a_clean_https_page_of_the_site(self):
        for bad in (
            "http://example.com/motive",                       # not https
            f"{BASE}/motive?query=bad",                        # query string
            f"{BASE}/motive#faq",                              # anchor
        ):
            with self.subTest(url=bad), self.assertRaises(ValidationError):
                SitelinkItem(**link(url=bad))

    def test_forbidden_words_of_the_site_rules(self):
        for bad in ("forbidden_term in text", "Schadstofffrei", "Allergiefrei", "promo code"):
            with self.subTest(text=bad), self.assertRaises(ValidationError):
                SitelinkItem(**link(d1=bad))

    def test_count_and_uniqueness(self):
        with self.assertRaises(ValidationError):
            CreateSitelinkAssetsRequest(campaign_id=CAMPAIGN, sitelinks=four()[:1])
        with self.assertRaises(ValidationError):
            CreateSitelinkAssetsRequest(campaign_id=CAMPAIGN, sitelinks=four() + four()[:3])  # 7
        twin_text = four()
        twin_text[1]["link_text"] = twin_text[0]["link_text"].upper()
        with self.assertRaises(ValidationError):
            CreateSitelinkAssetsRequest(campaign_id=CAMPAIGN, sitelinks=twin_text)
        twin_url = four()
        twin_url[1]["final_url"] = twin_url[0]["final_url"] + "/"
        with self.assertRaises(ValidationError):
            CreateSitelinkAssetsRequest(campaign_id=CAMPAIGN, sitelinks=twin_url)
        with self.assertRaises(ValidationError):
            CreateSitelinkAssetsRequest(campaign_id="24386071357; DROP", sitelinks=four())

    def test_only_the_creating_tool_is_a_mutation(self):
        self.assertIn("create_sitelink_assets", MUTATION_FUNCTIONS)
        self.assertNotIn("preview_sitelink_assets", MUTATION_FUNCTIONS)


class ShieldRejectionTests(AuditLogCase):
    def test_rejection_is_logged_with_the_right_action_type(self):
        for name, action in (("preview_sitelink_assets", "READ"), ("create_sitelink_assets", "MUTATION")):
            req, rejected = build_request(name, CreateSitelinkAssetsRequest, action_type=action,
                                          campaign_id=CAMPAIGN, sitelinks=[link(url="https://store.example.com/item/x")] * 2)
            self.assertIsNone(req)
            self.assertEqual(json.loads(rejected)["error"]["code"], "REQUEST_REJECTED")
        entries = self.log_entries()
        self.assertEqual([(e["function"], e["action_type"]) for e in entries],
                         [("preview_sitelink_assets", "READ"), ("create_sitelink_assets", "MUTATION")])


def row_campaign(status="PAUSED"):
    return SimpleNamespace(campaign=SimpleNamespace(id=int(CAMPAIGN), name="E-Commerce Search DE+AT", status=SimpleNamespace(name=status)))


def row_asset(asset_id, item):
    return SimpleNamespace(asset=SimpleNamespace(
        id=asset_id, final_urls=[item["final_url"]],
        sitelink_asset=SimpleNamespace(link_text=item["link_text"], description1=item["description1"], description2=item["description2"])))


def row_linked(asset_id):
    return SimpleNamespace(campaign_asset=SimpleNamespace(asset=f"customers/1234567890/assets/{asset_id}"))


class FakeAds:
    """A mocked Google Ads client: GAQL answers by the table in the query, the mutate calls are recorded."""

    def __init__(self, campaigns=None, assets=(), linked=()):
        self.campaigns = [row_campaign()] if campaigns is None else campaigns
        self.assets, self.linked = list(assets), list(linked)
        self.client = mock.MagicMock()
        self.ga = mock.MagicMock()
        self.ga.search.side_effect = self._search
        self.assets_svc, self.campaign_assets_svc = mock.MagicMock(), mock.MagicMock()
        self.assets_svc.asset_path.side_effect = lambda cid, aid: f"customers/{cid}/assets/{aid}"
        self.assets_svc.mutate_assets.side_effect = lambda customer_id, operations: mock.MagicMock(
            results=[mock.MagicMock(resource_name=f"customers/{customer_id}/assets/9000{i}") for i in range(len(operations))])
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


class PreviewTests(AuditLogCase):
    def test_preview_changes_nothing_and_plans_four_new_sitelinks(self):
        ads = FakeAds()
        with ads.patched():
            result = preview_sitelink_assets(CreateSitelinkAssetsRequest(campaign_id=CAMPAIGN, sitelinks=four()), customer_id="1234567890")
        self.assertTrue(result.success)
        plan = json.loads(result.data)
        self.assertEqual([i["action"] for i in plan["items"]], ["create"] * 4)
        self.assertEqual(plan["sitelinks_on_campaign_now"], 0)
        self.assertEqual(plan["sitelinks_on_campaign_after"], 4)
        self.assertEqual(plan["warnings"], [])
        self.assertTrue(plan["changes_nothing"])
        ads.assets_svc.mutate_assets.assert_not_called()
        ads.campaign_assets_svc.mutate_campaign_assets.assert_not_called()

    def test_preview_warns_below_four_sitelinks(self):
        ads = FakeAds()
        with ads.patched():
            result = preview_sitelink_assets(CreateSitelinkAssetsRequest(campaign_id=CAMPAIGN, sitelinks=four()[:2]), customer_id="1234567890")
        plan = json.loads(result.data)
        self.assertEqual(plan["sitelinks_on_campaign_after"], 2)
        self.assertEqual(len(plan["warnings"]), 1)

    def test_unknown_campaign_is_a_typed_failure(self):
        ads = FakeAds(campaigns=[])
        with ads.patched():
            result = preview_sitelink_assets(CreateSitelinkAssetsRequest(campaign_id=CAMPAIGN, sitelinks=four()), customer_id="1234567890")
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "CAMPAIGN_NOT_FOUND")


class CreateTests(AuditLogCase):
    def create(self, ads, sitelinks=None):
        with ads.patched():
            return create_sitelink_assets(CreateSitelinkAssetsRequest(campaign_id=CAMPAIGN, sitelinks=sitelinks or four()), customer_id="1234567890")

    def test_creates_four_assets_and_links_them_in_two_atomic_requests(self):
        ads = FakeAds()
        result = self.create(ads)
        self.assertTrue(result.success)
        ads.assets_svc.mutate_assets.assert_called_once()
        self.assertEqual(len(ads.assets_svc.mutate_assets.call_args.kwargs["operations"]), 4)
        ads.campaign_assets_svc.mutate_campaign_assets.assert_called_once()
        self.assertEqual(len(ads.campaign_assets_svc.mutate_campaign_assets.call_args.kwargs["operations"]), 4)
        self.assertIn("created 4 new asset(s)", result.data)
        self.assertIn("now has 4 sitelinks", result.data)

    def test_reuses_an_identical_asset_and_skips_a_linked_one(self):
        items = four()
        ads = FakeAds(assets=[row_asset(111, items[0]), row_asset(222, items[1])], linked=[row_linked(222)])
        result = self.create(ads, items)
        self.assertTrue(result.success)
        # items[0] exists but is not linked -> link only; items[1] exists and is linked -> nothing; items[2], [3] are new
        self.assertEqual(len(ads.assets_svc.mutate_assets.call_args.kwargs["operations"]), 2)
        self.assertEqual(len(ads.campaign_assets_svc.mutate_campaign_assets.call_args.kwargs["operations"]), 3)
        self.assertIn("created 2 new asset(s)", result.data)
        self.assertIn("reused existing ['111']", result.data)
        self.assertIn("already linked 1 ['222']", result.data)
        self.assertIn("now has 4 sitelinks", result.data)

    def test_a_repeat_after_everything_is_done_writes_nothing(self):
        items = four()
        ads = FakeAds(assets=[row_asset(100 + i, s) for i, s in enumerate(items)], linked=[row_linked(100 + i) for i in range(4)])
        result = self.create(ads, items)
        self.assertTrue(result.success)
        ads.assets_svc.mutate_assets.assert_not_called()
        ads.campaign_assets_svc.mutate_campaign_assets.assert_not_called()
        self.assertIn("already linked 4", result.data)

    def test_unknown_campaign_writes_nothing(self):
        ads = FakeAds(campaigns=[])
        result = self.create(ads)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "CAMPAIGN_NOT_FOUND")
        ads.assets_svc.mutate_assets.assert_not_called()

    def test_the_audit_trail_marks_the_write_as_a_mutation(self):
        self.create(FakeAds())
        entry = self.log_entries()[-1]
        self.assertEqual((entry["function"], entry["action_type"], entry["success"]), ("create_sitelink_assets", "MUTATION", True))
        self.assertIn("created 4", entry["result"])


if __name__ == "__main__":
    unittest.main()
