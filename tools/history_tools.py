import sys
import enum
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import get_ads_client
from core.config import CUSTOMER_ID
from shield.error_shield import handle_google_ads_errors, AUDIT_FILE
from shield.models_campaign import ChangeEventRecord
from shield import cloud_sink
from tools.observability_tools import CloudReaderError, cloud_error_result, read_cloud_audit_log, cloud_journal_start, journal_source

# change_event only covers the last 30 days and requires a LIMIT (max 10000)
MAX_DAYS = 30
# get_change_history output is read by an agent: 500 rows of 8 fields were ~380 000 characters, more than a chat takes in
DEFAULT_ROWS = 200
MAX_ROWS = 500
# change_event refuses any LIMIT above 10000 (INVALID_LIMIT_CLAUSE).
CHANGE_EVENT_LIMIT_CEILING = 10000
# verify_changes_logged reads the whole period oldest first and requests one row more than it keeps:
# if that row comes back, the period holds more and the newest events were cut off. So the request stays at the ceiling.
MAX_VERIFY_ROWS = CHANGE_EVENT_LIMIT_CEILING - 1
MAX_FIELDS_PER_CHANGE = 6
MAX_VALUE_CHARS = 80
# A change lands in change_event within the logged call's latency; allow a few seconds of clock skew.
# A wider window would wrongly credit unrelated changes made a minute later to an earlier MCP call.
CLOCK_SKEW_SECONDS = 5
# change_event shows API changes only after a delay; unmatched calls younger than this are "pending", not "no change"
CHANGE_EVENT_LAG_MINUTES = 15
# ids (campaign, ad group, asset...) are 6+ digits; shorter numbers are geo targets, counts and the like
_ID_TOKEN = re.compile(r"\d{6,}")

def _render(name: str, value) -> str:
    if isinstance(value, enum.Enum):
        return value.name
    if name.endswith("_micros") and isinstance(value, int):
        return f"{value / 1_000_000:.2f} EUR"
    text = " ".join(str(value).split())
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."

def _field_value(resource, path: str):
    value = resource
    for part in path.split("."):
        value = getattr(value, part)
    return value

def _describe_fields(event, resource_attr: str) -> List[str]:
    paths = list(event.changed_fields.paths)
    new_res = getattr(event.new_resource, resource_attr, None)
    old_res = getattr(event.old_resource, resource_attr, None)
    if event.resource_change_operation.name == "REMOVE":
        # nothing is "new" after a removal (its fields read as empty defaults): show what was removed
        new_res, old_res = old_res, None
    lines = []
    for path in paths[:MAX_FIELDS_PER_CHANGE]:
        name = path.split(".")[-1]
        try:
            new_val = _render(name, _field_value(new_res, path)) if new_res is not None else "?"
        except AttributeError:
            new_val = "?"
        if event.resource_change_operation.name == "UPDATE" and old_res is not None:
            try:
                lines.append(f"{path}: {_render(name, _field_value(old_res, path))} -> {new_val}")
                continue
            except AttributeError:
                pass
        lines.append(f"{path}: {new_val}")
    if len(paths) > MAX_FIELDS_PER_CHANGE:
        lines.append(f"... +{len(paths) - MAX_FIELDS_PER_CHANGE} more fields")
    return lines

def _check_days(days: int) -> None:
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}; change_event only keeps the last 30 days.")

def _search_change_events(ga_service, customer_id: str, days: int, campaign_id: Optional[str], limit: int, order: str):
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    end = date.today().isoformat()
    where = f"change_event.change_date_time >= '{start}' AND change_event.change_date_time <= '{end} 23:59:59'"
    if campaign_id:
        where += f" AND campaign.id = {campaign_id}"

    query = f"""
        SELECT
            change_event.change_date_time,
            change_event.client_type,
            change_event.user_email,
            change_event.change_resource_type,
            change_event.change_resource_name,
            change_event.resource_change_operation,
            change_event.changed_fields,
            change_event.new_resource,
            change_event.old_resource,
            campaign.name
        FROM change_event
        WHERE {where}
        ORDER BY change_event.change_date_time {order}
        LIMIT {limit}
    """
    return ga_service.search(customer_id=customer_id, query=query)

