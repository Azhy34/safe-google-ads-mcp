"""
Speed fixes of 12.11.2026 (trace of get_ad_groups: 417 ms of 978 ms was the creation of a new Google client before every call;
trace of preflight_log_check: 13.0 s of 21.9 s was "the oldest entry of the 30-day journal", asked again in every new process).

1. core.client.get_ads_client keeps ONE client per login id (Google's guide: reuse the client, a new one pays a token refresh and a
   new connection on its first request). The auth-failure path drops it, so a revoked token is never kept alive.
2. tools.observability_tools.cloud_journal_start keeps its answer on disk for 24 h, clamped to the 30-day retention, so a
   stale value can never claim that the journal reaches further back than it does.

No network: the Google client class and the journal query are replaced.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_client_and_journal_cache.py" -v
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
try:  # pins the fictional test policy and keeps the suite out of the real journals; absent in a clean checkout, nothing here needs it
    import policy_env  # noqa: F401
except ImportError:
    pass

from core import client as client_module
from tools import observability_tools


class FakeAdsClient:
    """What GoogleAdsClient.load_from_dict returns: a service factory that remembers the interceptors it was asked for."""
    built = 0

    def __init__(self, credentials):
        FakeAdsClient.built += 1
        self.credentials = credentials
        self.service_calls = []

    def get_service(self, name, *args, interceptors=None, **kwargs):
        self.service_calls.append((name, list(interceptors or [])))
        return object()


class ClientCacheCase(unittest.TestCase):
    def setUp(self):
        FakeAdsClient.built = 0
        client_module._CLIENTS.clear()
        patcher = mock.patch.object(client_module.GoogleAdsClient, "load_from_dict",
                                    side_effect=lambda credentials: FakeAdsClient(credentials))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(client_module._CLIENTS.clear)


class TestGoogleClientIsReused(ClientCacheCase):
    def test_the_same_login_gets_the_same_client_and_it_is_built_once(self):
        first = client_module.get_ads_client()
        second = client_module.get_ads_client()
        self.assertIs(first, second)
        self.assertEqual(FakeAdsClient.built, 1)

    def test_another_login_gets_its_own_client(self):
        a = client_module.get_ads_client("1111111111")
        b = client_module.get_ads_client("2222222222")
        self.assertIsNot(a, b)
        self.assertEqual(a.credentials["login_customer_id"], "1111111111")
        self.assertEqual(b.credentials["login_customer_id"], "2222222222")
        self.assertIs(a, client_module.get_ads_client("1111111111"))
        self.assertEqual(FakeAdsClient.built, 2)

    def test_the_call_journal_interceptor_is_attached_once_per_service_not_once_per_get_ads_client(self):
        for _ in range(4):
            client_module.get_ads_client().get_service("GoogleAdsService")
        client = client_module.get_ads_client()
        self.assertEqual(len(client.service_calls), 4)
        for name, interceptors in client.service_calls:
            self.assertEqual(name, "GoogleAdsService")
            self.assertEqual(interceptors.count(client_module._API_CALL_INTERCEPTOR), 1)

    def test_a_dropped_client_is_built_again(self):
        first = client_module.get_ads_client()
        client_module.drop_cached_client()
        second = client_module.get_ads_client()
        self.assertIsNot(first, second)
        self.assertEqual(FakeAdsClient.built, 2)

    def test_threads_asking_at_once_build_one_client(self):
        def slow_build(credentials):
            time.sleep(0.05)  # long enough for every thread to be inside the lock queue
            return FakeAdsClient(credentials)
        results = []
        with mock.patch.object(client_module.GoogleAdsClient, "load_from_dict", side_effect=slow_build):
            threads = [threading.Thread(target=lambda: results.append(client_module.get_ads_client())) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(results), 8)
        self.assertEqual(len({id(r) for r in results}), 1)
        self.assertEqual(FakeAdsClient.built, 1)


class FakeRpcError(Exception):
    """A bare gRPC failure as the interceptor sees it (not a GoogleAdsException)."""
    def __init__(self, code_name, details=""):
        self._code, self._details = code_name, details

    def code(self):
        return SimpleNamespace(name=self._code)

    def details(self):
        return self._details


class FakeOutcome:
    def __init__(self, exc=None):
        self._exc = exc

    def exception(self):
        return self._exc

    def trailing_metadata(self):
        return ()


class TestAuthFailureDropsTheClient(ClientCacheCase):
    def call_with(self, exc):
        client_module.get_ads_client()
        self.assertEqual(len(client_module._CLIENTS), 1)
        details = SimpleNamespace(method="/google.ads.googleads.v24.services.GoogleAdsService/Search")
        with mock.patch.object(client_module.time, "sleep"):
            client_module._API_CALL_INTERCEPTOR.intercept_unary_unary(lambda d, r: FakeOutcome(exc), details, "query")

    def test_unauthenticated_drops_the_cached_client(self):
        self.call_with(FakeRpcError("UNAUTHENTICATED", "Request had invalid authentication credentials"))
        self.assertEqual(client_module._CLIENTS, {})

    def test_a_dead_refresh_token_drops_the_cached_client(self):
        self.call_with(FakeRpcError("UNAVAILABLE", "Getting metadata from plugin failed with error: invalid_grant"))
        self.assertEqual(client_module._CLIENTS, {})

    def test_an_ordinary_failure_keeps_it(self):
        self.call_with(FakeRpcError("INVALID_ARGUMENT", "bad query"))
        self.assertEqual(len(client_module._CLIENTS), 1)

    def test_a_successful_call_keeps_it(self):
        self.call_with(None)
        self.assertEqual(len(client_module._CLIENTS), 1)


class TestJournalStartOnDisk(unittest.TestCase):
    OLDEST = "2026-10-20T10:00:00+00:00"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "journal_start.json"
        for patcher in (mock.patch.object(observability_tools, "JOURNAL_START_FILE", self.file),
                        mock.patch.dict(observability_tools._journal_start_cache, {"at": None, "value": None})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = datetime.now(timezone.utc)
        self.queries = []

        def fake_read(log_filter, max_entries=1, newest_first=True):
            self.queries.append(log_filter)
            return ([{"timestamp": self.next_entry}] if self.next_entry else []), False
        self.next_entry = (self.now - timedelta(days=20)).isoformat()
        patcher = mock.patch.object(observability_tools, "read_cloud_entries", side_effect=fake_read)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_file(self, value, fetched_ago):
        self.file.write_text(json.dumps({"value": value.isoformat(), "fetched_at": (self.now - fetched_ago).isoformat()}))

    def test_a_fresh_file_answers_without_asking_the_journal(self):
        value = self.now - timedelta(days=25)
        self.write_file(value, timedelta(hours=1))
        self.assertEqual(observability_tools.cloud_journal_start(), value)
        self.assertEqual(self.queries, [])

    def test_an_old_file_asks_again_and_rewrites_the_file(self):
        self.write_file(self.now - timedelta(days=25), timedelta(days=2))
        result = observability_tools.cloud_journal_start()
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(result, datetime.fromisoformat(self.next_entry))
        stored = json.loads(self.file.read_text())
        self.assertEqual(stored["value"], self.next_entry)
        self.assertLess(self.now - datetime.fromisoformat(stored["fetched_at"]), timedelta(minutes=1))

    def test_no_file_asks_once_and_writes_it(self):
        result = observability_tools.cloud_journal_start()
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(result, datetime.fromisoformat(self.next_entry))
        self.assertTrue(self.file.exists())

    def test_a_broken_file_is_ignored(self):
        self.file.write_text("{ not json")
        self.assertEqual(observability_tools.cloud_journal_start(), datetime.fromisoformat(self.next_entry))
        self.assertEqual(len(self.queries), 1)

    def test_a_file_without_the_fields_is_ignored(self):
        self.file.write_text(json.dumps({"value": None}))
        observability_tools.cloud_journal_start()
        self.assertEqual(len(self.queries), 1)

    def test_the_memory_cache_still_answers_the_second_call(self):
        observability_tools.cloud_journal_start()
        observability_tools.cloud_journal_start()
        self.assertEqual(len(self.queries), 1)

    def test_a_stale_value_is_clamped_to_the_retention_so_it_never_claims_more_than_the_journal_keeps(self):
        # written a few hours ago when the oldest entry was 31 days old; the journal has dropped it since
        self.write_file(self.now - timedelta(days=31), timedelta(hours=3))
        result = observability_tools.cloud_journal_start()
        self.assertEqual(self.queries, [])
        floor = self.now - timedelta(hours=observability_tools.MAX_HOURS)
        self.assertGreaterEqual(result, floor - timedelta(seconds=5))
        self.assertLess(result - floor, timedelta(minutes=1))

    def test_an_empty_journal_is_not_written_down(self):
        self.next_entry = None
        self.assertIsNone(observability_tools.cloud_journal_start())
        self.assertFalse(self.file.exists(), "an unknown answer must not stick for a day")

    def test_a_file_that_cannot_be_written_never_breaks_the_tool(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("a file where a folder is expected")
        with mock.patch.object(observability_tools, "JOURNAL_START_FILE", blocker / "journal_start.json"):
            self.assertEqual(observability_tools.cloud_journal_start(), datetime.fromisoformat(self.next_entry))


if __name__ == "__main__":
    unittest.main()
