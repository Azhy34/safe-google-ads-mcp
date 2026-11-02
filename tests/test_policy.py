"""
The business policy (core/policy.py): where it is read from, what happens when it is missing or wrong, and the rules it drives.

Offline: no network. The suite runs on the fictional advertiser of tests/policy_test.json (pinned by policy_env).

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_policy.py" -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
import policy_env  # noqa: F401  (pins the fictional test policy before anything reads it)

from core import policy
from shield.models_ad import DeployGoldenRSARequest
from shield.models_asset import CreateCalloutAssetsRequest, CreateSitelinkAssetsRequest
from shield.models_image import CreateImageAssetRequest, check_site_image_url
from shield.models_location import CampaignLocationRequest
from shield.models_shopping import CheckShoppingReadinessRequest, CreateShoppingCampaignRequest

DKI = "{KeyWord:Sample Shoes}"
HEADLINES = [DKI] + [f"Headline number {i:02d}" for i in range(2, 16)]
DESCRIPTIONS = [f"Description text {i}" for i in range(1, 5)]


def rsa(url, headlines=None, descriptions=None):
    return DeployGoldenRSARequest(ad_group_id="1", final_url=url, headlines=headlines or HEADLINES,
                                  descriptions=descriptions or DESCRIPTIONS, pinned_h1_dki=DKI)


def sitelinks(url):
    item = {"link_text": "All shoes", "description1": "Free shipping", "description2": "Fast delivery", "final_url": url}
    return CreateSitelinkAssetsRequest(campaign_id="1", sitelinks=[item, {**item, "link_text": "Sale", "final_url": url + "2"}])


class FileResolution(unittest.TestCase):
    """Which file is read: MCP_POLICY_FILE, then policy.local.json, then the shipped policy.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(policy.reload)
        for target, name in ((policy, "LOCAL_FILE"), (policy, "SHIPPED_FILE")):
            patcher = mock.patch.object(target, name, self.dir / f"{name}.json")
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MCP_POLICY_FILE", None)
        policy.reload()

    def write(self, name, data):
        path = self.dir / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_nothing_configured_means_everything_closed(self):
        self.assertIsNone(policy.source_path())
        self.assertEqual(policy.url_rules(), [])
        self.assertEqual(policy.site_hosts(), ())
        self.assertEqual(policy.text("account.customer_id"), "")

    def test_shipped_then_local_then_explicit(self):
        self.write("SHIPPED_FILE.json", {"account": {"customer_id": "111"}})
        self.assertEqual(policy.text("account.customer_id"), "111")
        self.write("LOCAL_FILE.json", {"account": {"customer_id": "222"}})
        policy.reload()
        self.assertEqual(policy.text("account.customer_id"), "222")          # local wins over shipped
        os.environ["MCP_POLICY_FILE"] = str(self.write("mine.json", {"account": {"customer_id": "333"}}))
        policy.reload()
        self.assertEqual(policy.text("account.customer_id"), "333")          # explicit wins over local
        self.assertEqual(policy.text("account.merchant_center_id"), "")      # nothing is merged between files

    def test_a_missing_explicit_file_is_an_error_not_a_silent_fallback(self):
        os.environ["MCP_POLICY_FILE"] = str(self.dir / "nope.json")
        with self.assertRaises(policy.PolicyError):
            policy.reload()
            policy.get("account")

    def test_a_broken_file_stops_the_server_early(self):
        bad = ({"typo_key": 1}, {"account": {"customer_idd": "1"}}, {"account": "text"}, {"site": {"allowed_hosts": [1]}},
               {"ads": {"url_rules": [{"require_query": "x"}]}}, {"ads": {"url_rules": [{"host_contains": "a", "oops": 1}]}},
               {"ads": {"display_paths": ["only-one"]}}, {"geo": {"allowed_target_ids": [0]}})
        for data in bad:
            with self.subTest(data=data):
                os.environ["MCP_POLICY_FILE"] = str(self.write("bad.json", data))
                policy.reload()
                with self.assertRaises(policy.PolicyError):
                    policy.get("account")

    def test_comment_keys_are_ignored_and_override_restores(self):
        os.environ["MCP_POLICY_FILE"] = str(self.write("c.json", {"_comment": "hi", "account": {"customer_id": "9"}}))
        policy.reload()
        self.assertEqual(policy.text("account.customer_id"), "9")
        with policy.override({"account": {"customer_id": "10"}}):
            self.assertEqual(policy.text("account.customer_id"), "10")
        self.assertEqual(policy.text("account.customer_id"), "9")