def _keyword(event) -> Optional[str]:
    resource_type = event.change_resource_type.name
    if not resource_type.endswith("CRITERION"):
        return None
    criterion = getattr(event.new_resource, resource_type.lower(), None)
    if criterion is None or not criterion.keyword.text:
        # a removed criterion has no new resource: its keyword is in the old one
        criterion = getattr(event.old_resource, resource_type.lower(), None)
    if criterion is None or not criterion.keyword.text:
        return None
    return criterion.keyword.text + (" (negative)" if criterion.negative else "")

@handle_google_ads_errors
def get_change_history(days: int = 7, campaign_id: Optional[str] = None, limit: int = DEFAULT_ROWS, customer_id: str = CUSTOMER_ID) -> Dict[str, Any]:
    """
    Account change history from Google's change_event: every change made through the UI, the API
    (this MCP, scripts, other agents) or automated rules, with the old and new values of changed fields.
    Newest first; `truncated` says that older changes of the period exist beyond `limit`.
    """
    _check_days(days)
    if not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_ROWS}.")
    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")

    results = []
    # one row more than asked: tells whether the period holds older changes that were cut off
    for row in _search_change_events(ga_service, customer_id, days, campaign_id, limit + 1, "DESC"):
        event = row.change_event
        resource_type = event.change_resource_type.name
        resource_attr = resource_type.lower()
        keyword = _keyword(event)

        results.append(ChangeEventRecord(
            changed_at=event.change_date_time,
            client_type=event.client_type.name,
            user_email=event.user_email or None,
            resource_type=resource_type,
            operation=event.resource_change_operation.name,
            campaign_name=row.campaign.name or None,
            resource_name=event.change_resource_name,
            keyword=keyword,
            changes=_describe_fields(event, resource_attr)
        ))
    truncated = len(results) > limit
    results = results[:limit]
    return {
        "returned": len(results),
        "limit": limit,
        "truncated": truncated,
        "oldest_returned_at": results[-1].changed_at if results else None,
        "events": results,
    }

def _parse_account_time(value: str, tz: ZoneInfo) -> datetime:
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in value else "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(value, fmt).replace(tzinfo=tz).astimezone(timezone.utc)

