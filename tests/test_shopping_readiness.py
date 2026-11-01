"""
check_shopping_readiness: can a Shopping campaign on the Merchant Center feed show a product today?

Nothing leaves the machine: Google Ads rows are SimpleNamespace stand-ins, Merchant API answers are the shapes seen live on
31.10.2026 (counts as strings, service rows of other contexts), HTTP and gcloud are patched. The audit trail goes to a temporary file.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_shopping_readiness.py" -v
"""
import json
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

from core import merchant_client
from core.merchant_client import MerchantApiError, merchant_request
from shield import cloud_sink, error_shield, telemetry
from shield.error_shield import MUTATION_FUNCTIONS
from shield.models_shopping import CheckShoppingReadinessRequest
from shield.telemetry import redact_secrets
from tools import shopping_readiness_tools as readiness
from tools.shopping_readiness_tools import AdsFacts, GmcState, check_shopping_readiness, evaluate_checks, read_gmc_state, verdict_of

CID = "1"
FAKE_TOKEN = "ya29." + "A" * 120


def request(**kw):
    return CheckShoppingReadinessRequest(**kw)


def healthy_gmc(**kw):
    state = GmcState(
        program={"state": "ENABLED", "activeRegionCodes": ["AT", "DE", "FR"],
                 "unmetRequirements": [{"title": "HAS_KOREAN_BUSINESS_REGISTRATION_NUMBER", "affectedRegionCodes": ["KR"]}]},
        feed_labels={"EXAMPLE_FEED"},
        counts={"DE": {"active": 521, "pending": 0, "disapproved": 0}, "AT": {"active": 521, "pending": 0, "disapproved": 0}},
        custom_label_0={"muster", "wand"})
    for key, value in kw.items():
        setattr(state, key, value)
    return state


def healthy_ads(**kw):
    facts = AdsFacts(linked=True, product_statuses={"ELIGIBLE": 520, "ELIGIBLE_LIMITED": 8})
    for key, value in kw.items():
        setattr(facts, key, value)
    return facts


def judge(req=None, countries=("DE",), unmapped=(), ads=None, gmc=None):
    return evaluate_checks(req or request(), list(countries), list(unmapped), ads or healthy_ads(), gmc or healthy_gmc())


def by_code(checks):
    return {check.code: check for check in checks}


