# Business Policy (what is yours, and where it lives)

The server code is shared; the advertiser's identity and rules are **data**, in one JSON file read by `core/policy.py`.
Full reference: [`docs/POLICY.md`](../../../docs/POLICY.md). This page is the short version for an agent.

## Which file
`MCP_POLICY_FILE` (if set) -> `config/policy.local.json` (private, git-ignored) -> `config/policy.json` (neutral, shipped).
The first that exists wins; nothing is merged. A broken file stops the server at start, naming the key.

## What it holds
- `account`: customer, manager, Merchant Center, feed label, the service account for reading Merchant Center, the Keyword Planner fallback.
- `ads`: URL rules (a required or forbidden query parameter such as a coupon per host, forbidden words in the ad text of a host),
  default display paths, default geo and language of a new Search campaign, the marker of the priority campaign for the collision fixer.
- `site`: the only hosts the sitelink and image tools may use, words refused in sitelinks and callouts, the image asset name prefix.
- `shopping`: the required campaign name prefix, default countries and ad group name.
- `geo`: the countries you deliver to (the leak audit) and the geo IDs `set_campaign_location` may switch.
- `promotion`: the default coupon code of a promotion asset.

## What an agent must do with it
1. **Never hard-code** an account ID, a coupon, a host or a campaign name in a call or a script. Read it from the policy or ask the owner.
2. A tool that answers "set X in the policy" (`site.allowed_hosts`, `account.merchant_center_id`, `ads.priority_campaign_marker`,
   `geo.target_country_codes`) is **closed by default**, not broken. Ask the owner for the value; do not work around it.
3. A `REQUEST_REJECTED` that quotes a rule message comes from `ads.url_rules` or `site.forbidden_terms`: fix the URL or the text,
   do not try to evade the rule.
4. After the owner edits the policy file, ask them to reconnect the MCP server (`/mcp`, Reconnect): the file is read at start.

## Adding a rule
Add data, not code: a new entry in `ads.url_rules` or a new word in `site.forbidden_terms`. Cover it with a test that uses
`policy.override({...})` (see `tests/test_policy.py`). Only a rule that cannot be expressed as data needs a new key; add the key
to `DEFAULTS` and `_check` in `core/policy.py`, to `config/policy.json` and to `docs/POLICY.md`.
