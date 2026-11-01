"""
verify_changes_logged: crediting account changes to the logged calls, dry runs, and a history cut off by the row ceiling.

The cases come from a real campaign build: a loop of link_asset_to_entity calls one second apart
left four calls with "no change in the account" while their changes were credited to the neighbour call, two
dry runs were reported as missing, and a history longer than the ceiling would have been cut silently.
The Google Ads service is faked; the audit journal is a list in memory.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_verify_changes_logged.py" -v
"""
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
import policy_env  # noqa: F401  (pins the fictional test policy before anything reads it)

from shield import cloud_sink, error_shield
from tools import history_tools
from tools.history_tools import _attribute_events, _is_dry_run, _resource_ids, verify_changes_logged

BERLIN = ZoneInfo("Europe/Berlin")
CAMPAIGN = "2000000002"


def link_call(start, asset_id, field_type="SITELINK", campaign=CAMPAIGN, latency_ms=1000.0, dry_run=False):
    request = {"entity_id": campaign, "field_type": field_type, "asset_id": asset_id, "level": "CAMPAIGN"}
    if dry_run:
        request["dry_run"] = True
    return {
        "timestamp": start.isoformat(),
        "operator": "operator1",
        "function": "link_asset_to_entity",
        "action_type": "MUTATION",
        "success": True,
        "latency_ms": latency_ms,
        "args": {"args": [request], "kwargs": {}},
    }


def link_event(time_utc, asset_id, field_type="SITELINK", campaign=CAMPAIGN):
    return SimpleNamespace(
        change_event=SimpleNamespace(
            change_date_time=time_utc.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M:%S.%f"),
            client_type=SimpleNamespace(name="GOOGLE_ADS_API"),
            change_resource_type=SimpleNamespace(name="CAMPAIGN_ASSET"),
            resource_change_operation=SimpleNamespace(name="CREATE"),
            change_resource_name=f"customers/1112223334/campaignAssets/{campaign}~{asset_id}~{field_type}",
            new_resource=SimpleNamespace(),
            old_resource=SimpleNamespace(),
        ),
        campaign=SimpleNamespace(name="ExampleShop Search DE - Flowers"),
    )


class FakeAds:
    """customer.time_zone and change_event; the change_event query honours its LIMIT like Google does."""

    def __init__(self, events):
        self.events = events
        self.ga_service = mock.MagicMock()
        self.ga_service.search.side_effect = self._search
        self.client = mock.MagicMock()
        self.client.get_service.return_value = self.ga_service

    def _search(self, customer_id, query):
        if "customer.time_zone" in query:
            return [SimpleNamespace(customer=SimpleNamespace(time_zone="Europe/Berlin"))]
        limit = int(re.search(r"LIMIT (\d+)", query).group(1))
        if limit > history_tools.CHANGE_EVENT_LIMIT_CEILING:
            # Google: change_event_error.INVALID_LIMIT_CLAUSE, "LIMIT less than or equal to 10k"
            raise AssertionError(f"change_event LIMIT {limit} is above the API ceiling")
        return self.events[:limit]


