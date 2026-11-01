# After the campaign exists

## Before enabling
1. Negatives first: a Shopping campaign matches searches you did not choose (`add_negative_keywords`, `apply_negative_shield`).
2. `get_campaigns()`: status `PAUSED`, daily budget and CPC ceiling as agreed.
3. The owner says yes with the numbers, then `toggle_campaign_status(campaign_id, enabled=True)`.

## Review schedule
| When | Look at | Do not |
|---|---|---|
| After 24 hours | Impressions per product, `check_shopping_readiness` again (statuses catch up) | Judge clicks |
| After 7 days | Clicks, spend, `mine_search_terms_and_harvest_negatives(campaign_id, days=7)` | Change two levers at once |
| After 14 days | Conversions in your analytics, cost per product, which offers earn their place | Raise the ceiling before the data supports it |

Bid ceiling: only `step_bid_threshold(campaign_id, "+0.05")` or `"-0.05"`, one step at a time, with the owner's yes.

## Pitfalls
- Several Shopping campaigns on one Merchant Center compete for the same products (`SHOPPING_OVERLAP`).
- A new campaign lists the whole feed as `NOT_ELIGIBLE` until it selects products; that says nothing about the products.
- The language is the feed's. A language criterion on a Shopping campaign is refused by Google.
- Google refuses a repeated temporary ID across a request; the tool numbers its operations itself, so never hand-build mutations.
- Verify every write: `verify_changes_logged(days=1)` must say `ALL_API_CHANGES_LOGGED`.
