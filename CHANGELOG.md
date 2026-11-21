# Changelog

All notable changes to `safe-google-ads-mcp`. Dates are ISO. Tool names are the MCP tool names.

## 1.2.0 - 2026-11-21

**63 tools (+4): Promotion assets, campaign start/end dates, ad group audience targeting, and Shopping product tree selection.**

### Added: tools
- `create_promotion_asset(name, promotion_target, final_url, ...)`: Creates a `PROMOTION` extension asset (percent or money off, optional coupon code or minimum order value, occasion enum, redemption and serving ISO dates), enforces `ads.url_rules` from the business policy, reuses identical existing assets, verifies stored fields via read-back, and optionally links to a campaign or ad group (`dry_run=True` preview supported).
- `set_campaign_dates(campaign_id, end_date, start_date, clear_end_date, dry_run)`: Sets or clears campaign start/end dates via an explicit `update_mask` (`YYYY-MM-DD 00:00:00` / `23:59:59`, `2037-12-30` sentinel to clear) and confirms stored dates via read-back.
- `set_ad_group_audience_targeting(ad_group_id, user_list_ids, targeting_mode, bid_modifier)`: Configures `TARGETING` (strict retargeting) or `OBSERVATION` (with optional bid modifier) audience settings on Search or Shopping ad groups while preserving demographic target restrictions and deduplicating existing user lists.
- `set_shopping_product_selection(ad_group_id, offers, cpc_ceiling_eur, dry_run)`: Atomically updates the `LISTING_GROUP` product partition tree inside an existing Shopping ad group to include only the chosen eligible offers (`validate_only` when `dry_run=True`), excluding all other feed items.

### Fixed & Tested
- `set_conversion_action_goal`: fixed enum resolution via `ConversionActionStatusEnum.ConversionActionStatus`.
- Expanded offline test suite covering all 4 new tools, Pydantic shields, read-back verification, and telemetry/audit logging.

## 1.1.1 - 2026-11-02

**No niche business logic left in the code.** What was still specific to one wallpaper shop moved into the policy or became neutral.

### Changed
- `ads.schedule_windows` (policy): the daily ad schedule of `create_search_campaign` and `create_shopping_campaign` is no longer a
  hard-coded 08:00 to 01:00; the shipped policy has none (ads run all day). The dry run of `create_shopping_campaign` prints the
  schedule it will apply.
- `negatives.packs` (policy): the negative keyword packs of `apply_negative_shield` are data. The shipped policy has a small generic
  pack per language (marketplaces, freebies, DIY, research words); the niche words that were in the code (home-improvement stores,
  wall-decor terms) are gone from the repository. A language without a pack is refused with the key to set.
- `check_shopping_readiness`: the Merchant Center country of a geo target is looked up in a wider table (DE, AT, CH, FR, IT, ES, NL,
  BE, PL, GB, US, CA, AU) instead of only DE and AT.
- The test suite no longer writes into the real `logs/audit.jsonl` and the online journal (`tests/policy_env.py` redirects both): a test that
  sent a bad request through the shield used to leave a rejected call there, which a health check reads as an agent hammering the shield.
- Tool descriptions, error texts and tests use neutral examples (`SKU-100-L`, `clearance`, shoe-store fixtures) instead of product codes
  and wording of one shop.

## 1.1.0 - 2026-11-01

**59 tools (+5), the business rules moved out of the code into a policy file, 3 new skills.**
The server code is now identical for every advertiser: identities, URL rules and allowed hosts live in `config/policy*.json`
(see [docs/POLICY.md](docs/POLICY.md)). With the shipped neutral policy nothing is advertiser-specific and the tools that
need an identity or an allowed host refuse to run until you set it (closed by default).

### Added: tools
- `set_ad_group_status(ad_group_id, enabled)` pauses or enables a whole ad group. It refuses to pause the last ENABLED group of
  a campaign, refuses to enable a group without an ENABLED positive keyword and an ENABLED Responsive Search Ad, warns when
  the group has no images of its own, names the update mask explicitly and answers with the status Google holds after the change.
- `set_campaign_location(campaign_id, geo_target_id, enabled)` switches one country on or off inside an existing campaign
  (the criterion is added or removed). It never removes the last targeted country and, when the policy lists
  `geo.allowed_target_ids`, accepts only those.
- `create_image_asset(source_url, ratio, name, dry_run)` downloads an image from the advertiser's own site (hosts from the
  policy, every redirect re-checked, size capped), cuts it to 1.91:1 or 1:1, converts WebP to JPEG, names the asset with a
  content hash (a repeat reuses the asset) and uploads it. It does not link it: use `link_asset_to_entity`.
- `create_shopping_campaign(name, offers, ...)` creates a Standard Shopping campaign on the Merchant Center feed: always PAUSED,
  Maximize Clicks with a CPC ceiling, Google search only, one ad group with one product ad. Products are always a choice
  (`offers`, checked against the catalog, or an explicit `all_products=True`). `dry_run=True` validates everything with Google
  (`validate_only`) and writes nothing. Runs `check_shopping_readiness` first and refuses on `BLOCKED`.
