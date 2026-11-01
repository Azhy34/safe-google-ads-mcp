---
name: google-ads-shopping-campaigns
description: Create and review a Standard Shopping campaign on a Merchant Center feed with the Safe Google Ads FastMCP shield - readiness check on both sides, deliberate product choice, dry run, creation always paused, verification. Triggers - "shopping campaign", "product campaign", "advertise the feed", "Merchant Center feed ads", "check shopping readiness", "create_shopping_campaign", "which products to advertise".
---

# Shopping Campaigns (feed-based, paused first)

No keywords, no ad texts: product cards come from the Merchant Center feed, so the **choice of products is the whole decision**.
Calls are written `tool(arg=...)`. In Antigravity/Gemini send them as `call_mcp_tool(ServerName="safe-google-ads-mcp", ToolName="<tool>", Arguments={...})`.

## Invariants
- Always `preflight_log_check()` first and `get_change_history()` before a write (see `google-ads-mcp-manager`).
- A new campaign is **always PAUSED**; enabling needs the owner's explicit yes (`toggle_campaign_status`).
- Products are chosen on purpose: `offers=[...]` or an explicit `all_products=True`, never the whole feed by default.
- Identity and defaults come from the policy (`account.merchant_center_id`, `account.feed_label`, `shopping.*`, see `docs/POLICY.md`).

## Task -> tool
| Task | Tool |
|---|---|
| Is Ads linked to Merchant Center? | `check_merchant_center_link()` |
| Can the feed show anything? (both sides) | `check_shopping_readiness(custom_label_0_values=[...])` |
| Validate a campaign, write nothing | `create_shopping_campaign(name, offers=[...], dry_run=True)` |
| Create it (paused) | `create_shopping_campaign(name, offers=[...])` |
| Protect before the first click | `add_negative_keywords(...)`, `apply_negative_shield(...)` |
| Change the bid ceiling | `step_bid_threshold(campaign_id, "+0.05")` |
| Verify | `get_campaigns()`, then `verify_changes_logged(days=1)` after ~15 minutes |

## Steps
1. Gate and read-first: `preflight_log_check()`, `get_campaigns()` (is there already a Shopping campaign on this Merchant Center?).
2. `check_merchant_center_link()` and `check_shopping_readiness()`: do not go on while the verdict is `BLOCKED`.
3. Choose the offers; dry run; show the owner the answer (budget, ceiling, countries, offers found in the catalog).
4. Create after the owner's yes with the numbers; read the campaign back (PAUSED).
5. Add negatives, enable only on the owner's yes, review after 24 hours, 7 and 14 days.

## Read next
| File | When |
|---|---|
| [references/readiness-checks.md](references/readiness-checks.md) | A check is `FAIL`, `WARN` or `SKIPPED` and you must explain it |
| [references/product-selection.md](references/product-selection.md) | Choosing `offers`, `all_products`, `custom_label_0` |
| [references/after-launch.md](references/after-launch.md) | Enabling, reviewing, pitfalls |
