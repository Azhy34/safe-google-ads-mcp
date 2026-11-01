# Switching countries

```python
set_campaign_location(campaign_id="<CAMPAIGN_ID>", geo_target_id=2276, enabled=False)   # 2276 = Germany, 2040 = Austria
```

## What it does
- `enabled=True` adds the country's location criterion if it is missing; `enabled=False` removes it. Google documents no "pause"
  for a location criterion, so off means removed, and on means added again. The campaign, ads, keywords and history stay.
- The answer is what Google holds after the change, not what was asked ("Targeted now: ...").

## Safeguards
- **Never the last targeted country**: a campaign without any location targets everywhere. Pause the campaign
  (`toggle_campaign_status`) if that is what you mean.
- Only the geo IDs in `geo.allowed_target_ids` of the policy, when the policy lists any; otherwise any positive geo target ID.
- The campaign must exist; a bad ID is `REQUEST_REJECTED` before any call to Google.

## Own campaign for a country
A country with its own budget, schedule, landing pages or ceiling is better as its own campaign than as a switch inside a shared one:
the budgets stop competing and the ceiling can differ. To split it off: create the campaign for that country (`create_search_campaign`
with `geo_target_constants=[<id>]`), then switch the country off in the old campaign. Do both in the owner's presence and verify each.

## After the change
- `get_campaign_targeting_overview(campaign_id)` shows the new country list.
- `verify_changes_logged(days=1)` after ~15 minutes.
- Judge the effect over at least a week: clicks from the removed country stop at once, earlier days do not change.
- Language targeting is separate (`language_constants`, e.g. 1001 German): a missing language is not a missing country.
- A Shopping campaign takes its countries from `shopping.default_geo` of the policy when it is created.