class ModelTests(unittest.TestCase):
    def test_defaults_are_our_shop(self):
        req = request()
        self.assertEqual((req.merchant_id, req.feed_label, req.geo_target_constants, req.custom_label_0_values),
                         ("1234567890", "EXAMPLE_FEED", [2276], []))

    def test_bad_input_is_refused(self):
        for bad in ({"merchant_id": "abc"}, {"feed_label": "bad label!"}, {"geo_target_constants": []},
                    {"custom_label_0_values": ["Muster; x"]}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                request(**bad)

    def test_label_values_are_normalised(self):
        self.assertEqual(request(custom_label_0_values=[" Muster ", "muster", "Wand"]).custom_label_0_values, ["muster", "wand"])

    def test_the_tool_is_a_registered_read(self):
        import server
        self.assertIn("check_shopping_readiness", server.mcp._tool_manager._tools)
        self.assertNotIn("check_shopping_readiness", MUTATION_FUNCTIONS)


class JudgingTests(unittest.TestCase):
    def test_everything_in_order_is_ready(self):
        checks = judge()
        self.assertEqual([c.status for c in checks], ["OK"] * 6)
        self.assertEqual(verdict_of(checks), "READY")

    def test_an_unmet_requirement_of_another_country_is_not_ours(self):
        self.assertEqual(by_code(judge())["GMC_PROGRAM"].status, "OK")   # the live program lists Korea; the shop sells to DE and AT

    def test_an_unmet_requirement_of_our_country_warns(self):
        gmc = healthy_gmc(program={"state": "ENABLED", "activeRegionCodes": ["DE"],
                                   "unmetRequirements": [{"title": "NEEDS_X", "affectedRegionCodes": ["DE"]}]})
        check = by_code(judge(gmc=gmc))["GMC_PROGRAM"]
        self.assertEqual(check.status, "WARN")
        self.assertIn("NEEDS_X", check.detail)

    def test_program_off_or_missing_a_country_blocks(self):
        off = by_code(judge(gmc=healthy_gmc(program={"state": "ELIGIBLE", "activeRegionCodes": []})))["GMC_PROGRAM"]
        self.assertEqual(off.status, "FAIL")
        no_at = by_code(judge(countries=("DE", "AT"), gmc=healthy_gmc(program={"state": "ENABLED", "activeRegionCodes": ["DE"]})))["GMC_PROGRAM"]
        self.assertEqual(no_at.status, "FAIL")
        self.assertIn("AT", no_at.detail)

    def test_a_geo_target_outside_the_map_warns(self):
        check = by_code(judge(unmapped=(2840,)))["GMC_PROGRAM"]
        self.assertEqual(check.status, "WARN")
        self.assertIn("2840", check.detail)

    def test_unknown_feed_label_blocks(self):
        check = by_code(judge(gmc=healthy_gmc(feed_labels={"OTHER"})))["GMC_FEED_LABEL"]
        self.assertEqual(check.status, "FAIL")
        self.assertIn("OTHER", check.detail)

    def test_products_no_approved_blocks_pending_warns_no_summary_warns(self):
        def products(counts):
            return by_code(judge(gmc=healthy_gmc(counts=counts)))["GMC_PRODUCTS"]
        self.assertEqual(products({"DE": {"active": 0, "pending": 6, "disapproved": 0}}).status, "FAIL")
        self.assertEqual(products({"DE": {"active": 521, "pending": 6, "disapproved": 0}}).status, "WARN")
        self.assertEqual(products({"DE": {"active": 5, "pending": 0, "disapproved": 2}}).status, "WARN")
        self.assertEqual(products({}).status, "WARN")
        self.assertEqual(products({"AT": {"active": 9, "pending": 0, "disapproved": 0}}).status, "WARN")   # DE has no row yet

    def test_what_google_ads_sees(self):
        def ads_check(statuses):
            return by_code(judge(ads=healthy_ads(product_statuses=statuses)))["ADS_SEES_PRODUCTS"]
        self.assertEqual(ads_check({"ELIGIBLE": 5}).status, "OK")
        self.assertEqual(ads_check({}).status, "WARN")
        self.assertEqual(ads_check({"NOT_ELIGIBLE": 3}).status, "OK")    # seen live: no Shopping campaign yet, so every product is NOT_ELIGIBLE
        self.assertEqual(ads_check(None).status, "WARN")

    def test_not_eligible_everywhere_warns_once_a_shopping_campaign_exists(self):
        ads = healthy_ads(product_statuses={"NOT_ELIGIBLE": 3}, other_campaigns=["ExampleShop Shopping Old"])
        self.assertEqual(by_code(judge(ads=ads))["ADS_SEES_PRODUCTS"].status, "WARN")

    def test_custom_label_is_checked_only_when_asked_and_case_matters(self):
        self.assertNotIn("GMC_CUSTOM_LABEL", by_code(judge()))
        asked = request(custom_label_0_values=["muster"])
        self.assertEqual(by_code(judge(req=asked))["GMC_CUSTOM_LABEL"].status, "OK")
        missing = by_code(judge(req=request(custom_label_0_values=["ads"])))["GMC_CUSTOM_LABEL"]
        self.assertEqual(missing.status, "FAIL")
        self.assertIn("ads", missing.detail)
        wrong_case = by_code(judge(req=asked, gmc=healthy_gmc(custom_label_0={"Muster"})))["GMC_CUSTOM_LABEL"]
        self.assertEqual(wrong_case.status, "FAIL")   # the tree node would say 'muster'

    def test_no_labels_in_the_catalog_says_so(self):
        check = by_code(judge(req=request(custom_label_0_values=["muster"]), gmc=healthy_gmc(custom_label_0=set())))["GMC_CUSTOM_LABEL"]
        self.assertEqual(check.status, "FAIL")
        self.assertIn("no labels at all", check.detail)

    def test_other_shopping_campaigns_on_the_same_feed_warn(self):
        check = by_code(judge(ads=healthy_ads(other_campaigns=["ExampleShop Shopping Old"])))["SHOPPING_OVERLAP"]
        self.assertEqual(check.status, "WARN")
        self.assertIn("ExampleShop Shopping Old", check.detail)

    def test_no_link_blocks_and_names_a_waiting_invitation(self):
        plain = by_code(judge(ads=healthy_ads(linked=False)))["ADS_LINK"]
        self.assertEqual(plain.status, "FAIL")
        waiting = by_code(judge(ads=healthy_ads(linked=False, pending_invitation="PENDING_APPROVAL")))["ADS_LINK"]
        self.assertIn("PENDING_APPROVAL", waiting.detail)

    def test_unreadable_merchant_center_skips_its_checks_and_never_blocks(self):
        gmc = GmcState(skipped="IMPERSONATION_DENIED", skipped_detail="no right")
        checks = by_code(judge(req=request(custom_label_0_values=["muster"]), gmc=gmc))
        for code in ("GMC_PROGRAM", "GMC_FEED_LABEL", "GMC_PRODUCTS", "GMC_CUSTOM_LABEL"):
            self.assertEqual(checks[code].status, "SKIPPED", code)
            self.assertIn("IMPERSONATION_DENIED", checks[code].detail)
        self.assertEqual(checks["ADS_LINK"].status, "OK")
        self.assertEqual(verdict_of(list(checks.values())), "READY_WITH_WARNINGS")

    def test_verdict(self):
        self.assertEqual(verdict_of(judge(gmc=healthy_gmc(feed_labels={"OTHER"}))), "BLOCKED")
        self.assertEqual(verdict_of(judge(ads=healthy_ads(product_statuses={}))), "READY_WITH_WARNINGS")


def search_payloads():
    """The Merchant API as seen live: counts are strings, other contexts come as extra rows."""
    return {
        "programs": {"name": "accounts/1234567890/programs/shopping-ads", "state": "ENABLED", "activeRegionCodes": ["AT", "DE"],
                     "unmetRequirements": [{"title": "HAS_KOREAN_BUSINESS_REGISTRATION_NUMBER", "affectedRegionCodes": ["KR"]}]},
        "dataSources": {"dataSources": [{"name": "accounts/1234567890/dataSources/1",
                                         "primaryProductDataSource": {"feedLabel": "EXAMPLE_FEED", "contentLanguage": "de"}},
                                        {"name": "accounts/1234567890/dataSources/2", "supplementalProductDataSource": {}}]},
        "aggregateProductStatuses": {"aggregateProductStatuses": [
            {"name": "x/SHOPPING_ADS~DE", "reportingContext": "SHOPPING_ADS", "country": "DE", "stats": {"activeCount": "521", "pendingCount": "6"}},
            {"name": "x/SHOPPING_ADS~AT", "reportingContext": "SHOPPING_ADS", "country": "AT", "stats": {"activeCount": "521", "pendingCount": "6"}},
            {"name": "x/FREE_LISTINGS~DE", "reportingContext": "FREE_LISTINGS", "country": "DE", "stats": {"activeCount": "296", "pendingCount": "231"}},
            {"name": "x/UNSPEC", "reportingContext": "REPORTING_CONTEXT_ENUM_UNSPECIFIED", "country": "DE", "stats": {"disapprovedCount": "9"}}]},
        "products": {"products": [{"productAttributes": {"customLabel0": "muster"}}, {"productAttributes": {}}]},
    }


class ReadingTests(unittest.TestCase):
    def patched(self, payloads=None, fail=None):
        """merchant_request / merchant_pages stand-ins keyed by the request label; `fail` maps a label to an error code."""
        payloads, calls = payloads or search_payloads(), []

        def answer(label, key=None):
            calls.append(label)
            if label in (fail or {}):
                raise MerchantApiError(fail[label], f"{label} failed")
            return payloads[label.split(".")[0]]

        def one(method, path, label, **kw):
            return answer(label)

        def pages(path, key, label, **kw):
            return answer(label)[key]

        return calls, mock.patch.multiple(readiness, merchant_request=one, merchant_pages=pages)

    def test_a_healthy_read_gives_every_part(self):
        calls, patcher = self.patched()
        with patcher:
            state = read_gmc_state(request())
        self.assertEqual(state.program["state"], "ENABLED")
        self.assertEqual(state.feed_labels, {"EXAMPLE_FEED"})          # the supplemental source has no label of its own
        self.assertEqual(state.counts, {"DE": {"active": 521, "pending": 6, "disapproved": 0},
                                        "AT": {"active": 521, "pending": 6, "disapproved": 0}})   # only the Shopping ads context
        self.assertIsNone(state.skipped)
        self.assertNotIn("products.list", calls)                          # products are read only when a label is asked for

    def test_custom_labels_are_read_when_asked(self):
        calls, patcher = self.patched()
        with patcher:
            state = read_gmc_state(request(custom_label_0_values=["muster"]))
        self.assertEqual(state.custom_label_0, {"muster"})

    def test_a_failure_of_access_skips_everything_after_it(self):
        calls, patcher = self.patched(fail={"programs.get": "IMPERSONATION_DENIED"})
        with patcher:
            state = read_gmc_state(request())
        self.assertEqual(state.skipped, "IMPERSONATION_DENIED")
        self.assertEqual(calls, ["programs.get"])                         # the next requests were not even tried
        self.assertIsNone(state.counts)

    def test_a_failure_of_one_part_leaves_the_others(self):
        calls, patcher = self.patched(fail={"dataSources.list": "RATE_LIMIT_EXCEEDED"})
        with patcher:
            state = read_gmc_state(request())
        self.assertIsNone(state.skipped)
        self.assertEqual(state.errors, {"feed_labels": "RATE_LIMIT_EXCEEDED"})
        self.assertIsNotNone(state.counts)

    def test_a_foreign_merchant_center_is_not_read(self):
        calls, patcher = self.patched()
        with patcher:
            state = read_gmc_state(request(merchant_id="123456"))
        self.assertEqual((state.skipped, calls), ("FOREIGN_MERCHANT_ID", []))


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class TransportTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(merchant_client._token, {"value": None, "until": 0.0})
        patcher.start()
        self.addCleanup(patcher.stop)

    def mint(self, returncode=0, stdout=FAKE_TOKEN, stderr=""):
        return (mock.patch.object(merchant_client.shutil, "which", return_value="gcloud"),
                mock.patch.object(merchant_client.subprocess, "run",
                                  return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)))

    def test_the_token_is_minted_once_and_sent_as_a_bearer(self):
        which, run = self.mint()
        with which, run as run_mock, mock.patch.object(merchant_client.requests, "request", return_value=FakeResponse(payload={"a": 1})) as http:
            self.assertEqual(merchant_request("GET", "accounts/v1/accounts/1/programs/shopping-ads", "programs.get"), {"a": 1})
            merchant_request("GET", "accounts/v1/accounts/1/programs/shopping-ads", "programs.get")
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(http.call_args.kwargs["headers"], {"Authorization": f"Bearer {FAKE_TOKEN}"})
        self.assertIn("--impersonate-service-account=merchant-reader@example-project.iam.gserviceaccount.com", run_mock.call_args.args[0])

    def test_minting_the_token_is_a_line_of_the_journal_block_and_a_cached_token_is_not(self):
        which, run = self.mint()
        tokens = telemetry.start_call_extras()
        try:
            with which, run, mock.patch.object(merchant_client.requests, "request", return_value=FakeResponse(payload={})):
                merchant_request("GET", "accounts/v1/accounts/1/x", "x")
                merchant_request("GET", "accounts/v1/accounts/1/x", "x")
            self.assertEqual([c["method"] for c in telemetry.captured_merchant_calls()], ["access_token", "x", "x"])   # minted once
            self.assertEqual({c["status"] for c in telemetry.captured_merchant_calls()}, {"OK"})
        finally:
            telemetry.end_call_extras(tokens)

    def test_a_failed_mint_is_recorded_with_its_code(self):
        which, run = self.mint(returncode=1, stdout="", stderr="ERROR: PERMISSION_DENIED: iam.serviceAccounts.getAccessToken")
        tokens = telemetry.start_call_extras()
        try:
            with which, run, self.assertRaises(MerchantApiError):
                merchant_request("GET", "accounts/v1/accounts/1/x", "x")
            self.assertEqual([(c["method"], c["status"]) for c in telemetry.captured_merchant_calls()], [("access_token", "IMPERSONATION_DENIED")])
        finally:
            telemetry.end_call_extras(tokens)

    def test_it_only_reads(self):
        for method, path in (("DELETE", "accounts/v1/accounts/1/x"), ("POST", "products/v1/accounts/1/productInputs:insert"),
                             ("PATCH", "accounts/v1/accounts/1/x")):
            with self.assertRaises(ValueError, msg=f"{method} {path}"):
                merchant_request(method, path, "x")
        which, run = self.mint()
        with which, run, mock.patch.object(merchant_client.requests, "request", return_value=FakeResponse(payload={})) as http:
            merchant_request("POST", "reports/v1/accounts/1/reports:search", "reports.search", json_body={"query": "SELECT id FROM product_view"})
        self.assertEqual(http.call_args.args[0], "POST")

    def test_a_dead_gcloud_session_and_a_missing_right_have_their_own_codes(self):
        for stderr, code in (("ERROR: PERMISSION_DENIED: iam.serviceAccounts.getAccessToken", "IMPERSONATION_DENIED"),
                             ("Reauthentication failed. Please run gcloud auth login", "GCLOUD_REAUTH_NEEDED"),
                             ("something else", "TOKEN_MINT_FAILED")):
            which, run = self.mint(returncode=1, stdout="", stderr=stderr)
            with which, run, self.assertRaises(MerchantApiError) as caught:
                merchant_request("GET", "accounts/v1/accounts/1/x", "x")
            self.assertEqual(caught.exception.code, code)
            self.assertIn(code, merchant_client.ACCESS_FAILURE_CODES)

    def test_http_errors_are_classified(self):
        which, run = self.mint()
        cases = ((404, {"error": {"message": "nope"}}, "RESOURCE_NOT_FOUND"), (429, {}, "RATE_LIMIT_EXCEEDED"), (503, {}, "UPSTREAM_ERROR"),
                 (401, {"error": {"message": "GCP project is not registered with the merchant account"}}, "GCP_NOT_REGISTERED"))
        for status, payload, code in cases:
            with which, run, mock.patch.object(merchant_client.requests, "request", return_value=FakeResponse(status, payload)):
                with self.assertRaises(MerchantApiError) as caught:
                    merchant_request("GET", "accounts/v1/accounts/1/x", "x")
            self.assertEqual(caught.exception.code, code)

    def test_a_plain_401_is_retried_once_with_a_fresh_token(self):
        which, run = self.mint()
        answers = [FakeResponse(401, {"error": {"message": "expired"}}), FakeResponse(payload={"ok": True})]
        with which, run as run_mock, mock.patch.object(merchant_client.requests, "request", side_effect=answers):
            self.assertEqual(merchant_request("GET", "accounts/v1/accounts/1/x", "x"), {"ok": True})
        self.assertEqual(run_mock.call_count, 2)

    def test_a_timeout_is_a_typed_error(self):
        which, run = self.mint()
        with which, run, mock.patch.object(merchant_client.requests, "request", side_effect=merchant_client.requests.exceptions.Timeout()):
            with self.assertRaises(MerchantApiError) as caught:
                merchant_request("GET", "accounts/v1/accounts/1/x", "x")
        self.assertEqual(caught.exception.code, "REQUEST_TIMEOUT")

    def test_pages_are_followed_and_a_runaway_list_is_an_error(self):
        which, run = self.mint()
        pages = [FakeResponse(payload={"items": [1], "nextPageToken": "t"}), FakeResponse(payload={"items": [2]})]
        with which, run, mock.patch.object(merchant_client.requests, "request", side_effect=pages) as http:
            self.assertEqual(merchant_client.merchant_pages("accounts/v1/accounts/1/x", "items", "x.list"), [1, 2])
        self.assertEqual(http.call_args.kwargs["params"], {"pageSize": 250, "pageToken": "t"})
        endless = mock.Mock(return_value=FakeResponse(payload={"items": [1], "nextPageToken": "t"}))
        with which, run, mock.patch.object(merchant_client.requests, "request", endless):
            with self.assertRaises(MerchantApiError) as caught:
                merchant_client.merchant_pages("accounts/v1/accounts/1/x", "items", "x.list", max_pages=3)
        self.assertEqual(caught.exception.code, "TOO_MANY_PAGES")

    def test_a_token_is_masked_even_when_it_is_far_longer_than_a_regular_one(self):
        text = redact_secrets(f"Authorization: Bearer {'ya29.c.' + 'x' * 900}")
        self.assertIn("[REDACTED_GADS_BEARER]", text)
        self.assertNotIn("xxxxxxxxxx", text)


