# 🛡️ Safe Google Ads FastMCP Server
> **Enterprise-grade FastMCP server for the Google Ads API.** Empowers autonomous AI agents (Claude Code, Antigravity, Cursor) to safely inspect, optimize, and manage live search campaigns with **two-layer deterministic execution shields**, **38 purpose-built tools**, and **W3C distributed tracing**.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastMCP](https://img.shields.io/badge/Protocol-Model%20Context%20Protocol-green.svg)](https://modelcontextprotocol.io)
[![Observability: W3C Trace](https://img.shields.io/badge/Tracing-W3C%20Distributed%20Trace-orange.svg)](https://www.w3.org/TR/trace-context/)

---

## ⚡ The Problem: Why Direct MCP Connection is a Hallucination

Most agent demos assume a naive architecture: `LLM ➔ Generic MCP ➔ Production API`.

In high-stakes enterprise domains like advertising, this pattern is dangerous:
1. **Unbounded Financial Blast Radius:** An unconstrained agent can step a bid by 5.00 EUR instead of 0.05 EUR, or mutate 5 campaigns simultaneously, burning monthly budgets in minutes.
2. **Context Window Exhaustion:** Exposing Google's 500+ raw gRPC methods bloats prompt context by 30,000+ tokens, inducing hallucinated arguments and non-deterministic tool picking.
3. **The "AI Black Box" Fallacy:** Over standard stdio transports, client-level OpenTelemetry context is dropped. When a tool call times out or fails, engineers have zero visibility into whether the delay came from local Python logic, DNS, or Google's upstream servers.

> **Google’s official Google Ads MCP server is strictly read-only for this exact reason.** 
> To grant an autonomous agent write capabilities, you must insert an **enforced deterministic boundary**.

---

## 🏗️ High-Level Architecture

```text
┌────────────────────────────────────────────────────────────────────────┐
│             AI Agent Client (Claude Code / Antigravity / Cursor)        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ 1. tools/call (JSON-RPC over stdio)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│             Safe Google Ads FastMCP Gateway Server (server.py)         │
│                                                                        │
│   [ LAYER 1: Two-Layer Pydantic Execution Shield (shield/) ]           │
│   • Input boundary validation: Bounded bid ceilings (0.10 - 3.00 EUR)  │
│   • Fixed stepping increments (±0.05 EUR strictly enforced)             │
│   • Bad input is cut off early: returns REQUEST_REJECTED (0 API cost)  │
│                                                                        │
│   [ PREFLIGHT GATEKEEPER (shield/preflight.py) ]                       │
│   • Ad tools refuse execution until preflight_log_check passes         │
│   • Audits tool health, unlogged manual changes & process anomalies    │
│                                                                        │
│   [ LAYER 2: W3C Distributed Tracing & gRPC Interceptor ]              │
│   • Generates autonomous session_id & W3C trace_id per execution       │
│   • Custom gRPC Client Interceptor captures upstream Google request_id │
│   • Measures exact latency breakdown: Upstream API vs Local Code       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ 2. Mutate / Query Stream (gRPC v24)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│              Google Ads API v24 (Google Cloud Infrastructure)           │
│   CampaignService • AdGroupService • AssetService • CriterionService   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 🔬 Benchmark: Real-Time Latency & Tracing Breakdown

Below is a live trace extracted via `get_cloud_trace` showing complete sub-millisecond execution observability:

```json
{
  "trace_id": "36afa63cbde1b76e0e5dec32bdf193b4",
  "total_ms": 3269.6,
  "google_ads_api_ms": 2871.8,  // 87.8% — Upstream Google Ads gRPC Search Stream
  "own_code_ms": 397.8,         // 12.2% — Local FastMCP & Pydantic shield execution
  "spans": [
    {
      "span_id": "6b1f33fe1551a352",
      "name": "tools/call get_campaigns",
      "labels": {
        "operator": "local_operator",
        "session_id": "20261019T131819Z-9079",
        "request_id": "IjOtKBq4cRoeKoajPvhldA",
        "action_type": "READ"
      }
    },
    {
      "span_id": "cdc19910a2add11c",
      "parent_span_id": "6b1f33fe1551a352",
      "name": "/google.ads.googleads.v24.services.GoogleAdsService/Search",
      "labels": { "rpc.grpc.status_code": "0" }
    }
  ]
}
```

---

## 🧰 38 Custom FastMCP Tools Catalog

The tools mirror Google Ads operational hierarchy, split into granular objects and transactional Smart Facades:

| Operational Level | Key MCP Tools | Responsibilities |
|---|---|---|
| **🚦 Gate & Health** | `preflight_log_check`, `analyze_tool_health` | Mandatory pre-flight lock; anomaly & error spike audit. |
| **📢 Campaigns & Bids** | `get_campaigns`, `get_campaign_details`, `step_bid_threshold`, `set_campaign_daily_budget`, `toggle_campaign_status` | Campaign CPC ceilings stepped strictly by `±0.05 EUR`. |
| **👥 Groups & Keywords** | `get_ad_groups`, `get_ad_group_keywords`, `set_keyword_status`, `auto_fix_collisions_and_dupes` | Quality score audits, cross-campaign collision fixer. |
| **✍️ RSA Ads & Facades** | `get_campaign_ads`, `deploy_golden_rsa` | **Smart Facade:** Enforces 15 headlines & 4 descriptions, atomic rotation (pauses oldest ad atomically). |
| **🧩 Google Assets** | `create_price_asset`, `sync_price_extension`, `link_asset_to_entity`, `get_assets_diversity`, `get_assets_performance` | Price extensions, image assets, sitelinks, and performance labeling. |
| **🧹 Negative Shields** | `apply_negative_shield`, `mine_search_terms_and_harvest_negatives`, `remove_negative_keyword` | E-commerce waste harvesting and multi-country negative keyword sets. |
| **📈 Analytics & GA4** | `get_daily_performance`, `get_impression_share_loss`, `search_keyword_ideas` | Keyword Planner discovery and spend vs GA4 dwell-time metrics. |
| **🔭 Observability** | `get_cloud_logs`, `get_cloud_trace`, `verify_changes_logged`, `evaluate_agent_process` | Cloud Logging inspection, W3C trace breakdown, and process evals. |

---

## 🚀 Quickstart & Installation

### 1. Clone & Install
```bash
git clone https://github.com/Azhy34/safe-google-ads-mcp.git
cd safe-google-ads-mcp
pip install -r requirements.txt
```

### 2. Configure Credentials (Zero-Leak)
Copy the template and provide your Google Ads developer tokens:
```bash
cp .env.example .env
```

### 3. Test in Official MCP Inspector
You can inspect schemas and test tools interactively without spending LLM tokens:
```bash
npx @modelcontextprotocol/inspector --web --config .mcp.json
```

### 4. Connect to Claude Code / Desktop
Add the server entry to your client configuration:
```json
{
  "mcpServers": {
    "safe-google-ads-mcp": {
      "command": "python",
      "args": ["/path/to/safe-google-ads-mcp/server.py"],
      "cwd": "/path/to/safe-google-ads-mcp"
    }
  }
}
```

---

## 📊 Live SDLC Orchestration in Jira

This framework was designed, decomposed, and built autonomously using our open-source Jira Cloud engine ([jira-cloud-automation-shield](https://github.com/Azhy34/jira-cloud-automation-shield)):

* **Public Jira Epic:** `[KAN-30] Safe Google Ads FastMCP Shield & Distributed Tracing Framework`
* **Live Kanban Board:** [Atlassian Cloud Kanban Board](https://mykhail-azhyshchev.atlassian.net/jira/software/projects/KAN/boards)
* **Automated Audit Comments:** Every task transition across `To Do` ➔ `In Progress` ➔ `Done` is stamped with correlation `trace_id`.

---

## 📄 License
MIT License. Created by [Mykhail Azhyshchev](https://azhyshchev.de).
