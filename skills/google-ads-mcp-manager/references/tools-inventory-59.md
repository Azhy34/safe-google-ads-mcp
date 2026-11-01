# Complete Catalog of 59 FastMCP Tools (`safe-google-ads-mcp`)

All 59 tools are organized into 6 operational levels matching Google Ads account hierarchy.

---

### Level 0. Session, Audit & Observability (7 tools)
1. `preflight_log_check(acknowledge=False, days=None)` — **Mandatory session gatekeeper**. Verifies MCP integrity, third-party mutations, and SOP compliance before unlocking account mutations.
2. `get_cloud_logs(hours=24, severity="ERROR", ...)` — Fetches structured logs from Google Cloud Logging with filtering by tool and severity.
3. `get_cloud_trace(trace_id="...")` — Analyzes exact sub-millisecond execution breakdown across upstream Google Ads gRPC calls vs local Python execution.
4. `analyze_tool_health(days=7)` — Computes tool error rates, p95 latency percentiles, and anomaly alerts (`NEW_ERROR_CODE`, `ERROR_SPIKE`).
5. `evaluate_agent_process(days=1)` — Evaluates whether the autonomous agent obeyed SOP rules (`READ_FIRST`, `ONE_LEVER`, `VERIFY_AFTER`).
6. `get_change_history(days=7, campaign_id="...")` — Official Google Ads change history (`change_event`) detailing who changed what and exact before/after values.
7. `verify_changes_logged(days=1, source="local")` — Reconciles upstream Google Ads change events against the audit journal (`audit.jsonl`, or the cloud journal of every operator with `source="cloud"`). Reads up to 9,999 changes; a longer history ends in `INCOMPLETE_HISTORY`; dry runs are counted apart; the newest changes may be `pending_in_change_history`.

---

### Level 1. Campaigns, Budgets & Bid Ladders (9 tools)
8. `get_campaigns()` — Lists all campaigns, serving statuses, portfolio bidding strategies, daily budgets, and CPC ceilings.
9. `get_campaign_details(campaign_id="...")` — Detailed campaign card: exact CPC ceiling, daily budget, network settings, and dayparting.
10. `toggle_campaign_status(campaign_id="...", status="PAUSED"/"ENABLED")` — Pauses or enables a campaign.
11. `set_campaign_location(campaign_id="...", geo_target_id=2276, enabled=True/False)` — Switches one country on or off inside an existing campaign (adds or removes the location criterion). **Safeguards:** never removes the last targeted country; only the IDs in `geo.allowed_target_ids` when the policy lists any; answers with the targeting Google holds after the change.
12. `create_shopping_campaign(name="...", offers=[...], budget_eur=5.0, cpc_ceiling_eur=0.50, dry_run=False)` — Creates a Standard Shopping campaign on the Merchant Center feed: always PAUSED, Maximize Clicks with a ceiling, Google search only, one ad group with one product ad. Products are a deliberate choice (`offers`, or an explicit `all_products=True`); `dry_run=True` validates with Google and writes nothing; refuses when `check_shopping_readiness` is `BLOCKED`.
13. `set_campaign_daily_budget(campaign_id="...", daily_budget_eur=10.0)` — Sets daily budget with bounded validation.
14. `step_bid_threshold(campaign_id="...", delta="+0.05"/"-0.05")` — Steps campaign CPC ceiling strictly by `±0.05 EUR`.
15. `create_search_campaign(...)` — Creates search campaign with clean network settings (Display/Search Partners OFF) and dayparting schedule.
16. `get_campaign_targeting_overview(campaign_id="...")` — Audits geo-targeting, language settings, devices, and active ad schedules.

---

### Level 2. Ad Groups & Keywords (7 tools)
17. `get_ad_groups(campaign_id="...")` — Lists ad groups with serving statuses and CPC configurations.
18. `create_ad_group(campaign_id="...", name="...", ...)` — Creates a new ad group with initial keyword list.
19. `get_ad_group_keywords(ad_group_id="...")` — Retrieves keywords with Quality Score (1–10), predicted CTR, and ad relevance.
20. `add_ad_group_keywords(ad_group_id="...", keywords=[...])` — Adds positive keywords (Exact `[kw]` or Phrase `"kw"`).
21. `set_keyword_status(ad_group_id="...", criterion_id="...", enabled=True/False)` — Pauses or enables an individual keyword.
22. `set_ad_group_status(ad_group_id="...", enabled=True/False)` — Pauses or enables a whole ad group. **Safeguards:** refuses to pause the last ENABLED group of a campaign; refuses to enable a group without an ENABLED positive keyword and an ENABLED RSA; warns when the group has no images of its own; reads the status back.
23. `set_ad_group_cpc_bid(...)` — Sets ad group base bid (only active for `MANUAL_CPC`; returns protective rejection in `TARGET_SPEND`).

---

### Level 3. Responsive Search Ads (RSA) (3 tools)
24. `get_campaign_ads(campaign_id="...")` — Lists RSAs with headline/description arrays and Ad Strength metrics.
25. `deploy_golden_rsa(...)` — Deploys or updates in-place an RSA satisfying the strict 15/4 Golden Matrix with Dynamic Keyword Insertion (`{KeyWord:...}`) pinned to Headline 1.
26. `set_ad_status(ad_group_id="...", ad_id="...", enabled=True/False)` — Enables or pauses a specific RSA in an ad group. **Safeguard:** Refuses to pause the last enabled RSA in an ad group to prevent accidental traffic drop.

