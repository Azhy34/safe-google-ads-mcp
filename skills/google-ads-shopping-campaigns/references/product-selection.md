# Choosing the products

A Shopping campaign advertises what the feed holds. Spending on the wrong products is the main way to waste a small budget, so
the tool makes the choice explicit.

- `offers=["<offer_id>", ...]`: only these products. Each ID must exist in the catalog; the tool checks them against Google Ads
  (`shopping_product`, IDs spelled as in the feed) and, when readable, Merchant Center. At most 100 offers per campaign.
- `all_products=True`: the whole feed, deliberately. With it, `exclude_custom_label_0=["label"]` leaves out products by
  `g:custom_label_0`.
- `offers` and `all_products` exclude each other, and one of the two is required. `exclude_custom_label_0` works only with `all_products`.

## How the selection is built
The campaign is created with a product tree under the ad group: everything excluded at the root, then your offers included
(or the whole feed minus the excluded labels). The campaign-level product list still shows the whole feed; the ad group's tree is
what is advertised. Read the dry run, not the campaign scope, to see what will run.

## Small themes
A theme with few searches starves a keyword campaign. A Shopping campaign on the products of that theme can still collect clicks
from product searches. Keep one theme per campaign (its own budget and negatives), and do not start more campaigns than you
can review.

## Other countries
The countries come from `shopping.default_geo` of the policy; `create_shopping_campaign` has no geo argument. Give a country that
needs its own budget its own campaign: set the policy value first, create the campaign, then change it back if needed.