- `check_shopping_readiness(merchant_id, feed_label, ...)` reads both sides (Google Ads and Merchant Center) and answers
  `READY` / `READY_WITH_WARNINGS` / `BLOCKED`: account link, Shopping ads program per country, feed label, approved products,
  what Google Ads itself sees, `custom_label_0` values, other Shopping campaigns on the same Merchant Center.

### Added: platform
- `core/policy.py` and `config/policy.json`: the business policy layer. Resolution order: `MCP_POLICY_FILE`, then
  `config/policy.local.json` (private, git-ignored), then the shipped neutral `config/policy.json`. A broken policy file stops
  the server at start with a clear message.
- `core/merchant_client.py`: a read-only Merchant API client with no key file (it impersonates a service account through the
  operator's gcloud session). Its requests are traced as `merchant_api.*` spans and listed in the journal entry next to the
  Google Ads calls; `get_cloud_trace` reports `merchant_api_calls` and `merchant_api_ms`.
- Read tools can add fields to their own journal entry (a verdict, why a part was skipped), because reads do not log results.
- Skills: `google-ads-shopping-campaigns`, `google-ads-image-assets`, `google-ads-geo-targeting`; tools catalog updated to 59.
- Tests: 237 offline tests. Run them with `python -m unittest discover -s tests` from the repository root. The suite pins a
  fictional advertiser (`tests/policy_test.json`), so it never reads a real policy.

### Changed
- `get_assets_associations(level, field_type, campaign_id, ad_group_id)`: filters narrow the answer to one campaign or one ad
  group (without them it covers the whole account, about 70,000 characters on a real account, more than one tool answer can
  hold); every id is digits only; image records carry `asset_size` ("1200x628 (1.91:1)").
- `get_campaign_images(name_contains)`: filter by asset name; no row cap any more (a `LIMIT 100` used to hide every image past
  the first hundred).
- `link_asset_to_entity` / `unlink_asset_from_entity`: the search ad image field type is `AD_IMAGE` (there is no `IMAGE` in the API).
- `verify_changes_logged`: reads up to 9,999 changes per period (`change_event` refuses a `LIMIT` above 10,000); a longer
  history ends in the verdict `INCOMPLETE_HISTORY` with `unchecked_after_truncation` instead of a silent cut; dry runs are
  counted apart; every change is credited to the call that named the same ids, so a loop of calls one second apart no longer
  shifts credit to the neighbouring call; changes still inside Google's reporting lag are `pending_in_change_history`.
- `preflight_log_check`: an `INCOMPLETE_HISTORY` result is `ATTENTION`, not a pass.
- `evaluate_agent_process`: the lever groups know ad group status, image assets and location targeting.
- `set_conversion_action_goal`: the update mask names its fields explicitly. Before, `primary_for_goal=False` went to Google as
  an update of nothing and came back "successful".
- Tool comments, messages and findings are English only.

### Changed: defaults that were advertiser-specific (breaking for callers that relied on them)
- `campaign_id` has no default any more in `get_campaign_details`, `get_campaign_targeting_overview`, `get_campaign_ads`,
  `mine_search_terms_and_harvest_negatives` and `audit_campaign_4level_shield`. It used to default to one particular
  campaign. Pass it.
- `deploy_golden_rsa`: `path1` / `path2` default to `ads.display_paths` of the policy (neutral: `Shop` / `Sale`).
- `create_search_campaign`: `geo_target_constants` / `language_constants` default to `ads.default_geo` / `ads.default_language`
  and must end up non-empty.
- `create_shopping_campaign`, `check_shopping_readiness`, `check_merchant_center_link`: `merchant_id`, `feed_label` and the ad
  group name default to the policy (`account.*`, `shopping.default_ad_group_name`).
- `auto_fix_collisions_and_dupes`: the priority campaign is the one whose name contains `ads.priority_campaign_marker`
  (it used to be a hard-coded campaign name); the tool refuses to run without it.
- The coupon rule is data now: a host can require a query parameter (a coupon) and another host can forbid it in the URL and in
  every headline and description (`ads.url_rules`). `deploy_golden_rsa` and the level 3 audit of
  `audit_campaign_4level_shield` read the same rules.
- Sitelink and callout checks, `create_image_asset` and the Shopping campaign name prefix read `site.allowed_hosts`,
  `site.forbidden_terms` and `shopping.name_prefix`.
- `audit_geo_physical_locations` takes the target countries from `geo.target_country_codes`.

### Removed from the code
- Every hard-coded account, Merchant Center and campaign identifier, the advertiser's coupon rule, its allowed hosts and
  forbidden words, and the example names of one particular shop.

## 1.0.0 (state of 2026-10-28)

First public release: 54 tools, two-layer Pydantic shield, preflight gate, W3C tracing, dual journal (local `audit.jsonl` and
Cloud Logging), seven agent skills.
