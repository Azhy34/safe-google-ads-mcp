"""
Offline tests for create_promotion_asset: Pydantic shield, micro-percent conversion (10% = 100,000),
dry_run preview, idempotent reuse, read-back verification, linking to campaign/ad group, and telemetry/audit logging.
All Google Ads services are mocked; audit output goes to a temporary file.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_promotion_asset.py" -v
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
from shield.models_asset import CreatePromotionAssetRequest
from tools.asset_tools import create_promotion_asset
from tools.eval_tools import LEVER_GROUPS

CAMPAIGN = "2000000001"
AD_GROUP = "3000000001"
MARKETPLACE_URL = "https://marketplace.example/es?coupon=SALE10"
SITE_URL = "https://example-shop.test/collection"


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


class PromotionContractTests(unittest.TestCase):
    def test_valid_percent_promotion_and_registries(self):
        req = CreatePromotionAssetRequest(
            name="ES Promo 10% Autumn",
            promotion_target="Kids Decor",
            final_url=MARKETPLACE_URL,
            language_code="ES",
            percent_off=10,
            promotion_code="SALE10",
            occasion="black_friday",
            redemption_start_date="2026-11-20",
            redemption_end_date="2026-11-30",
            campaign_id=CAMPAIGN,
        )
        self.assertEqual(req.language_code, "es")
        self.assertEqual(req.occasion, "BLACK_FRIDAY")
        self.assertIn("create_promotion_asset", MUTATION_FUNCTIONS)
        self.assertIn("create_promotion_asset", LEVER_GROUPS["assets"])

    def test_rejects_invalid_discounts_lengths_dates_and_mutual_exclusions(self):
        base = dict(name="Promo", promotion_target="Kids Decor", final_url=MARKETPLACE_URL)
        # Missing discount
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(**base)
        # Both discount types
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(**base, percent_off=10, money_amount_off_eur=15.0)
        # Out-of-range percent
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(**base, percent_off=0)
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(**base, percent_off=101)
        # promotion_target > 20 chars
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(name="Promo", promotion_target="x" * 21, final_url=MARKETPLACE_URL, percent_off=10)
        # promotion_code > 15 chars
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(**base, percent_off=10, promotion_code="CODE123456789012")
        # Both promotion_code and orders_over_eur
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(**base, percent_off=10, promotion_code="SALE10", orders_over_eur=50.0)
        # Both campaign_id and ad_group_id
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(**base, percent_off=10, campaign_id=CAMPAIGN, ad_group_id=AD_GROUP)
        # Non-HTTPS URL
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(name="Promo", promotion_target="Decor", final_url="http://marketplace.example", percent_off=10)
        # Reversed redemption dates
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(**base, percent_off=10, redemption_start_date="2026-11-30", redemption_end_date="2026-11-20")
        # Unsupported occasion
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(**base, percent_off=10, occasion="INVALID_HOLIDAY")

    def test_enforces_policy_forbid_query_and_forbid_text_on_direct_site(self):
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(
                name="Direct Site Promo",
                promotion_target="Running Shoes",
                final_url=f"{SITE_URL}?coupon=SAVE10",
                percent_off=10,
            )
        with self.assertRaises(ValidationError):
            CreatePromotionAssetRequest(
                name="Direct Site Promo",
                promotion_target="Running Shoes",
                final_url=SITE_URL,
                percent_off=10,
                promotion_code="SAVE10",
            )


def row_campaign():
    return SimpleNamespace(campaign=SimpleNamespace(id=int(CAMPAIGN), name="ExampleShop Search ES", status=SimpleNamespace(name="PAUSED")))


def row_ad_group():
    return SimpleNamespace(ad_group=SimpleNamespace(id=int(AD_GROUP), name="ES-Category", status=SimpleNamespace(name="ENABLED")))


def row_promotion(
    asset_id,
    name="ES Promo 10%",
    target="Kids Decor",
    lang="es",
    percent_off_micros=100_000,
    money_micros=0,
    code="SALE10",
    occasion="BLACK_FRIDAY",
    r_start="2026-11-20",
    r_end="2026-11-30",
    url=MARKETPLACE_URL,
):
    return SimpleNamespace(
        asset=SimpleNamespace(
            id=int(asset_id),
            name=name,
            final_urls=[url],
            promotion_asset=SimpleNamespace(
                promotion_target=target,
                language_code=lang,
                percent_off=percent_off_micros,
                money_amount_off=SimpleNamespace(amount_micros=money_micros),
                promotion_code=code,
                occasion=SimpleNamespace(name=occasion) if occasion else SimpleNamespace(name="UNSPECIFIED"),
                redemption_start_date=r_start,
                redemption_end_date=r_end,
            ),
        )
    )


class FakePromotionAds:
    def __init__(self, campaigns=None, ad_groups=None, existing_promos=(), linked_campaign_ids=(), linked_ag_ids=(), read_back=True):
        self.campaigns = [row_campaign()] if campaigns is None else campaigns
        self.ad_groups = [row_ad_group()] if ad_groups is None else ad_groups
        self.existing_promos = list(existing_promos)
        self.linked_campaign_ids = list(linked_campaign_ids)
        self.linked_ag_ids = list(linked_ag_ids)
        self.read_back = read_back
        self.created_ops = []

        self.client = mock.MagicMock()
        self.ga = mock.MagicMock()
        self.ga.search.side_effect = self._search
        self.asset_svc = mock.MagicMock()
        self.camp_asset_svc = mock.MagicMock()
        self.ag_asset_svc = mock.MagicMock()
        self.camp_svc = mock.MagicMock()
        self.ag_svc = mock.MagicMock()

        self.asset_svc.asset_path.side_effect = lambda cid, aid: f"customers/{cid}/assets/{aid}"
        self.camp_svc.campaign_path.side_effect = lambda cid, camp_id: f"customers/{cid}/campaigns/{camp_id}"
        self.ag_svc.ad_group_path.side_effect = lambda cid, ag_id: f"customers/{cid}/adGroups/{ag_id}"
        self.asset_svc.mutate_assets.side_effect = self._mutate_assets

        self.client.enums.AssetTypeEnum.PROMOTION = "PROMOTION"
        self.client.enums.AssetFieldTypeEnum.PROMOTION = "PROMOTION"
        self.client.enums.PromotionExtensionDiscountModifierEnum.UP_TO = "UP_TO"
        self.client.enums.PromotionExtensionOccasionEnum.BLACK_FRIDAY = "BLACK_FRIDAY"
        self.client.get_service.side_effect = lambda name: {
            "GoogleAdsService": self.ga,
            "AssetService": self.asset_svc,
            "CampaignAssetService": self.camp_asset_svc,
            "AdGroupAssetService": self.ag_asset_svc,
            "CampaignService": self.camp_svc,
            "AdGroupService": self.ag_svc,
        }.get(name, mock.MagicMock())

    def _mutate_assets(self, customer_id, operations):
        self.created_ops.extend(operations)
        return mock.MagicMock(results=[mock.MagicMock(resource_name=f"customers/{customer_id}/assets/990001")])

    def _search(self, customer_id, query):
        if "FROM campaign_asset" in query:
            return [SimpleNamespace(campaign_asset=SimpleNamespace(asset=f"customers/{customer_id}/assets/{aid}"))
                    for aid in self.linked_campaign_ids]
        if "FROM ad_group_asset" in query:
            return [SimpleNamespace(ad_group_asset=SimpleNamespace(asset=f"customers/{customer_id}/assets/{aid}"))
                    for aid in self.linked_ag_ids]
        if "WHERE asset.id =" in query:
            if not self.read_back:
                return []
            op_asset = self.created_ops[-1].create
            p = op_asset.promotion_asset
            return [row_promotion(
                990001,
                name=op_asset.name,
                target=p.promotion_target,
                lang=p.language_code,
                percent_off_micros=p.percent_off if isinstance(p.percent_off, int) else 0,
                code=p.promotion_code if isinstance(p.promotion_code, str) else "",
                r_start=p.redemption_start_date if isinstance(p.redemption_start_date, str) else "",
                r_end=p.redemption_end_date if isinstance(p.redemption_end_date, str) else "",
                url=MARKETPLACE_URL,
            )]
        if "FROM asset" in query:
            return self.existing_promos
        if "FROM ad_group" in query:
            return self.ad_groups
        return self.campaigns

    def patched(self):
        return mock.patch("tools.asset_tools.get_ads_client", return_value=self.client)


class PromotionToolTests(AuditLogCase):
    def _req(self, **overrides):
        params = dict(
            name="ES Promo 10%",
            promotion_target="Kids Decor",
            final_url=MARKETPLACE_URL,
            language_code="es",
            percent_off=10,
            promotion_code="SALE10",
            occasion="BLACK_FRIDAY",
            redemption_start_date="2026-11-20",
            redemption_end_date="2026-11-30",
            campaign_id=CAMPAIGN,
        )
        params.update(overrides)
        return CreatePromotionAssetRequest(**params)

    def test_dry_run_previews_without_mutating_google(self):
        ads = FakePromotionAds()
        with ads.patched():
            result = create_promotion_asset(self._req(dry_run=True), customer_id="1112223334")
        self.assertTrue(result.success)
        self.assertIn("DRY RUN:", result.data)
        self.assertIn("10% off", result.data)
        ads.asset_svc.mutate_assets.assert_not_called()
        ads.camp_asset_svc.mutate_campaign_assets.assert_not_called()
        entry = self.log_entries()[-1]
        self.assertEqual(entry["function"], "create_promotion_asset")
        self.assertEqual(entry["action_type"], "MUTATION")
        self.assertTrue(entry["success"])

    def test_creates_with_exact_micro_percent_reads_back_links_and_logs_telemetry(self):
        ads = FakePromotionAds()
        with ads.patched():
            result = create_promotion_asset(self._req(), customer_id="1112223334")
        self.assertTrue(result.success)
        self.assertIn("Created Promotion Asset 990001", result.data)
        self.assertIn("10% off", result.data)
        self.assertIn(f"linked to Campaign {CAMPAIGN}", result.data)

        op = ads.asset_svc.mutate_assets.call_args.kwargs["operations"][0]
        self.assertEqual(op.create.promotion_asset.percent_off, 100_000)  # 10% in Google micro-percent (1,000,000 = 100%)
        self.assertEqual(op.create.promotion_asset.promotion_code, "SALE10")
        self.assertEqual(op.create.promotion_asset.occasion, "BLACK_FRIDAY")
        self.assertEqual(op.create.promotion_asset.redemption_start_date, "2026-11-20")
        self.assertEqual(op.create.promotion_asset.redemption_end_date, "2026-11-30")

        link_ops = ads.camp_asset_svc.mutate_campaign_assets.call_args.kwargs["operations"]
        self.assertEqual(len(link_ops), 1)
        self.assertEqual(link_ops[0].create.field_type, "PROMOTION")

        # Telemetry & audit trail check
        self.assertIsNotNone(result.telemetry.trace_id)
        self.assertIsNotNone(result.telemetry.span_id)
        entry = self.log_entries()[-1]
        self.assertEqual((entry["function"], entry["action_type"], entry["severity"], entry["success"]),
                         ("create_promotion_asset", "MUTATION", "NOTICE", True))
        self.assertIn("990001", entry["result"])

    def test_reuses_identical_existing_promotion_and_links_to_ad_group(self):
        ads = FakePromotionAds(existing_promos=[row_promotion(777777)])
        with ads.patched():
            result = create_promotion_asset(self._req(campaign_id=None, ad_group_id=AD_GROUP), customer_id="1112223334")
        self.assertTrue(result.success)
        self.assertIn("Reused existing Promotion Asset 777777", result.data)
        self.assertIn(f"linked to Ad Group {AD_GROUP}", result.data)
        ads.asset_svc.mutate_assets.assert_not_called()
        ads.ag_asset_svc.mutate_ad_group_assets.assert_called_once()

    def test_read_back_failure_returns_not_applied(self):
        ads = FakePromotionAds(read_back=False)
        with ads.patched():
            result = create_promotion_asset(self._req(), customer_id="1112223334")
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "NOT_APPLIED")
        ads.camp_asset_svc.mutate_campaign_assets.assert_not_called()

    def test_unknown_campaign_refuses_without_mutating(self):
        ads = FakePromotionAds(campaigns=[])
        with ads.patched():
            result = create_promotion_asset(self._req(), customer_id="1112223334")
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "CAMPAIGN_NOT_FOUND")
        ads.asset_svc.mutate_assets.assert_not_called()

    def test_server_registration_and_shield_rejection(self):
        import server
        tool = server.mcp._tool_manager._tools["create_promotion_asset"]
        self.assertTrue({"name", "promotion_target", "final_url", "percent_off", "promotion_code", "dry_run"}
                        <= set(tool.parameters["properties"]))
        with mock.patch("tools.asset_tools.get_ads_client", side_effect=AssertionError("Google must not be called")):
            answer = json.loads(server.create_promotion_asset(
                name="Bad Promo", promotion_target="x" * 25, final_url=MARKETPLACE_URL, percent_off=10))
        self.assertFalse(answer["success"])
        self.assertEqual(answer["error"]["code"], "REQUEST_REJECTED")
        entry = self.log_entries()[-1]
        self.assertEqual((entry["function"], entry["error_code"]), ("create_promotion_asset", "REQUEST_REJECTED"))


if __name__ == "__main__":
    unittest.main()