class ShippedPolicyIsNeutral(unittest.TestCase):
    """config/policy.json carries no identity and no rule, and the tools that need them refuse (closed by default)."""

    def setUp(self):
        shipped = json.loads((PKG / "config" / "policy.json").read_text(encoding="utf-8"))
        cm = policy.override(shipped)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def test_it_sets_nothing(self):
        self.assertEqual(policy.url_rules(), [])
        self.assertEqual(policy.site_hosts(), ())
        self.assertEqual(policy.site_forbidden_terms(), ())
        self.assertEqual(policy.text("account.customer_id"), "")
        self.assertEqual(policy.text("account.merchant_center_id"), "")
        self.assertEqual(policy.text("shopping.name_prefix"), "")

    def test_any_url_and_text_are_accepted_by_the_rsa_shield(self):
        rsa("https://anything.example/shoes?coupon=X", descriptions=["Use X for 5% off", "b", "c", "d"])

    def test_site_tools_refuse_without_allowed_hosts(self):
        with self.assertRaises(ValidationError) as ctx:
            sitelinks("https://anything.example/a")
        self.assertIn("site.allowed_hosts", str(ctx.exception))
        with self.assertRaises(ValueError):
            check_site_image_url("https://anything.example/img/a.jpg")
        with self.assertRaises(ValidationError):
            CreateImageAssetRequest(source_url="https://anything.example/img/a.jpg", ratio="SQUARE")

    def test_callouts_have_no_forbidden_words(self):
        CreateCalloutAssetsRequest(campaign_id="1", callouts=["Marketplace", "Coupon"])

    def test_shopping_needs_a_merchant_center_and_a_feed_label(self):
        with self.assertRaises(ValidationError) as ctx:
            CreateShoppingCampaignRequest(name="Any name", offers=["A-1"])
        self.assertIn("account.merchant_center_id", str(ctx.exception))
        with self.assertRaises(ValidationError):
            CheckShoppingReadinessRequest()
        ok = CreateShoppingCampaignRequest(name="Any name", offers=["A-1"], merchant_id="1234567", feed_label="FEED")
        self.assertEqual((ok.ad_group_name, ok.geo_target_constants), ("All products", [2276]))

    def test_location_accepts_any_positive_geo_target(self):
        self.assertEqual(CampaignLocationRequest(campaign_id="1", geo_target_id=2250, enabled=True).geo_target_id, 2250)
        with self.assertRaises(ValidationError):
            CampaignLocationRequest(campaign_id="1", geo_target_id=0, enabled=True)

    def test_no_schedule_and_generic_negative_packs(self):
        self.assertEqual(policy.schedule_windows(), [])
        for language in ("de", "fr", "es"):
            self.assertTrue(policy.negative_pack(language))
        self.assertEqual(policy.negative_pack("xx"), [])
        for word in ("diy", "amazon"):
            self.assertIn(word, policy.negative_pack("de"))

    def test_display_paths_default_to_neutral_words(self):
        request = rsa("https://anything.example/shoes")
        self.assertEqual((request.path1, request.path2), ("Shop", "Sale"))


