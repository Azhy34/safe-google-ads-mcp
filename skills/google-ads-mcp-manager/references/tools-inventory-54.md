# Complete Catalog of 54 FastMCP Tools (`safe-google-ads-mcp`)

All 54 tools are organized into 6 operational levels matching Google Ads account hierarchy.

---

### Level 0. Session, Audit & Observability (7 tools)
1. `preflight_log_check(acknowledge=False, days=None)` — **Mandatory session gatekeeper**. Verifies MCP integrity, third-party mutations, and SOP compliance before unlocking account mutations.
2. `get_cloud_logs(hours=24, severity="ERROR", ...)` — Fetches structured logs from Google Cloud Logging with filtering by tool and severity.
3. `get_cloud_trace(trace_id="...")` — Analyzes exact sub-millisecond execution breakdown across upstream Google Ads gRPC calls vs local Python execution.
4. `analyze_tool_health(days=7)` — Computes tool error rates, p95 latency percentiles, and anomaly alerts (`NEW_ERROR_CODE`, `ERROR_SPIKE`).
5. `evaluate_agent_process(days=1)` — Evaluates whether the autonomous agent obeyed SOP rules (`READ_FIRST`, `ONE_LEVER`, `VERIFY_AFTER`).
6. `get_change_history(days=7, campaign_id="...")` — Official Google Ads change history (`change_event`) detailing who changed what and exact before/after values.
7. `verify_changes_logged(days=1)` — Reconciles upstream Google Ads change events against the local audit log (`audit.jsonl`).

---

### Level 1. Campaigns, Budgets & Bid Ladders (7 tools)
8. `get_campaigns()` — Lists all campaigns, serving statuses, portfolio bidding strategies, daily budgets, and CPC ceilings.
9. `get_campaign_details(campaign_id="...")` — Detailed campaign card: exact CPC ceiling, daily budget, network settings, and dayparting.
10. `toggle_campaign_status(campaign_id="...", status="PAUSED"/"ENABLED")` — Pauses or enables a campaign.
11. `set_campaign_daily_budget(campaign_id="...", daily_budget_eur=10.0)` — Sets daily budget with bounded validation.
12. `step_bid_threshold(campaign_id="...", delta="+0.05"/"-0.05")` — Steps campaign CPC ceiling strictly by `±0.05 EUR`.
13. `create_search_campaign(...)` — Creates search campaign with clean network settings (Display/Search Partners OFF) and dayparting schedule.
14. `get_campaign_targeting_overview(campaign_id="...")` — Audits geo-targeting, language settings, devices, and active ad schedules.

---

### Level 2. Ad Groups & Keywords (6 tools)
15. `get_ad_groups(campaign_id="...")` — Lists ad groups with serving statuses and CPC configurations.
16. `create_ad_group(campaign_id="...", name="...", ...)` — Creates a new ad group with initial keyword list.
17. `get_ad_group_keywords(ad_group_id="...")` — Retrieves keywords with Quality Score (1–10), predicted CTR, and ad relevance.
18. `add_ad_group_keywords(ad_group_id="...", keywords=[...])` — Adds positive keywords (Exact `[kw]` or Phrase `"kw"`).
19. `set_keyword_status(ad_group_id="...", criterion_id="...", enabled=True/False)` — Pauses or enables an individual keyword.
20. `set_ad_group_cpc_bid(...)` — Sets ad group base bid (only active for `MANUAL_CPC`; returns protective rejection in `TARGET_SPEND`).

---

### Level 3. Responsive Search Ads (RSA) (3 tools)
21. `get_campaign_ads(campaign_id="...")` — Lists RSAs with headline/description arrays and Ad Strength metrics.
22. `deploy_golden_rsa(...)` — Deploys or updates in-place an RSA satisfying the strict 15/4 Golden Matrix with Dynamic Keyword Insertion (`{KeyWord:...}`) pinned to Headline 1.
23. `set_ad_status(ad_group_id="...", ad_id="...", enabled=True/False)` — Enables or pauses a specific RSA in an ad group. **Safeguard:** Refuses to pause the last enabled RSA in an ad group to prevent accidental traffic drop.

---

### Level 4. Assets & Extensions (14 tools)
24. `get_campaign_assets_overview(campaign_id="...")` — Audits asset coverage across all types.
25. `get_campaign_images()` — Gallery of uploaded marketing images (1:1 Square, 1.91:1 Landscape).
26. `get_assets_associations(level="...", field_type="...")` — Inspects linked assets across Customer, Campaign, and Ad Group levels.
27. `get_assets_performance(days=30)` — Performance metrics (impressions, clicks, CTR) for active extensions.
28. `get_assets_diversity(campaign_id="...")` — Audits whether the campaign has all essential extension types.
29. `create_price_asset(...)` — Creates structured price offerings.
30. `sync_price_extension(...)` — Atomically syncs price assets with campaigns.
31. `preview_sitelink_assets(...)` — Validates sitelink landing URLs prior to linking.
32. `create_sitelink_assets(...)` — Creates and links sitelink extensions.
33. `create_callout_assets(...)` — Creates callout extensions (25 characters max each).
34. `create_structured_snippet_asset(...)` — Creates structured snippet extensions with Google-approved headers.
35. `link_asset_to_entity(...)` — Links an asset to a campaign or ad group.
36. `unlink_asset_from_entity(...)` — Unlinks an asset from an entity.

---

### Level 5. Negative Shields, Geo, Conversions & Auditing (17 tools)
37. `audit_campaign_4level_shield(campaign_id="...")` — 4-level quality audit checking bidding, ad group structure, RSA quality, and negative keyword shields.
38. `apply_negative_shield(campaign_id="...", language="EN")` — Deploys baseline negative keyword packs.
39. `add_negative_keywords(campaign_id="...", keywords=[...])` — Adds targeted negative keywords.
40. `remove_negative_keyword(campaign_id="...", keyword_text="...")` — Removes a negative keyword criterion.
41. `sync_shared_negative_list(...)` — Synchronizes account-level shared negative keyword sets without duplicates.
42. `mine_search_terms_and_harvest_negatives(campaign_id="...", days=14)` — Mines non-converting search terms to extract negative keyword recommendations.
43. `auto_fix_collisions_and_dupes(...)` — Resolves keyword cannibalization across ad groups.
44. `get_daily_performance(days=14)` — 14-day daily metrics breakdown (impressions, clicks, spend, CTR, average CPC).
45. `get_impression_share_loss(campaign_id="...")` — Analyzes Impression Share lost to budget vs rank.
46. `search_keyword_ideas(keywords=[...])` — Queries Google Keyword Planner for search volume and CPC bid ranges.
47. `fetch_google_ai_recommendations()` — Retrieves official Google Ads AI optimization recommendations.
48. `audit_geo_physical_locations(days=30)` — Scans `user_location_view` to detect query leakage outside targeted geographic territories.
49. `check_merchant_center_link(merchant_id="...")` — Audits `ProductLink` status and invitations with Google Merchant Center.
50. `check_enhanced_conversions_status()` — Validates Enhanced Conversions tracking settings and Customer Data Terms acceptance.
51. `upload_click_conversion(...)` — Uploads offline click conversions with `gclid`/`gbraid`/`wbraid` and SHA-256 hashed customer parameters.
52. `set_conversion_action_goal(...)` — Configures conversion goals as primary or secondary.
53. `get_demand_gen_currency_floor("EUR")` — Retrieves minimum budget threshold per currency.
54. `get_google_ads_invoices(year="2026")` — Fetches official invoices with net spend and VAT breakdowns.