class AttributionTests(unittest.TestCase):

    def test_calls_a_second_apart_each_get_their_own_event(self):
        """Events sit 0.3 s before the logged start (Google's clock is behind): by time alone they all slide to the previous call."""
        base = datetime(2026, 10, 31, 12, 55, 17, tzinfo=timezone.utc)
        assets = ["433367599716", "433283698168", "433283698165", "433367608020"]
        calls = []
        events = []
        for n, asset in enumerate(assets):
            start = base + timedelta(seconds=n * 1.0)
            calls.append({**link_call(start, asset), "_t": start})
            events.append({"time": start - timedelta(seconds=0.3), "ids": _resource_ids(f"customers/1/campaignAssets/{CAMPAIGN}~{asset}~SITELINK")})
        matched, unmatched = _attribute_events(events, calls)
        self.assertEqual(matched, {0: 1, 1: 1, 2: 1, 3: 1})
        self.assertEqual(unmatched, [])

    def test_without_shared_ids_the_nearest_call_in_time_wins(self):
        start = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
        calls = [{"_t": start, "latency_ms": 2000.0, "args": {"args": [{}], "kwargs": {}}},
                 {"_t": start + timedelta(seconds=30), "latency_ms": 2000.0, "args": {"args": [{}], "kwargs": {}}}]
        events = [{"time": start + timedelta(seconds=31), "ids": set()}]
        self.assertEqual(_attribute_events(events, calls), ({1: 1}, []))

    def test_an_event_far_from_every_call_is_not_explained(self):
        start = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
        calls = [{"_t": start, "latency_ms": 1000.0, "args": {"args": [{}], "kwargs": {}}}]
        events = [{"time": start + timedelta(minutes=5), "ids": set()}]
        self.assertEqual(_attribute_events(events, calls), ({}, [0]))

    def test_the_account_id_in_a_resource_name_is_not_an_id_of_the_change(self):
        self.assertEqual(_resource_ids("customers/1112223334/campaignAssets/2000000002~434974953097~AD_IMAGE"),
                         {"2000000002", "434974953097"})

    def test_a_dry_run_is_found_in_the_request_and_in_kwargs(self):
        self.assertTrue(_is_dry_run({"args": {"args": [{"dry_run": True}], "kwargs": {}}}))
        self.assertTrue(_is_dry_run({"args": {"args": [], "kwargs": {"dry_run": True}}}))
        self.assertFalse(_is_dry_run({"args": {"args": [{"dry_run": False}], "kwargs": {}}}))
        self.assertFalse(_is_dry_run({"args": None}))


class VerifyTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(error_shield, "AUDIT_FILE", Path(tmp.name) / "audit.jsonl"),
                        mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
                        mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"}),
                        mock.patch.object(history_tools, "journal_source", lambda since, source: "local")):
            patcher.start()
            self.addCleanup(patcher.stop)
        # old enough to be past the change_event lag, recent enough for days=2
        self.base = datetime.now(timezone.utc) - timedelta(minutes=40)

    def run_verify(self, calls, events):
        audit = history_tools._with_time(list(calls))
        with mock.patch.object(history_tools, "_read_audit_log", return_value=audit), \
                mock.patch.object(history_tools, "get_ads_client", return_value=FakeAds(events).client):
            result = verify_changes_logged(days=2, source="local", customer_id="1")
        self.assertTrue(result.success, result.error)
        return result.data

    def test_a_loop_of_links_is_fully_matched_and_dry_runs_are_not_reported_missing(self):
        assets = ["433367599716", "433283698168", "433283698165", "433367608020"]
        calls = [link_call(self.base + timedelta(seconds=n), a) for n, a in enumerate(assets)]
        calls.append(link_call(self.base + timedelta(seconds=40), "434628670415", dry_run=True))
        events = [link_event(self.base + timedelta(seconds=n - 0.3), a) for n, a in enumerate(assets)]
        data = self.run_verify(calls, events)
        self.assertEqual(data["verdict"], "ALL_API_CHANGES_LOGGED")
        self.assertEqual(data["matched_to_log"], 4)
        self.assertEqual([c["account_changes"] for c in data["matched_calls"]], [1, 1, 1, 1])
        self.assertEqual(data["logged_calls_without_account_change"], [])
        self.assertEqual(data["dry_run_calls"], 1)
        self.assertFalse(data["history_truncated"])

    def test_a_call_that_really_changed_nothing_is_still_reported(self):
        assets = ["433367599716", "433283698168"]
        calls = [link_call(self.base + timedelta(seconds=n * 10), a) for n, a in enumerate(assets)]
        events = [link_event(self.base - timedelta(seconds=0.3), assets[0])]
        data = self.run_verify(calls, events)
        self.assertEqual([c["args"]["args"][0]["asset_id"] for c in data["logged_calls_without_account_change"]], [assets[1]])

    def test_a_change_no_call_explains_is_reported_as_outside_the_log(self):
        calls = [link_call(self.base, "433367599716")]
        events = [link_event(self.base - timedelta(seconds=0.3), "433367599716"),
                  link_event(self.base + timedelta(minutes=10), "999999999999")]
        data = self.run_verify(calls, events)
        self.assertEqual(data["verdict"], "CHANGES_OUTSIDE_MCP_LOG")
        self.assertEqual(data["not_in_log"], 1)

    def test_a_history_longer_than_the_ceiling_is_reported_not_cut_silently(self):
        assets = ["433367599716", "433283698168", "433283698165", "433367608020"]
        starts = [self.base + timedelta(seconds=n * 10) for n in range(4)]
        calls = [link_call(s, a) for s, a in zip(starts, assets)]
        events = [link_event(s - timedelta(seconds=0.3), a) for s, a in zip(starts, assets)]
        with mock.patch.object(history_tools, "MAX_VERIFY_ROWS", 3):
            data = self.run_verify(calls, events)
        self.assertEqual(data["verdict"], "INCOMPLETE_HISTORY")
        self.assertTrue(data["history_truncated"])
        self.assertEqual(data["history_rows_read"], 3)
        self.assertEqual(data["history_row_limit"], 3)
        self.assertIn("NOT checked", data["coverage_note"])
        # the call after the cut-off is unchecked, never "no change in the account"
        self.assertEqual([c["args"]["args"][0]["asset_id"] for c in data["unchecked_after_truncation"]], [assets[3]])
        self.assertEqual(data["logged_calls_without_account_change"], [])

    def test_the_default_request_stays_within_the_api_limit_ceiling(self):
        """31.10.2026: MAX_VERIFY_ROWS + 1 = 10001 came back as INVALID_LIMIT_CLAUSE from the real API."""
        self.assertLessEqual(history_tools.MAX_VERIFY_ROWS + 1, history_tools.CHANGE_EVENT_LIMIT_CEILING)
        calls = [link_call(self.base, "433367599716")]
        events = [link_event(self.base - timedelta(seconds=0.3), "433367599716")]
        data = self.run_verify(calls, events)
        self.assertEqual(data["verdict"], "ALL_API_CHANGES_LOGGED")

    def test_a_history_exactly_at_the_ceiling_is_complete(self):
        assets = ["433367599716", "433283698168", "433283698165"]
        starts = [self.base + timedelta(seconds=n * 10) for n in range(3)]
        calls = [link_call(s, a) for s, a in zip(starts, assets)]
        events = [link_event(s - timedelta(seconds=0.3), a) for s, a in zip(starts, assets)]
        with mock.patch.object(history_tools, "MAX_VERIFY_ROWS", 3):
            data = self.run_verify(calls, events)
        self.assertEqual(data["verdict"], "ALL_API_CHANGES_LOGGED")
        self.assertFalse(data["history_truncated"])
        self.assertEqual(data["unchecked_after_truncation"], [])


class PreflightTests(unittest.TestCase):

    def check(self, result):
        from tools import preflight_tools
        fake = SimpleNamespace(__wrapped__=lambda days, source: result)
        now = datetime.now(timezone.utc)
        with mock.patch.object(history_tools, "verify_changes_logged", fake):
            return preflight_tools._check_outside_changes(now - timedelta(days=2), now, "local")

    def base_result(self, verdict, **extra):
        return {"verdict": verdict, "account_time_zone": "Europe/Berlin", "source": "local",
                "not_in_log_changes": [], "changes_by_other_clients": [], "coverage_note": None, **extra}

    def test_a_cut_off_history_stops_the_gate_instead_of_reading_all_clear(self):
        check = self.check(self.base_result("INCOMPLETE_HISTORY", coverage_note="Google returned more than 10000 changes"))
        self.assertEqual(check.status, "ATTENTION")
        self.assertIn("10000", check.summary)

    def test_a_complete_history_with_everything_matched_is_ok(self):
        self.assertEqual(self.check(self.base_result("ALL_API_CHANGES_LOGGED")).status, "OK")


if __name__ == "__main__":
    unittest.main()
