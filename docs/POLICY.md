# Business policy

The server code is the same for every advertiser. Everything that belongs to **your** business (your accounts, your landing
hosts, the coupon your marketplace listings must carry, the words you never want in an ad) lives in one JSON file, the
*policy*, not in the code. The shield (`shield/`), the tools (`tools/`) and `core/config.py` ask `core/policy.py` for it.

## Which file is read

The first of these that exists wins. Nothing is merged between files.

1. the file named by the environment variable `MCP_POLICY_FILE` (a missing file is an error, not a silent fallback)
2. `config/policy.local.json`: private to your clone, listed in `.gitignore`
3. `config/policy.json`: the shipped, **neutral** policy (no identity, no rule)

A policy file that cannot be read, has an unknown key or a value of the wrong type stops the server when it starts, with the
name of the key. Keys that start with an underscore (`_comment`) are ignored. Keys you leave out keep their neutral value.

## Closed by default

A tool that needs something the policy does not give it refuses instead of guessing:

| Missing | What happens |
|---|---|
| `site.allowed_hosts` | `create_image_asset`, `create_sitelink_assets` and `preview_sitelink_assets` are refused (`REQUEST_REJECTED`, "set site.allowed_hosts in the policy") |
| `account.merchant_center_id` / `account.feed_label` | `create_shopping_campaign` and `check_shopping_readiness` refuse unless you pass `merchant_id` and `feed_label` |
| `ads.priority_campaign_marker` | `auto_fix_collisions_and_dupes` refuses to run |
| `ads.default_geo` / `ads.default_language` | `create_search_campaign` refuses an empty geo or language list unless you pass them |
| `negatives.packs.<language>` | `apply_negative_shield` refuses a language that has no pack and no `negative_words` |
| `geo.target_country_codes` | `audit_geo_physical_locations` has no target countries and refuses to judge |
| `ads.url_rules` empty | no URL or ad text is checked against a rule |
| `site.forbidden_terms` empty | no word is refused in sitelinks and callouts |

## Keys

```json
{
  "account": {
    "customer_id": "",
    "login_customer_id": "",
    "merchant_center_id": "",
    "feed_label": "",
    "merchant_service_account": "",
    "planner_fallback": { "login_customer_id": "", "customer_id": "" }
  },
  "ads": {
    "url_rules": [],
    "display_paths": ["Shop", "Sale"],
    "default_geo": [],
    "default_language": [],
    "priority_campaign_marker": "",
    "schedule_windows": []
  },
  "negatives": { "packs": {} },
  "site": { "allowed_hosts": [], "forbidden_terms": [], "asset_name_prefix": "Site image" },
  "shopping": { "name_prefix": "", "default_geo": [2276], "default_ad_group_name": "All products" },
  "geo": { "target_country_codes": [], "allowed_target_ids": [] },
  "promotion": { "default_code": "" }
}
```

| Key | Used by | Meaning |
|---|---|---|
| `account.customer_id` | every tool | The account to operate on, 10 digits without dashes. The policy wins; `GOOGLE_ADS_CUSTOMER_ID` is the fallback |
| `account.login_customer_id` | every tool | The manager (MCC) account the calls go through. `GOOGLE_ADS_LOGIN_CUSTOMER_ID` wins; this is the fallback |
| `account.merchant_center_id`, `account.feed_label` | Shopping tools, `check_merchant_center_link` | Defaults for `merchant_id` and `feed_label` |
| `account.merchant_service_account` | `core/merchant_client.py` | The service account the operator's gcloud session impersonates to read Merchant Center (no key file) |
| `account.planner_fallback` | `search_keyword_ideas` | A test manager and customer to fall back to when your developer token cannot call Keyword Planner yet. The fallback answers without search volumes. Leave it empty to get the error as it is |
| `ads.url_rules` | `deploy_golden_rsa`, the level 3 audit | List of URL rules, see below |
| `ads.display_paths` | `deploy_golden_rsa` | Default `path1` and `path2` (two texts, 1 to 15 characters) |
| `ads.default_geo`, `ads.default_language` | `create_search_campaign` | Default geo target and language constant IDs of a new Search campaign |
| `ads.schedule_windows` | `create_search_campaign`, `create_shopping_campaign` | Daily ad schedule: a list of `[start_hour, end_hour]` windows (0 to 24) applied to every day of the week, for example `[[8, 24], [0, 1]]` keeps 01:00 to 08:00 closed. Empty: no schedule, ads run all day |
| `negatives.packs` | `apply_negative_shield` | Negative keyword packs per language code (`de`, `fr`, `es`): `{"de": ["amazon", "kostenlos", ...]}`. The shipped policy carries a small generic pack (marketplaces, freebies, DIY, research words); put the niche words of your own business in your policy. A language without a pack is refused |
| `ads.priority_campaign_marker` | `auto_fix_collisions_and_dupes` | Text in the name of the campaigns whose keywords win; duplicates in all other campaigns are paused |
| `site.allowed_hosts` | sitelinks, `create_image_asset` | The only hosts your site tools may use (exact host names, `https` only) |
| `site.forbidden_terms` | sitelinks, callouts | Words refused in sitelink texts and URLs and in callouts (case-insensitive substrings) |
| `site.asset_name_prefix` | `create_image_asset` | Start of the image asset name. Same content gets the same name (a content hash is appended), so changing the prefix later makes the next upload create a copy |
| `shopping.name_prefix` | `create_shopping_campaign` | A Shopping campaign name must start with this (leave empty for no rule) |
| `shopping.default_geo` | Shopping tools | Default geo targets of a Shopping campaign |
| `shopping.default_ad_group_name` | `create_shopping_campaign` | Default name of the one Shopping ad group |
| `geo.target_country_codes` | `audit_geo_physical_locations` | Countries you deliver to; clicks from any other country count as a budget leak |
| `geo.allowed_target_ids` | `set_campaign_location` | Geo target IDs the tool may switch (empty: any positive ID) |
| `promotion.default_code` | promotion assets | Default coupon code of a promotion asset |