---

### Level 4. Assets & Extensions (14 tools)
27. `get_campaign_assets_overview(campaign_id="...")` — Audits asset coverage across all types.
28. `get_campaign_images(name_contains=None)` — Gallery of the uploaded marketing images (1:1 Square, 1.91:1 Landscape) with preview URLs; `name_contains` filters by asset name; no row cap.
29. `get_assets_associations(level=None, field_type=None, campaign_id=None, ad_group_id=None)` — Inspects linked assets across Customer, Campaign and Ad Group levels. Filters narrow it to one campaign or ad group (without them it covers the whole account and is very large); image records carry `asset_size`. Search ad images are field type `AD_IMAGE`.
30. `get_assets_performance(days=30)` — Performance metrics (impressions, clicks, CTR) for active extensions.
31. `get_assets_diversity(campaign_id="...")` — Audits whether the campaign has all essential extension types.
32. `create_price_asset(...)` — Creates structured price offerings.
33. `sync_price_extension(...)` — Atomically syncs price assets with campaigns.
34. `preview_sitelink_assets(...)` — Validates sitelink landing URLs prior to linking.
35. `create_sitelink_assets(...)` — Creates and links sitelink extensions.
36. `create_callout_assets(...)` — Creates callout extensions (25 characters max each).
37. `create_structured_snippet_asset(...)` — Creates structured snippet extensions with Google-approved headers.
38. `link_asset_to_entity(level, entity_id, asset_id, field_type)` — Links an asset to the account, a campaign or an ad group (one link per call).
39. `unlink_asset_from_entity(level, entity_id, asset_id, field_type)` — Removes the link of an asset (the asset itself stays).
40. `create_image_asset(source_url="https://...", ratio="LANDSCAPE"/"SQUARE", name=None, dry_run=False)` — Uploads an image of the advertiser's own site (hosts from `site.allowed_hosts`) as an image asset: re-checks every redirect, crops to 1.91:1 or 1:1, converts WebP to JPEG, names the asset with a content hash (a repeat reuses it). Does not link it; use `link_asset_to_entity(field_type="AD_IMAGE")`.

---

### Level 5. Shields, Geo, Merchant Center, Conversions & Auditing (19 tools)
41. `audit_campaign_4level_shield(campaign_id="...")` — 4-level quality audit checking bidding, ad group structure, RSA quality, and negative keyword shields.
42. `apply_negative_shield(campaign_id="...", language="EN")` — Deploys baseline negative keyword packs.
43. `add_negative_keywords(campaign_id="...", keywords=[...])` — Adds targeted negative keywords.
44. `remove_negative_keyword(campaign_id="...", keyword_text="...")` — Removes a negative keyword criterion.
45. `sync_shared_negative_list(...)` — Synchronizes account-level shared negative keyword sets without duplicates.
46. `mine_search_terms_and_harvest_negatives(campaign_id="...", days=14)` — Mines non-converting search terms to extract negative keyword recommendations.
47. `auto_fix_collisions_and_dupes()` — Resolves keyword cannibalization: keywords of the priority campaign (name contains `ads.priority_campaign_marker` of the policy) win, duplicates in the other campaigns are paused. Refuses without the marker.
48. `get_daily_performance(days=14)` — 14-day daily metrics breakdown (impressions, clicks, spend, CTR, average CPC).
49. `get_impression_share_loss(campaign_id="...")` — Analyzes Impression Share lost to budget vs rank.
50. `search_keyword_ideas(keywords=[...])` — Queries Google Keyword Planner for search volume and CPC bid ranges.
51. `fetch_google_ai_recommendations()` — Retrieves official Google Ads AI optimization recommendations.
52. `audit_geo_physical_locations(days=30)` — Scans `user_location_view` to detect query leakage outside `geo.target_country_codes` of the policy (refuses without it).
53. `check_merchant_center_link(merchant_id="...")` — Audits `ProductLink` status and invitations with Google Merchant Center.
54. `check_shopping_readiness(merchant_id=None, feed_label=None, geo_target_constants=None, custom_label_0_values=None)` — Reads Google Ads and Merchant Center and answers `READY` / `READY_WITH_WARNINGS` / `BLOCKED` with checks (account link, Shopping ads program per country, feed label, approved products, what Google Ads sees, custom labels, other Shopping campaigns). Read-only.
55. `check_enhanced_conversions_status()` — Validates Enhanced Conversions tracking settings and Customer Data Terms acceptance.
56. `upload_click_conversion(...)` — Uploads offline click conversions with `gclid`/`gbraid`/`wbraid` and SHA-256 hashed customer parameters.
57. `set_conversion_action_goal(...)` — Configures conversion goals as primary or secondary.
58. `get_demand_gen_currency_floor("EUR")` — Retrieves minimum budget threshold per currency.
59. `get_google_ads_invoices(year="2026")` — Fetches official invoices with net spend and VAT breakdowns.
