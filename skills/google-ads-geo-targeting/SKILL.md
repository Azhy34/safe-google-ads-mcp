---
name: google-ads-geo-targeting
description: Check and change the countries of a Google Ads Search campaign with the Safe Google Ads FastMCP shield - read the targeting, find budget leaking to non-target countries, switch a country on or off without ever leaving a campaign with no location. Triggers - "geo targeting", "countries of a campaign", "budget leak", "clicks from other countries", "add Austria", "remove a country", "set_campaign_location", "audit_geo_physical_locations".
---

# Geo Targeting

A campaign without any location targets the world, and the default location option ("Presence or interest") also shows ads to
people who only search *for* your country. Keep the countries explicit and check for leaks.
Calls are written `tool(arg=...)`. In Antigravity/Gemini send them as `call_mcp_tool(ServerName="safe-google-ads-mcp", ToolName="<tool>", Arguments={...})`.

## Invariants
- `preflight_log_check()` first; `get_change_history(campaign_id=...)` before a write.
- `set_campaign_location` never removes the **last** targeted country; pause the campaign instead if that is the intent.
- The leak audit needs `geo.target_country_codes` in the policy; without it the tool refuses to judge (see `docs/POLICY.md`).
- When the policy lists `geo.allowed_target_ids`, only those IDs can be switched.

## Task -> tool
| Task | Tool |
|---|---|
| Countries, languages, schedule, devices, networks | `get_campaign_targeting_overview(campaign_id)` |
| Money spent outside the target countries | `audit_geo_physical_locations(days=30)` |
| Country on / off in an existing campaign | `set_campaign_location(campaign_id, geo_target_id, enabled)` |
| Pause the campaign instead | `toggle_campaign_status(campaign_id, enabled=False)` |
| Verify | `get_campaign_targeting_overview(campaign_id)`, then `verify_changes_logged(days=1)` |

## Steps
1. Read the targeting; a clean Search campaign is Google Search only (search partners and Display off) with the countries you deliver to.
2. Audit leaks over 30 days; if clicks come from elsewhere, switch the location option to "Presence: people in or regularly in your targeted locations" in the Google Ads interface.
3. Switch a country after the owner's yes; read the targeting back; verify the journal after ~15 minutes.

## Read next
| File | When |
|---|---|
| [references/leak-audit.md](references/leak-audit.md) | Reading the leak report and acting on it |
| [references/switching-countries.md](references/switching-countries.md) | Adding or removing a country, its limits, own campaign for a country |