class ToolTests(unittest.TestCase):
    """The decorated tool end to end: a result, and a journal entry with the Merchant API block, the verdict and no token."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.audit = Path(tmp.name) / "audit.jsonl"
        for patcher in (mock.patch.object(error_shield, "AUDIT_FILE", self.audit),
                        mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
                        mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"}),
                        mock.patch.dict(merchant_client._token, {"value": FAKE_TOKEN, "until": float("inf")})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def ads_client(self, linked=True, shopping_product_fails=False):
        ga = mock.MagicMock()
        merchant = lambda: SimpleNamespace(merchant_center_id=1234567890 if linked else 1)

        def search(customer_id, query):
            if "FROM product_link_invitation" in query:
                return []
            if "FROM product_link" in query:
                return [SimpleNamespace(product_link=SimpleNamespace(merchant_center=merchant()))]
            if "FROM shopping_product" in query:
                if shopping_product_fails:
                    raise GoogleAdsException(mock.MagicMock(), mock.MagicMock(), mock.MagicMock(), "rid")
                return [SimpleNamespace(shopping_product=SimpleNamespace(
                    merchant_center_id=1234567890, feed_label="EXAMPLE_FEED", status=SimpleNamespace(name="ELIGIBLE")))] * 3
            if "FROM campaign" in query:
                return []
            raise AssertionError(f"unexpected query: {query}")

        ga.search.side_effect = search
        return SimpleNamespace(get_service=lambda name: ga)

    def merchant_http(self, payloads=None):
        payloads = payloads or search_payloads()

        def answer(method, url, **kw):
            for marker, key in (("programs/shopping-ads", "programs"), ("dataSources", "dataSources"),
                                ("aggregateProductStatuses", "aggregateProductStatuses"), ("/products", "products")):
                if marker in url:
                    return FakeResponse(payload=payloads[key])
            raise AssertionError(f"unexpected request: {url}")

        return mock.patch.object(merchant_client.requests, "request", side_effect=answer)

    def run_tool(self, client, **kw):
        with mock.patch("tools.shopping_readiness_tools.get_ads_client", return_value=client):
            return check_shopping_readiness(request(**kw), customer_id=CID)

    def entry(self):
        return json.loads(self.audit.read_text(encoding="utf-8").strip().splitlines()[-1])

    def test_healthy_account_is_ready_with_a_warning_for_the_review_queue(self):
        with self.merchant_http():
            result = self.run_tool(self.ads_client())
        self.assertTrue(result.success)
        data = result.data
        self.assertEqual(data.verdict, "READY_WITH_WARNINGS")             # 6 products wait for the first review
        self.assertEqual({c.code: c.status for c in data.checks}["GMC_PRODUCTS"], "WARN")
        self.assertEqual(data.gmc["product_counts"]["DE"], {"active": 521, "pending": 6, "disapproved": 0})
        self.assertEqual(data.ads["product_statuses"], {"ELIGIBLE": 3})

    def test_the_journal_entry_carries_the_merchant_block_and_the_verdict(self):
        with self.merchant_http():
            self.run_tool(self.ads_client())
        entry = self.entry()
        self.assertEqual((entry["function"], entry["action_type"], entry["severity"], entry["success"]), ("check_shopping_readiness", "READ", "INFO", True))
        self.assertEqual(entry["readiness_verdict"], "READY_WITH_WARNINGS")
        self.assertEqual(entry["warned_checks"], ["GMC_PRODUCTS"])
        self.assertNotIn("failed_checks", entry)
        self.assertEqual(entry["merchant_api"]["calls"], 3)
        self.assertEqual(entry["merchant_api"]["failed"], 0)
        self.assertEqual(entry["merchant_api"]["methods"], ["aggregateProductStatuses.list", "dataSources.list", "programs.get"])

    def test_no_token_reaches_the_journal(self):
        with self.merchant_http():
            self.run_tool(self.ads_client())
        self.assertNotIn("ya29", self.audit.read_text(encoding="utf-8"))

    def test_unreachable_merchant_center_is_a_reason_in_the_journal_not_a_failure(self):
        with mock.patch.dict(merchant_client._token, {"value": None, "until": 0.0}), \
                mock.patch.object(merchant_client.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"GCLOUD_PATH": ""}):
            result = self.run_tool(self.ads_client())
        self.assertTrue(result.success)
        self.assertEqual(result.data.verdict, "READY_WITH_WARNINGS")
        entry = self.entry()
        self.assertEqual(entry["gmc_skipped_reason"], "GCLOUD_NOT_FOUND")
        self.assertEqual((entry["merchant_api"]["calls"], entry["merchant_api"]["failed"], entry["merchant_api"]["methods"]),
                         (1, 1, ["access_token"]))                       # no request was made, the failed token is the one line

    def test_a_blocked_account_is_logged_with_the_failed_checks(self):
        with self.merchant_http():
            result = self.run_tool(self.ads_client(linked=False))
        self.assertEqual(result.data.verdict, "BLOCKED")
        self.assertEqual(self.entry()["failed_checks"], ["ADS_LINK"])

    def test_an_unreadable_shopping_product_report_is_a_warning_not_a_failure(self):
        with self.merchant_http():
            result = self.run_tool(self.ads_client(shopping_product_fails=True))
        self.assertTrue(result.success)
        self.assertEqual({c.code: c.status for c in result.data.checks}["ADS_SEES_PRODUCTS"], "WARN")

    def test_a_failed_merchant_request_is_counted_in_the_block(self):
        payloads = search_payloads()

        def answer(method, url, **kw):
            if "dataSources" in url:
                return FakeResponse(429, {"error": {"message": "slow down"}})
            for marker, key in (("programs/shopping-ads", "programs"), ("aggregateProductStatuses", "aggregateProductStatuses")):
                if marker in url:
                    return FakeResponse(payload=payloads[key])
            raise AssertionError(url)

        with mock.patch.object(merchant_client.requests, "request", side_effect=answer):
            result = self.run_tool(self.ads_client())
        self.assertEqual({c.code: c.status for c in result.data.checks}["GMC_FEED_LABEL"], "SKIPPED")
        entry = self.entry()
        self.assertEqual(entry["merchant_api"]["failed"], 1)
        self.assertEqual(entry["gmc_read_errors"], {"feed_labels": "RATE_LIMIT_EXCEEDED"})


if __name__ == "__main__":
    unittest.main()
