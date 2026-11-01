# Readiness checks

`check_shopping_readiness` reads Google Ads **and** Merchant Center and answers `READY`, `READY_WITH_WARNINGS` or `BLOCKED`.
`create_shopping_campaign` refuses to write while the verdict is `BLOCKED`.

| Code | Side | Reads | A failure means |
|---|---|---|---|
| `ADS_LINK` | Ads | ProductLink between the Ads account and the Merchant Center | Not linked: accept the invitation (it is reported if one is pending) in Google Ads or Merchant Center |
| `GMC_PROGRAM` | Merchant Center | Shopping ads program per country | The program is not `ENABLED` in a country you target, or a requirement is unmet |
| `GMC_FEED_LABEL` | Merchant Center | Data sources and their feed labels | No source carries the label you pass (`feed_label`); the answer lists the labels that exist |
| `GMC_PRODUCTS` | Merchant Center | Product status summary per country | No approved products (counts approved / pending / disapproved are in the detail) |
| `ADS_SEES_PRODUCTS` | Ads | `shopping_product` report | Google Ads does not see the products yet (statuses lag up to 24 hours), or all are `NOT_ELIGIBLE` before any campaign selects them |
| `GMC_CUSTOM_LABEL` | Merchant Center | `custom_label_0` values in the catalog | A value you plan to select or exclude does not exist (case matters) |
| `SHOPPING_OVERLAP` | Ads | Other Shopping campaigns on the same Merchant Center | The same products will compete in several campaigns |

## SKIPPED is not OK
`SKIPPED` means Merchant Center could not be read (no gcloud session, the session is stale, no access). The detail says why.
Merchant Center is read with a service account that the operator's gcloud session impersonates (`account.merchant_service_account`
in the policy): run `gcloud auth login` if the session is stale. Treat `SKIPPED` as "unknown", never as a pass.

## Timing
Product statuses can trail the feed by up to 24 hours; a fresh upload may show `GMC_PRODUCTS` empty for a while. Re-run the check
before you conclude that something is broken.