def _with_time(entries) -> List[Dict[str, Any]]:
    out = []
    for entry in entries:
        try:
            entry["_t"] = datetime.fromisoformat(entry["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(entry)
    return out

def _read_audit_log() -> List[Dict[str, Any]]:
    if not AUDIT_FILE.exists():
        return []
    entries = []
    with cloud_sink.child_span("journal.read_local"):
        text = AUDIT_FILE.read_text(encoding="utf-8")
    for line in text.splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return _with_time(entries)

def _call_ids(entry: Dict[str, Any]) -> set:
    """Ids (campaign, ad group, asset...) a logged call named in its arguments."""
    return set(_ID_TOKEN.findall(json.dumps(entry.get("args"), ensure_ascii=False, default=str)))

def _resource_ids(resource_name: str) -> set:
    """Ids in a change_event resource name; the account id leads every name and tells nothing, so it is dropped."""
    parts = resource_name.split("/", 2)
    tail = parts[2] if len(parts) == 3 and parts[0] == "customers" else resource_name
    return set(_ID_TOKEN.findall(tail))

def _is_dry_run(entry: Dict[str, Any]) -> bool:
    args = entry.get("args")
    args = args if isinstance(args, dict) else {}
    requests = list(args.get("args") or []) + [args.get("kwargs") or {}]
    return any(isinstance(r, dict) and r.get("dry_run") is True for r in requests)

def _attribute_events(events: List[Dict[str, Any]], calls: List[Dict[str, Any]]):
    """
    events: [{"time": UTC datetime, "ids": set}], calls: audit entries (with "_t" = start, "latency_ms").
    Returns ({call index: number of events}, [indexes of events no call explains]).
    A change belongs to a call whose [start, start + latency] holds it, a few seconds of clock skew allowed.
    Among the calls that fit in time, the one that named the same ids in its arguments wins. Time alone misleads:
    Google's clock runs a few tenths of a second behind the local one, and calls a second apart (a loop of
    link_asset_to_entity) overlap within the skew, so an event lands on its neighbour and the call it belongs to
    looks like "no change in the account".
    """
    call_ids = [_call_ids(c) for c in calls]
    matched_counts: Dict[int, int] = {}
    unmatched: List[int] = []
    for n, event in enumerate(events):
        best = None
        for i, entry in enumerate(calls):
            start = entry["_t"]
            end = start + timedelta(milliseconds=entry.get("latency_ms", 0))
            outside = max((start - event["time"]).total_seconds(), (event["time"] - end).total_seconds(), 0.0)
            if outside > CLOCK_SKEW_SECONDS:
                continue
            score = (-len(event["ids"] & call_ids[i]), outside, abs((end - event["time"]).total_seconds()))
            if best is None or score < best[1]:
                best = (i, score)
        if best is None:
            unmatched.append(n)
        else:
            matched_counts[best[0]] = matched_counts.get(best[0], 0) + 1
    return matched_counts, unmatched

SOURCES = ("local", "cloud", "auto")

@handle_google_ads_errors
def verify_changes_logged(days: int = 2, source: str = "local", customer_id: str = CUSTOMER_ID) -> Dict[str, Any]:
    """
    Reconciles Google's change_event with the MCP audit trail: every API change in the account
    should match a logged MUTATION call. Reports changes made outside this MCP, logged calls that left
    no trace in the account, and calls chained in one process (parent_span_id = a script, not MCP).
    source="local" reads this machine's logs/audit.jsonl; source="cloud" reads the online journal of every operator,
    so another operator's MCP changes no longer look like changes outside the MCP; source="auto" takes the online
    journal when it reaches back over the whole period, else this machine's file.
    Changes older than the journal cannot be checked: they are counted in before_log_started, and when that is all
    there is the verdict is NOT_VERIFIABLE_BEFORE_JOURNAL, never ALL_API_CHANGES_LOGGED.
    The period is read oldest first up to MAX_VERIFY_ROWS changes; if Google has more, the verdict is INCOMPLETE_HISTORY
    (history_truncated=true, coverage_note says up to when) and calls after the cut-off are listed as
    unchecked_after_truncation, never as "no change". Dry runs (dry_run=true) are only counted in dry_run_calls.
    A change is credited to the call that named the same ids in its arguments, then to the nearest one in time.
    """
    _check_days(days)
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}.")
    requested_source = source
    try:
        # the same rule as every other reader of the journal (observability_tools.journal_source)
        source = journal_source(datetime.now(timezone.utc) - timedelta(days=days), source)
    except CloudReaderError as e:
        return cloud_error_result(e)
    if source == "cloud":
        # The online journal is read before Google: a missing reader key costs no Ads API call
        try:
            # one extra day covers the account time zone offset of the window start
            audit = _with_time(read_cloud_audit_log(datetime.now(timezone.utc) - timedelta(days=days + 1)))
            log_start = cloud_journal_start()
        except CloudReaderError as e:
            return cloud_error_result(e)
    else:
        audit = _read_audit_log()
        log_start = min((e["_t"] for e in audit), default=None)

    client = get_ads_client()
    ga_service = client.get_service("GoogleAdsService")

    tz_name = list(ga_service.search(customer_id=customer_id, query="SELECT customer.time_zone FROM customer LIMIT 1"))[0].customer.time_zone
    tz = ZoneInfo(tz_name)
    account_calls = [e for e in audit if e.get("action_type") == "MUTATION" and e.get("success")]
    # a dry run only previews: it leaves nothing in the account, so it is neither expected there nor matched to a change
    dry_runs = [e for e in account_calls if _is_dry_run(e)]
    mutations = [e for e in account_calls if not _is_dry_run(e)]
    # MCP requests never share a trace; a shared trace_id means several calls ran in one process (a script)
    trace_counts: Dict[str, int] = {}
    for e in audit:
        trace = e.get("logging.googleapis.com/trace")
        if trace:
            trace_counts[trace] = trace_counts.get(trace, 0) + 1

    # one row more than the ceiling: if it comes back, the period holds newer changes that were cut off (oldest first)
    rows = list(_search_change_events(ga_service, customer_id, days, None, MAX_VERIFY_ROWS + 1, "ASC"))
    truncated = len(rows) > MAX_VERIFY_ROWS
    rows = rows[:MAX_VERIFY_ROWS]
    covered_until = None

    before_log, other_clients, to_match = [], [], []
    api_changes = 0
    for row in rows:
        event = row.change_event
        changed_utc = _parse_account_time(event.change_date_time, tz)
        covered_until = changed_utc
        item = {
            "changed_at": event.change_date_time,
            "client_type": event.client_type.name,
            "resource_type": event.change_resource_type.name,
            "operation": event.resource_change_operation.name,
            "campaign": row.campaign.name or None,
            "keyword": _keyword(event),
        }
        if event.client_type.name != "GOOGLE_ADS_API":
            other_clients.append(item)
            continue
        api_changes += 1
        # Google's clock is a few tenths of a second behind: the first logged call's own change reads as older than the journal
        if log_start is None or changed_utc < log_start - timedelta(seconds=CLOCK_SKEW_SECONDS):
            before_log.append(item)
            continue

        to_match.append((item, {"time": changed_utc, "ids": _resource_ids(event.change_resource_name)}))

    matched_counts, unmatched = _attribute_events([m[1] for m in to_match], mutations)
    not_in_log = [to_match[n][0] for n in unmatched]

    window_start = datetime.combine(date.today() - timedelta(days=days - 1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    lag_cutoff = datetime.now(timezone.utc) - timedelta(minutes=CHANGE_EVENT_LAG_MINUTES)
    # a cut-off history says nothing about calls that came after its last event
    unchecked_cutoff = covered_until - timedelta(seconds=CLOCK_SKEW_SECONDS) if truncated and covered_until else None
    matched_calls, logged_without_change, pending_in_change_history, unchecked_after_truncation = [], [], [], []
    for i, entry in enumerate(mutations):
        if entry["_t"] < window_start:
            continue
        call = {
            "logged_at_utc": entry["timestamp"],
            "operator": entry.get("operator"),
            "function": entry.get("function"),
            "args": entry.get("args"),
            "script_suspected": bool(entry.get("parent_span_id"))
                                or trace_counts.get(entry.get("logging.googleapis.com/trace"), 0) > 1,
        }
        if i in matched_counts:
            matched_calls.append({**call, "account_changes": matched_counts[i]})
        elif unchecked_cutoff is not None and entry["_t"] > unchecked_cutoff:
            unchecked_after_truncation.append(call)
        elif entry["_t"] > lag_cutoff:
            pending_in_change_history.append(call)
        else:
            logged_without_change.append(call)

    matched = sum(matched_counts.values())
    if log_start is None:
        verdict = "NO_AUDIT_LOG"
    elif not_in_log:
        verdict = "CHANGES_OUTSIDE_MCP_LOG"
    elif truncated:
        # the verdict "all logged" would only cover the changes that were read
        verdict = "INCOMPLETE_HISTORY"
    elif api_changes and matched == 0 and before_log:
        # every API change of the period predates the journal: nothing was actually compared
        verdict = "NOT_VERIFIABLE_BEFORE_JOURNAL"
    else:
        verdict = "ALL_API_CHANGES_LOGGED"
    notes = []
    if truncated:
        notes.append(f"Google returned more than {MAX_VERIFY_ROWS} changes for {days} day(s); only the oldest {MAX_VERIFY_ROWS} "
                     f"(up to {covered_until.isoformat() if covered_until else '?'} UTC) were compared. Newer changes were NOT checked: "
                     f"{len(unchecked_after_truncation)} logged call(s) after that point are listed as unchecked_after_truncation. "
                     f"Re-run with a smaller days.")
    if before_log:
        notes.append(f"{len(before_log)} of {api_changes} API changes predate the journal ({source}) and could not be checked; "
                     + ("try source=\"local\" or \"auto\", this machine's file may reach further back." if source == "cloud"
                        else "no older journal exists."))
    coverage_note = " ".join(notes) or None
    return {
        "source": source,
        **({"source_requested": requested_source} if requested_source != source else {}),
        "account_time_zone": tz_name,
        "period_days": days,
        "audit_log_starts_utc": log_start.isoformat() if log_start else None,
        "verdict": verdict,
        "coverage_note": coverage_note,
        "history_truncated": truncated,
        "history_rows_read": len(rows),
        "history_row_limit": MAX_VERIFY_ROWS,
        "api_changes": api_changes,
        "matched_to_log": matched,
        "not_in_log": len(not_in_log),
        "before_log_started": len(before_log),
        # previews (dry_run=True) of the period: they change nothing, so they are counted here and never reported as missing
        "dry_run_calls": sum(1 for e in dry_runs if e["_t"] >= window_start),
        "matched_calls": matched_calls,
        "not_in_log_changes": not_in_log,
        "logged_calls_without_account_change": logged_without_change,
        "unchecked_after_truncation": unchecked_after_truncation,
        # change_event lags behind the API by a few minutes; re-run later to match these
        "pending_in_change_history": pending_in_change_history,
        "changes_by_other_clients": other_clients,
    }
