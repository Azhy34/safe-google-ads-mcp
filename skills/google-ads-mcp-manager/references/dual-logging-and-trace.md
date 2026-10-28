# Dual Logging & W3C Distributed Tracing Architecture

Every Google Ads API interaction is tracked via dual independent logging systems with zero-trust DLP redaction.

---

## 1. Local Audit Log (`logs/audit.jsonl`)

- **Primary Source of Truth:** Append-only JSON Lines ledger stored locally.
- **Zero-Trust DLP Redaction:** `redact_secrets()` automatically masks OAuth tokens, API secrets, developer tokens, and customer PII before writing to disk.
- **Clean stdio Transport:** To prevent breaking FastMCP's JSON-RPC stdio protocol, logs are never written to stdout.
- **Entry Structure:**
  - `timestamp`: UTC ISO-8601 timestamp.
  - `operator`: Operator identity (`agent`, `operator`).
  - `session_id`: Unique identifier of the FastMCP process session.
  - `tool`: Exposed FastMCP tool name.
  - `function`: Internal function name.
  - `action_type`: `MUTATION` or `READ`.
  - `args`: Redacted call arguments.
  - `success`: Execution boolean.
  - `latency_ms`: Duration in milliseconds.
  - `request_id`: Google Ads API official request-id (for Google support escalation).
  - `logging.googleapis.com/trace`: W3C distributed trace ID.

---

## 2. Cloud Logging & Cloud Trace (Optional Google Cloud Integration)

When Google Cloud credentials are configured in `.env`, telemetry is synchronized in real time:
1. **Cloud Logging:**
   - Log name: `google-ads-mcp`.
   - Severity mapping:
     - `DEBUG` — Diagnostic checks (`preflight_log_check`, `get_cloud_logs`).
     - `INFO` — Read-only queries (`READ`).
     - `NOTICE` — Successful account mutations (`MUTATION`).
     - `WARNING` — Shield-rejected input (`REQUEST_REJECTED`).
     - `ERROR` — Upstream Google Ads gRPC errors.
     - `CRITICAL` — Authentication failure (`AUTH_REFRESH_FAILED`).
2. **Cloud Trace (W3C Distributed Tracing):**
   - Root Span: `tools/call <tool_name>` (total execution duration).
   - Child Spans: Direct gRPC service calls to Google Ads API (`GoogleAdsService/Search`, `AdGroupAdService/MutateAdGroupAds`).
   - Detailed span waterfalls can be inspected at any time via `get_cloud_trace(trace_id="...")`.