class ToolsThatNeedThePolicyRefuse(unittest.TestCase):
    """Tools that cannot work without a policy value refuse with a clear message instead of guessing."""

    def setUp(self):
        from shield import cloud_sink, error_shield
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (mock.patch.object(error_shield, "AUDIT_FILE", Path(self.tmp.name) / "audit.jsonl"),
                        mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
                        mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_geo_audit_needs_target_countries(self):
        from tools import geo_tools
        with mock.patch.object(geo_tools, "TARGET_COUNTRY_CODES", set()):
            result = geo_tools.audit_geo_physical_locations(30, customer_id="1112223334")
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "TARGET_COUNTRIES_NOT_SET")
        self.assertIn("geo.target_country_codes", result.error.message)

    def test_collision_fixer_needs_a_priority_marker(self):
        from tools import negative_tools
        with policy.override({"ads": {"priority_campaign_marker": ""}}):
            result = negative_tools.auto_fix_collisions_and_dupes(customer_id="1112223334")
        self.assertFalse(result.success)
        self.assertIn("ads.priority_campaign_marker", result.error.message)

    def test_negative_packs_come_from_the_policy_and_an_empty_pack_is_refused(self):
        from shield.models_criterion import ApplyNegativeShieldRequest
        from tools import negative_tools
        self.assertEqual(policy.negative_pack("DE"), ["amazon", "ebay", "kostenlos", "diy"])
        request = ApplyNegativeShieldRequest(campaign_id="1", language="de")
        with policy.override({"negatives": {"packs": {}}}):
            with mock.patch.object(negative_tools, "get_ads_client"):
                result = negative_tools.apply_negative_shield(request, customer_id="1112223334")
        self.assertFalse(result.success)
        self.assertIn("negatives.packs.de", result.error.message)

    def test_a_new_search_campaign_needs_geo_and_language(self):
        from shield.models_campaign import CreateSearchCampaignRequest
        base = dict(name="Any campaign", ad_group_name="Any group")
        with self.assertRaises(ValidationError):
            CreateSearchCampaignRequest(**base, geo_target_constants=[], language_constants=[1001])
        with self.assertRaises(ValidationError):
            CreateSearchCampaignRequest(**base, geo_target_constants=[2276], language_constants=[])
        CreateSearchCampaignRequest(**base, geo_target_constants=[2276], language_constants=[1001])


class UrlRulesDriveTheShield(unittest.TestCase):
    """The rules of the fictional advertiser (a marketplace that needs a coupon, an own site that must not carry one)."""

    SHOP = "https://example-shop.test/shoes"
    MARKET = "https://www.marketplace.example/listing/1"

    def test_a_required_query_is_enforced_on_its_host(self):
        with self.assertRaises(ValidationError) as ctx:
            rsa(self.MARKET)
        self.assertIn("coupon=SAVE10", str(ctx.exception))
        rsa(self.MARKET + "?coupon=SAVE10")

    def test_a_forbidden_query_and_forbidden_text_are_refused_on_the_site(self):
        rsa(self.SHOP)
        with self.assertRaises(ValidationError):
            rsa(self.SHOP + "?coupon=SAVE10")
        with self.assertRaises(ValidationError) as ctx:
            rsa(self.SHOP, headlines=[DKI, "Save10 today"] + HEADLINES[2:])
        self.assertIn("headline", str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            rsa(self.SHOP, descriptions=["Take SAVE10 off", "b", "c", "d"])
        self.assertIn("description", str(ctx.exception))

    def test_other_hosts_are_not_checked(self):
        rsa("https://other.example/x", descriptions=["Take SAVE10 off", "b", "c", "d"])

    def test_the_site_words_come_from_the_policy(self):
        with self.assertRaises(ValidationError):
            CreateCalloutAssetsRequest(campaign_id="1", callouts=["Marketplace deals", "Fast"])
        with self.assertRaises(ValidationError):
            CreateCalloutAssetsRequest(campaign_id="1", callouts=["Toxin-free", "Fast"])
        with policy.override({"site": {"forbidden_terms": ["cheap"]}, "ads": {}}):
            CreateCalloutAssetsRequest(campaign_id="1", callouts=["Marketplace deals", "Fast"])
            with self.assertRaises(ValidationError):
                CreateCalloutAssetsRequest(campaign_id="1", callouts=["So cheap", "Fast"])

    def test_schedule_windows_drive_both_campaign_tools(self):
        self.assertEqual(policy.schedule_windows(), [(8, 24), (0, 1)])
        with policy.override({"ads": {"schedule_windows": [[9, 18]]}}):
            self.assertEqual(policy.schedule_windows(), [(9, 18)])
        for bad in ([[18, 9]], [[0, 25]], [[1]], [["a", 2]]):
            with self.subTest(bad=bad), self.assertRaises(policy.PolicyError):
                with policy.override({"ads": {"schedule_windows": bad}}):
                    pass

    def test_the_policy_decides_the_default_display_paths_and_shopping_prefix(self):
        with policy.override({"ads": {"display_paths": ["Wear", "Deals"]}}):
            request = rsa(self.SHOP)
            self.assertEqual((request.path1, request.path2), ("Wear", "Deals"))
        with self.assertRaises(ValidationError):
            CreateShoppingCampaignRequest(name="Other Shopping", offers=["A-1"])
        CreateShoppingCampaignRequest(name="ExampleShop Shopping", offers=["A-1"])


if __name__ == "__main__":
    unittest.main()