### URL rules

`ads.url_rules` is a list. A rule applies to every URL that contains `host_contains` (case-insensitive):

| Field | Effect |
|---|---|
| `host_contains` | Required. Text the URL must contain for the rule to apply |
| `require_query` | The URL must contain this text, for example `coupon=SAVE10` |
| `forbid_query` | The URL must not contain this text, for example `coupon=` |
| `forbid_text` | List of words that must not appear in any headline or description of an ad that goes to such a URL (case-insensitive) |
| `message` | The error shown when the rule is broken (optional) |

Example: listings on a marketplace must carry a coupon, the own shop must carry none, neither in the link nor in the ad text:

```json
{
  "ads": {
    "url_rules": [
      { "host_contains": "marketplace.example", "require_query": "coupon=SAVE10",
        "message": "Marketplace URLs must include ?coupon=SAVE10" },
      { "host_contains": "my-shop.example", "forbid_query": "coupon=", "forbid_text": ["SAVE10"],
        "message": "my-shop.example sells at direct prices: no coupon in its ads" }
    ]
  },
  "site": { "allowed_hosts": ["my-shop.example", "www.my-shop.example"], "forbidden_terms": ["marketplace", "save10", "coupon"] }
}
```

A broken rule is refused by the shield with `REQUEST_REJECTED` before any call to Google, so it costs nothing and cannot reach
the account.

## Set it up

```bash
cp config/policy.json config/policy.local.json    # then edit policy.local.json
```

Reconnect the MCP server (`/mcp`, Reconnect) after you edit the file: the server reads it when it starts. To see which file is
in use: `python -c "from core import policy; print(policy.loaded_from())"` from the repository root.

## Defaults that stay in code

Not everything is policy. These are safety rails and opinionated defaults of the tool itself; change them in the code if you must,
and add a test:

- Maximize Clicks (`TARGET_SPEND`) with a CPC ceiling, ceiling steps of exactly ±0.05 EUR within 0.10 to 3.00 EUR, daily budget bounds.
- New Search and Shopping campaigns: Google search only (search partners and Display off), always created PAUSED (Shopping) or
  with the status you pass (Search, default PAUSED), the EU political advertising declaration.
- The 15 headlines / 4 descriptions matrix of `deploy_golden_rsa` with Dynamic Keyword Insertion pinned to headline 1.
- The default of `search_keyword_ideas` (German, Germany) and the supported languages of the negative packs (`de`, `fr`, `es`).
- The preflight gate, the one-lever-at-a-time rule and the other process rules of `evaluate_agent_process`.

## Tests

The suite never reads your policy. `tests/policy_env.py` points `MCP_POLICY_FILE` at `tests/policy_test.json`, a fictional
advertiser (`example-shop.test`, a marketplace that needs `coupon=SAVE10`), so the tests are the same on every machine. To
keep your own rules covered, add a test that loads your file with `policy.override(...)` the way `tests/test_policy.py` does.
