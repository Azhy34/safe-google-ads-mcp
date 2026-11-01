# Link, verify, remove

## Link
```python
create_image_asset(source_url="https://<your-site>/img/<file>.jpg", ratio="SQUARE")      # returns the asset_id
link_asset_to_entity(level="AD_GROUP", entity_id="<AD_GROUP_ID>", asset_id="<ASSET_ID>", field_type="AD_IMAGE")
get_assets_associations(level="AD_GROUP", ad_group_id="<AD_GROUP_ID>", field_type="AD_IMAGE")   # read back
```
`level` is `CUSTOMER`, `CAMPAIGN` or `AD_GROUP`; `entity_id` is the account, campaign or ad group ID. Records of
`get_assets_associations` carry `asset_size` ("1200x628 (1.91:1)") so you can count landscape and square pictures.

## Which filters
| You want | Call |
|---|---|
| One ad group | `level="AD_GROUP", ad_group_id=...` |
| One campaign (its own links and its groups') | `campaign_id=...` |
| One type | `field_type="AD_IMAGE"`, `"SITELINK"`, `"CALLOUT"`, `"STRUCTURED_SNIPPET"`, `"PROMOTION"`, `"PRICE"` |
Ids are digits only; anything else is `REQUEST_REJECTED` without a call to Google. `ad_group_id` cannot be combined with
`level="CUSTOMER"` or `"CAMPAIGN"`.

## Remove
```python
unlink_asset_from_entity(level="CAMPAIGN", entity_id="<CAMPAIGN_ID>", asset_id="<ASSET_ID>", field_type="AD_IMAGE")
```
This removes the link, not the asset (it stays in the library). Per Google Ads help, a more specific link takes precedence and
campaign-level images are what an ad group shows when it has none of its own. Before you remove the last campaign-level image,
check which groups would be left without any. `set_ad_group_status` warns when it enables a group with no images of its own.

## Verify
About 15 minutes after the writes: `verify_changes_logged(days=1)` must say `ALL_API_CHANGES_LOGGED`. A change inside Google's
reporting lag is listed as `pending_in_change_history`; look again later.

## Pitfalls
- Never link a picture you did not look at.
- The library is account-wide: use `get_campaign_images(name_contains=...)` to find an old upload instead of uploading again.
- One lever at a time: do not change images and keywords in the same step (`evaluate_agent_process` flags it).
