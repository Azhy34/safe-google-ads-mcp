"""
create_shopping_campaign: a Standard Shopping campaign on the Merchant Center feed, for a chosen selection of products.

The client is the real GoogleAdsClient (fake credentials, nothing leaves the machine): types and enums are Google's own, so the
operations checked here are the ones Google would get. Only the services are mocked. The audit trail goes to a temporary file.
The readiness judgement (check_shopping_readiness) has its own tests; here it is a canned answer.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_shopping_campaign.py" -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from google.ads.googleads.errors import GoogleAdsException
from pydantic import ValidationError

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
import policy_env  # noqa: F401  (pins the fictional test policy before anything reads it)

from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials

from core.merchant_client import MerchantApiError
from shield import cloud_sink, error_shield
from shield.error_shield import MUTATION_FUNCTIONS
from shield.models_shopping import CreateShoppingCampaignRequest, ReadinessCheck, ShoppingReadinessResponse
from tools.shopping_tools import (
    build_campaign_operations, build_listing_tree_operations, create_shopping_campaign, criterion_as_mutate_operation,
)

NAME = "ExampleShop Shopping DE - Site"
CID = "1"
# What Google Ads lists for the feed, spelled as Google Ads spells it (here deliberately unlike the request, to show that its spelling
# wins; live it is the feed's own, SKU-003-L). (Before a Shopping campaign includes
# the products it lists every one of them as NOT_ELIGIBLE, seen live 31.10.2026: the tool must not read anything into that status.)
CATALOG = ["sku-003-clearance", "sku-003-L", "sku-003-M", "sku-001-clearance"]
SELECTION = ["SKU-003-clearance", "SKU-003-L", "SKU-003-M"]


def gmc_product(offer, state="APPROVED"):
    """A Merchant Center product as the Merchant API lists it, with its Shopping ads status for Germany."""
    key = {"APPROVED": "approvedCountries", "PENDING": "pendingCountries", "DISAPPROVED": "disapprovedCountries"}[state]
    return {"offerId": offer, "productStatus": {"destinationStatuses": [
        {"reportingContext": "FREE_LISTINGS", "approvedCountries": ["DE"]}, {"reportingContext": "SHOPPING_ADS", key: ["DE"]}]}}


GMC_CATALOG = [gmc_product(offer) for offer in (*SELECTION, "SKU-001-clearance")]


def real_client():
    return GoogleAdsClient(credentials=Credentials(token="t"), developer_token="d", use_proto_plus=True)


def request(**kw):
    """The whole feed unless a selection is given: a request without either is refused (ModelTests)."""
    if "offers" not in kw:
        kw.setdefault("all_products", True)
    return CreateShoppingCampaignRequest(name=kw.pop("name", NAME), **kw)


def readiness(*checks, verdict=None):
    checks = list(checks) or [ReadinessCheck(code="ADS_LINK", side="ADS", status="OK", detail="linked")]
    verdict = verdict or ("BLOCKED" if any(c.status == "FAIL" for c in checks) else
                          "READY_WITH_WARNINGS" if any(c.status in ("WARN", "SKIPPED") for c in checks) else "READY")
    return ShoppingReadinessResponse(customer_id=CID, merchant_id="1234567890", feed_label="EXAMPLE_FEED", countries=["DE"], verdict=verdict, checks=checks)


class ModelTests(unittest.TestCase):
    def test_defaults_are_the_agreed_start(self):
        req = request()
        self.assertEqual((req.budget_eur, req.cpc_ceiling_eur), (5.0, 0.50))
        self.assertEqual((req.merchant_id, req.feed_label), ("1234567890", "EXAMPLE_FEED"))
        self.assertEqual(req.geo_target_constants, [2276])                         # Germany only: Austria is a campaign of its own
        self.assertFalse(req.dry_run)

    def test_bad_input_is_refused(self):
        for bad in ({"name": "Shopping Site"},                    # not ours: GA4 and the sheet look for the prefix
                    {"name": "ExampleShop x'; DROP"},               # a quote would reach a GAQL query
                    {"budget_eur": 0.5}, {"budget_eur": 80},
                    {"cpc_ceiling_eur": 0.05}, {"cpc_ceiling_eur": 5},
                    {"merchant_id": "abc"}, {"feed_label": "bad label!"},
                    {"geo_target_constants": []}, {"campaign_priority": 3},
                    {"exclude_custom_label_0": ["Clearance; x"]},
                    {"offers": ["WE 003; DROP"]}, {"offers": ["x" * 61]}, {"offers": [f"SKU-{n}" for n in range(101)]}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                request(**bad)

    def test_a_campaign_needs_a_choice_of_products(self):
        with self.assertRaises(ValidationError) as caught:                       # the whole feed is never the default
            CreateShoppingCampaignRequest(name=NAME)
        self.assertIn("never the default", str(caught.exception))
        for bad in ({"offers": ["SKU-003-clearance"], "all_products": True},        # either a selection or the whole feed
                    {"offers": ["SKU-003-clearance"], "exclude_custom_label_0": ["clearance"]}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                CreateShoppingCampaignRequest(name=NAME, **bad)

    def test_exclusion_values_are_normalised(self):
        self.assertEqual(request(exclude_custom_label_0=[" Clearance ", "clearance", "Probe"]).exclude_custom_label_0, ["clearance", "probe"])

    def test_offers_are_trimmed_and_deduplicated_without_regard_to_case(self):
        self.assertEqual(request(offers=[" SKU-003-clearance ", "sku-003-CLEARANCE", "SKU-003-L"]).offers, ["SKU-003-clearance", "SKU-003-L"])

    def test_the_tool_is_a_mutation_and_is_registered(self):
        self.assertIn("create_shopping_campaign", MUTATION_FUNCTIONS)
        source = (PKG / "server.py").read_text(encoding="utf-8")
        self.assertIn("def create_shopping_campaign(", source)
        self.assertIn('build_request(\n        "create_shopping_campaign", CreateShoppingCampaignRequest', source)


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.client = real_client()
        self.ops = build_campaign_operations(self.client, CID, request())

    def of(self, kind):
        return [getattr(op, kind) for op in self.ops if op._pb.WhichOneof("operation") == kind]

    def test_one_budget_one_campaign_one_group_one_ad(self):
        self.assertEqual([len(self.of(k)) for k in ("campaign_budget_operation", "campaign_operation", "ad_group_operation", "ad_group_ad_operation")], [1] * 4)

    def test_budget_is_dedicated_and_in_cents(self):
        budget = self.of("campaign_budget_operation")[0].create
        self.assertEqual(budget.amount_micros, 5_000_000)
        self.assertFalse(budget.explicitly_shared)
        self.assertEqual(budget.resource_name, "customers/1/campaignBudgets/-1")

    def test_campaign_is_a_paused_shopping_campaign_on_maximize_clicks(self):
        campaign = self.of("campaign_operation")[0].create
        c = self.client.enums
        self.assertEqual(c.CampaignStatusEnum(campaign.status).name, "PAUSED")
        self.assertEqual(c.AdvertisingChannelTypeEnum(campaign.advertising_channel_type).name, "SHOPPING")
        self.assertEqual(campaign.shopping_setting.merchant_id, 1234567890)
        self.assertEqual(campaign.shopping_setting.feed_label, "EXAMPLE_FEED")
        self.assertEqual(campaign.target_spend.cpc_bid_ceiling_micros, 500_000)
        self.assertEqual(campaign.campaign_budget, "customers/1/campaignBudgets/-1")
        self.assertTrue(campaign.network_settings.target_google_search)
        self.assertFalse(campaign.network_settings.target_search_network)
        self.assertFalse(campaign.network_settings.target_content_network)
        self.assertEqual(c.PositiveGeoTargetTypeEnum(campaign.geo_target_type_setting.positive_geo_target_type).name, "PRESENCE")

    def test_targeting_is_germany_and_fourteen_schedule_windows_and_no_language(self):
        criteria = self.of("campaign_criterion_operation")
        geo = sorted(c.create.location.geo_target_constant for c in criteria if c.create.location.geo_target_constant)
        windows = [(c.create.ad_schedule.start_hour, c.create.ad_schedule.end_hour) for c in criteria if c.create.ad_schedule.end_hour]
        self.assertEqual(geo, ["geoTargetConstants/2276"])
        self.assertFalse([c for c in criteria if c.create.language.language_constant])   # Google refuses a language criterion on a Shopping campaign
        self.assertEqual(len(windows), 14)
        self.assertEqual(sorted(set(windows)), [(0, 1), (8, 24)])  # the night 01:00-08:00 is never open
        self.assertTrue(all(c.create.campaign == "customers/1/campaigns/-2" for c in criteria))

    def test_ad_group_is_a_shopping_group_and_the_ad_is_a_product_ad(self):
        group = self.of("ad_group_operation")[0].create
        ad = self.of("ad_group_ad_operation")[0].create
        self.assertEqual(self.client.enums.AdGroupTypeEnum(group.type_).name, "SHOPPING_PRODUCT_ADS")
        self.assertEqual(group.campaign, "customers/1/campaigns/-2")
        self.assertEqual(group.cpc_bid_micros, 500_000)
        self.assertEqual(ad.ad_group, group.resource_name)
        self.assertEqual(ad.ad._pb.WhichOneof("ad_data"), "shopping_product_ad")

    def test_the_budget_of_a_test_campaign_is_in_cents_too(self):
        ops = build_campaign_operations(self.client, CID, request(offers=SELECTION, budget_eur=2.0))
        budget = [op.campaign_budget_operation.create for op in ops if op._pb.WhichOneof("operation") == "campaign_budget_operation"][0]
        self.assertEqual(budget.amount_micros, 2_000_000)


class ListingTreeTests(unittest.TestCase):
    def setUp(self):
        self.client = real_client()

    def kind(self, node):
        return self.client.enums.ListingGroupTypeEnum(node.listing_group.type_).name

    def test_without_exclusions_one_root_includes_every_product(self):
        ops = build_listing_tree_operations(self.client, CID, "77", request())
        self.assertEqual(len(ops), 1)
        node = ops[0].create
        self.assertEqual(self.kind(node), "UNIT")
        self.assertFalse(node.negative)
        self.assertEqual(node.cpc_bid_micros, 500_000)
        self.assertEqual(node.ad_group, "customers/1/adGroups/77")

    def test_with_exclusions_a_subdivision_leaves_the_value_out_and_keeps_everything_else(self):
        ops = build_listing_tree_operations(self.client, CID, "77", request(exclude_custom_label_0=["clearance"]))
        root, excluded, rest = (op.create for op in ops)
        self.assertEqual(self.kind(root), "SUBDIVISION")
        self.assertEqual(root.resource_name, "customers/1/adGroupCriteria/77~-4")
        self.assertTrue(excluded.negative)
        self.assertEqual(excluded.listing_group.case_value.product_custom_attribute.value, "clearance")
        self.assertEqual(self.client.enums.ProductCustomAttributeIndexEnum(excluded.listing_group.case_value.product_custom_attribute.index).name, "INDEX0")
        self.assertEqual(excluded.listing_group.parent_ad_group_criterion, root.resource_name)
        self.assertFalse(rest.negative)                                            # everything else is advertised
        self.assertEqual(rest.listing_group.case_value.product_custom_attribute.value, "")   # the node without a value
        self.assertEqual(rest.cpc_bid_micros, 500_000)
        self.assertEqual(rest.listing_group.parent_ad_group_criterion, root.resource_name)

    def test_the_tree_travels_in_the_one_request_under_the_temporary_ad_group(self):
        ops = build_listing_tree_operations(self.client, CID, "-3", request(offers=SELECTION), ["a", "b", "c"])
        wrapped = [criterion_as_mutate_operation(self.client, op) for op in ops]
        self.assertTrue(all(op._pb.WhichOneof("operation") == "ad_group_criterion_operation" for op in wrapped))
        root, *_, rest = (op.ad_group_criterion_operation.create for op in wrapped)
        self.assertEqual((root.ad_group, root.resource_name), ("customers/1/adGroups/-3", "customers/1/adGroupCriteria/-3~-4"))
        self.assertEqual(rest.listing_group.parent_ad_group_criterion, "customers/1/adGroupCriteria/-3~-4")

    def test_every_temporary_id_of_the_request_is_used_once(self):
        # Google refuses a repeated temporary ID whatever the resource (DUPLICATE_TEMP_IDS, seen live 31.10.2026: the tree root reused the budget's -1)
        for req, item_ids in ((request(offers=SELECTION), ["a", "b", "c"]), (request(exclude_custom_label_0=["clearance"]), [])):
            tree = build_listing_tree_operations(self.client, CID, "-3", req, item_ids)
            operations = build_campaign_operations(self.client, CID, req) + [criterion_as_mutate_operation(self.client, op) for op in tree]
            kinds = ("campaign_budget_operation", "campaign_operation", "ad_group_operation", "ad_group_criterion_operation")
            names = [getattr(op, kind).create.resource_name for op in operations for kind in kinds
                     if op._pb.WhichOneof("operation") == kind and getattr(op, kind).create.resource_name]
            temp_ids = [name.rsplit("/", 1)[1].rsplit("~", 1)[-1] for name in names]
            self.assertEqual(len(temp_ids), 4)                                     # budget, campaign, ad group, root of the tree
            self.assertEqual(len(set(temp_ids)), 4, temp_ids)

    def test_a_selection_advertises_the_offers_and_leaves_everything_else_out(self):
        ids = ["sku-003-clearance", "sku-003-L", "sku-003-M"]
        ops = build_listing_tree_operations(self.client, CID, "77", request(offers=SELECTION), ids)
        root, *items, rest = (op.create for op in ops)
        self.assertEqual((self.kind(root), root.resource_name), ("SUBDIVISION", "customers/1/adGroupCriteria/77~-4"))
        self.assertEqual(root.cpc_bid_micros, 0)                                   # a subdivision takes no bid
        self.assertEqual([i.listing_group.case_value.product_item_id.value for i in items], ids)
        for item in items:
            self.assertEqual(self.kind(item), "UNIT")
            self.assertFalse(item.negative)
            self.assertEqual(item.cpc_bid_micros, 500_000)
            self.assertEqual(item.listing_group.parent_ad_group_criterion, root.resource_name)
        self.assertEqual(self.kind(rest), "UNIT")
        self.assertTrue(rest.negative)                                             # everything else is not advertised
        self.assertEqual(rest.listing_group.case_value._pb.WhichOneof("dimension"), "product_item_id")
        self.assertFalse(rest.listing_group.case_value.product_item_id._pb.HasField("value"))   # the node without a value
        self.assertEqual(rest.listing_group.parent_ad_group_criterion, root.resource_name)
        self.assertTrue(all(i.ad_group == "customers/1/adGroups/77" for i in (root, *items, rest)))


class ToolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(error_shield, "AUDIT_FILE", Path(tmp.name) / "audit.jsonl"),
                        mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
                        mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make(self, name_taken=False, mutate_error=None, catalog=None, ad_group_statuses=("ELIGIBLE",) * 3, products_report_fails=False):
        """A client whose GoogleAdsService is a stand-in; state["mutates"] holds (operations, validate_only) of every mutate."""
        client = real_client()
        ga = mock.MagicMock()
        state = {"mutates": []}
        catalog = CATALOG if catalog is None else catalog

        def search(customer_id, query):
            if "FROM campaign" in query and "campaign.name =" in query:
                return [object()] if name_taken else []
            if "shopping_product.ad_group =" in query:                                # the products of the new ad group
                if products_report_fails:
                    raise GoogleAdsException(mock.MagicMock(), mock.MagicMock(), mock.MagicMock(), "rid")
                return [SimpleNamespace(shopping_product=SimpleNamespace(item_id=f"x{n}", status=SimpleNamespace(name=status)))
                        for n, status in enumerate(ad_group_statuses)]
            if "FROM shopping_product" in query:
                return [SimpleNamespace(shopping_product=SimpleNamespace(
                    merchant_center_id=1234567890, feed_label="EXAMPLE_FEED", item_id=item)) for item in catalog]
            if "campaign_budget.amount_micros" in query:
                return [SimpleNamespace(
                    campaign=SimpleNamespace(
                        name=NAME, status=SimpleNamespace(name="PAUSED"), advertising_channel_type=SimpleNamespace(name="SHOPPING"),
                        shopping_setting=SimpleNamespace(merchant_id=1234567890, feed_label="EXAMPLE_FEED"),
                        target_spend=SimpleNamespace(cpc_bid_ceiling_micros=500_000)),
                    campaign_budget=SimpleNamespace(amount_micros=5_000_000))]
            if "listing_group.type" in query:
                return [SimpleNamespace(ad_group_criterion=SimpleNamespace(negative=False))]
            raise AssertionError(f"unexpected query: {query}")

        def mutate(request):
            state["mutates"].append((list(request.mutate_operations), request.validate_only))
            if mutate_error:
                raise mutate_error
            return SimpleNamespace(mutate_operation_responses=[
                SimpleNamespace(campaign_result=SimpleNamespace(resource_name="customers/1/campaigns/555"),
                                ad_group_result=SimpleNamespace(resource_name="")),
                SimpleNamespace(campaign_result=SimpleNamespace(resource_name=""),
                                ad_group_result=SimpleNamespace(resource_name="customers/1/adGroups/777"))])

        ga.search.side_effect = search
        ga.mutate.side_effect = mutate
        client.get_service = lambda name: ga
        return client, ga, state

    def run_tool(self, client, ready=None, gmc=None, **kw):
        """`ready` is the readiness answer; `gmc` the Merchant Center products (a list, or an exception to raise); by default all approved."""
        gmc = GMC_CATALOG if gmc is None else gmc
        pages = mock.MagicMock(side_effect=gmc) if isinstance(gmc, Exception) else mock.MagicMock(return_value=gmc)
        with mock.patch("tools.shopping_tools.get_ads_client", return_value=client), \
                mock.patch("tools.shopping_tools.assess_shopping_readiness", return_value=ready or readiness()), \
                mock.patch("tools.shopping_selection.merchant_pages", pages):
            return create_shopping_campaign(request(**kw), customer_id=CID)

    @staticmethod
    def written(state):
        """The mutates that really write: validate_only ones do not."""
        return [operations for operations, validate_only in state["mutates"] if not validate_only]

    @staticmethod
    def tree_of(operations, n):
        """The last n operations are the product tree; their criteria."""
        return [op.ad_group_criterion_operation.create for op in operations[-n:]]

    # the whole feed

    def test_dry_run_has_google_check_the_whole_request_and_writes_nothing(self):
        client, ga, state = self.make()
        result = self.run_tool(client, dry_run=True)
        self.assertTrue(result.success)
        self.assertIn("DRY RUN", result.data)
        self.assertIn("PAUSED", result.data)
        self.assertIn("the whole feed", result.data)
        self.assertIn("validate_only", result.data)
        self.assertEqual(len(state["mutates"]), 1)
        self.assertTrue(state["mutates"][0][1])                                  # validate_only
        self.assertEqual(self.written(state), [])
        self.assertEqual(len(state["mutates"][0][0]), 19 + 1)                    # the campaign and the one root node of the tree

    def test_create_sends_one_all_or_nothing_request_with_the_tree_and_reads_it_back(self):
        client, ga, state = self.make()
        result = self.run_tool(client)
        self.assertTrue(result.success)
        self.assertEqual(len(state["mutates"]), 1)                               # one request: a failure leaves nothing behind
        operations = self.written(state)[0]
        self.assertEqual(len(operations), 1 + 1 + 1 + 14 + 1 + 1 + 1)            # budget, campaign, 1 geo, 14 windows, group, ad, tree
        self.assertEqual(self.tree_of(operations, 1)[0].ad_group, "customers/1/adGroups/-3")   # the temporary ID of the ad group, resolved by Google
        self.assertIn("Shopping campaign 555", result.data)
        self.assertIn("PAUSED", result.data)
        self.assertIn("5.00 EUR", result.data)

    def test_a_taken_name_creates_nothing(self):
        client, ga, state = self.make(name_taken=True)
        result = self.run_tool(client)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "CAMPAIGN_NAME_EXISTS")
        self.assertEqual(state["mutates"], [])

    def test_a_blocked_readiness_creates_nothing_and_names_the_check(self):
        for check, code, field in (("ADS_LINK", "MERCHANT_CENTER_NOT_LINKED", "merchant_id"), ("GMC_PRODUCTS", "GMC_PRODUCTS", "merchant_id"),
                                   ("GMC_FEED_LABEL", "GMC_FEED_LABEL", "feed_label"), ("GMC_PROGRAM", "GMC_PROGRAM", "merchant_id")):
            client, ga, state = self.make()
            result = self.run_tool(client, ready=readiness(ReadinessCheck(code=check, side="GMC", status="FAIL", detail="it is not in order")))
            self.assertFalse(result.success, check)
            self.assertEqual((result.error.code, result.error.field_path), (code, [field]), check)
            self.assertIn("it is not in order", result.error.message)
            self.assertEqual(state["mutates"], [], check)

    def test_warnings_do_not_stop_the_campaign_and_are_shown(self):
        client, ga, state = self.make()
        warn = ReadinessCheck(code="GMC_PRODUCTS", side="GMC", status="WARN", detail="6 products wait for the first review")
        result = self.run_tool(client, ready=readiness(warn), dry_run=True)
        self.assertTrue(result.success)
        self.assertIn("READY_WITH_WARNINGS", result.data)
        self.assertIn("6 products wait for the first review", result.data)

    def test_when_google_refuses_the_request_nothing_more_is_sent(self):
        for dry_run in (True, False):
            client, ga, state = self.make(mutate_error=RuntimeError("context_error.OPERATION_NOT_PERMITTED_FOR_CONTEXT"))
            result = self.run_tool(client, dry_run=dry_run)
            self.assertFalse(result.success, dry_run)
            self.assertEqual(len(state["mutates"]), 1, dry_run)                  # no second request, no half-built campaign

    # a selection

    def test_dry_run_of_a_selection_resolves_the_offers_and_writes_nothing(self):
        client, ga, state = self.make()
        result = self.run_tool(client, offers=SELECTION, dry_run=True, budget_eur=2.0)
        self.assertTrue(result.success)
        self.assertIn("only 3 offer(s) (sku-003-clearance, sku-003-L, sku-003-M)", result.data)   # as Google Ads spells them
        self.assertIn("found in google_ads+merchant_center", result.data)
        self.assertIn("2.00 EUR a day", result.data)
        self.assertIn("24 operations (19 for the campaign, 5 for the product tree)", result.data)      # root, 3 offers, everything else
        self.assertEqual(self.written(state), [])

    def test_creating_a_selection_builds_the_item_tree_and_reports_what_google_lists(self):
        client, ga, state = self.make()
        result = self.run_tool(client, offers=SELECTION)
        self.assertTrue(result.success)
        tree = self.tree_of(self.written(state)[0], 5)
        self.assertEqual([node.listing_group.case_value.product_item_id.value for node in tree[1:4]],
                         ["sku-003-clearance", "sku-003-L", "sku-003-M"])
        self.assertTrue(tree[4].negative)
        self.assertIn("Google Ads lists 3 product(s) for the ad group: ELIGIBLE 3", result.data)

    def test_the_products_of_the_ad_group_are_reported_by_status_not_by_the_feed(self):
        # the campaign scope lists the whole feed (528 live), so the read-back asks for the ad group, where the product tree acts
        client, ga, state = self.make(ad_group_statuses=("ELIGIBLE", "NOT_ELIGIBLE", "NOT_ELIGIBLE"))
        result = self.run_tool(client, offers=SELECTION)
        self.assertTrue(result.success)
        self.assertIn("Google Ads lists 3 product(s) for the ad group: ELIGIBLE 1, NOT_ELIGIBLE 2", result.data)
        self.assertIn("3 product(s) were selected", result.data)

    def test_an_empty_report_right_after_creation_is_said_plainly(self):
        client, ga, state = self.make(ad_group_statuses=())
        result = self.run_tool(client, offers=SELECTION)
        self.assertTrue(result.success)
        self.assertIn("Google Ads lists 0 product(s) for the ad group: nothing yet", result.data)

    def test_an_unreadable_products_report_is_said_not_hidden(self):
        client, ga, state = self.make(products_report_fails=True)
        result = self.run_tool(client, offers=SELECTION)
        self.assertTrue(result.success)
        self.assertIn("the products report could not be read", result.data)

    def test_an_offer_the_catalog_does_not_have_is_refused(self):
        client, ga, state = self.make()
        result = self.run_tool(client, offers=["SKU-003-L", "SKU-003-150x25O"], gmc=[gmc_product("SKU-003-L")])   # a letter O for a zero
        self.assertFalse(result.success)
        self.assertEqual((result.error.code, result.error.field_path), ("OFFER_NOT_FOUND", ["offers"]))
        self.assertIn("SKU-003-150x25O", result.error.message)
        self.assertEqual(state["mutates"], [])

    def test_an_offer_merchant_center_disapproved_is_refused(self):
        client, ga, state = self.make()
        result = self.run_tool(client, offers=["SKU-003-clearance", "SKU-001-clearance"],
                               gmc=[gmc_product("SKU-003-clearance"), gmc_product("SKU-001-clearance", "DISAPPROVED")])
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "OFFER_NOT_ELIGIBLE")
        self.assertIn("Merchant Center disapproved SKU-001-clearance", result.error.message)
        self.assertEqual(state["mutates"], [])

    def test_the_google_ads_status_is_not_read_as_a_verdict_on_the_product(self):
        # before the first Shopping campaign Google Ads lists all products as NOT_ELIGIBLE: the offers still go through
        client, ga, state = self.make()
        result = self.run_tool(client, offers=SELECTION, dry_run=True)
        self.assertTrue(result.success)
        self.assertNotIn("NOT_ELIGIBLE", result.data)

    def test_an_offer_waiting_for_its_first_review_is_allowed_and_said(self):
        client, ga, state = self.make()
        result = self.run_tool(client, offers=SELECTION, dry_run=True,
                               gmc=[gmc_product("SKU-003-clearance", "PENDING"), gmc_product("SKU-003-L"), gmc_product("SKU-003-M")])
        self.assertTrue(result.success)
        self.assertIn("waiting for the first review in Merchant Center, they show once approved: SKU-003-clearance", result.data)

    def test_when_merchant_center_is_silent_the_offers_go_through_with_a_note(self):
        client, ga, state = self.make()
        result = self.run_tool(client, offers=SELECTION, dry_run=True, gmc=MerchantApiError("GCLOUD_NOT_FOUND", "no gcloud"))
        self.assertTrue(result.success)
        self.assertIn("approval in Merchant Center could not be checked", result.data)

    def test_when_google_ads_lists_nothing_yet_the_merchant_center_catalog_is_used(self):
        client, ga, state = self.make(catalog=[])
        result = self.run_tool(client, offers=["SKU-003-clearance", "sku-003-L"], dry_run=True)
        self.assertTrue(result.success)
        self.assertIn("only 2 offer(s) (SKU-003-clearance, SKU-003-L), found in merchant_center", result.data)   # the catalog's own spelling

    def test_when_nothing_can_list_the_catalog_nothing_is_created(self):
        client, ga, state = self.make(catalog=[])
        result = self.run_tool(client, offers=SELECTION, gmc=MerchantApiError("GCLOUD_NOT_FOUND", "no gcloud"))
        self.assertFalse(result.success)
        self.assertEqual((result.error.code, result.error.field_path), ("CATALOG_NOT_READABLE", ["offers"]))
        self.assertEqual(state["mutates"], [])


if __name__ == "__main__":
    unittest.main()
