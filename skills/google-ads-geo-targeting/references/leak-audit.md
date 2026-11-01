# Leak audit

`audit_geo_physical_locations(days=30)` reads `user_location_view` (the physical location of the people who clicked), groups it by
country and compares it with `geo.target_country_codes` of the policy. Windows: up to 7, 14 or 30 days.

## The report
- Totals: cost and clicks inside the target countries and outside them, and the share that leaked.
- Per country: status `TARGETED` or `LEAK`, impressions, clicks, cost, conversions.
- Per campaign: which campaigns leak, how much, and to which countries.
- Findings in words, with the recommendation when the leak is real.

## Acting on a leak
1. Check the campaign's location option first. With "Presence or interest" Google also serves people who merely *search for* your
   country. Set it to "Presence: people in or regularly in your targeted locations" in the Google Ads interface (the API tools
   do not change this setting).
2. Check the language: a missing language criterion lets in searchers from anywhere.
3. Add negative keywords for place names you do not serve if clicks come from searches for them.
4. Re-run the audit after at least a week; statistics of earlier days do not change.

## When it refuses
`TARGET_COUNTRIES_NOT_SET`: the policy has no `geo.target_country_codes`, and without the countries you deliver to every country
would count as a leak. Ask the owner for the list and put it in the policy (`docs/POLICY.md`), then reconnect the server.
Countries the tool does not know by ID show up as `??` with the criterion ID; they count as leaks until named.
