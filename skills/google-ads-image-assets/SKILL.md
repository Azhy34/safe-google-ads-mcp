---
name: google-ads-image-assets
description: Put images of the advertiser's own site into Google Ads Search ad groups with the Safe Google Ads FastMCP shield - dry-run the crop, create the image asset once, link it to the right ad group as AD_IMAGE, read it back with narrow filters, unlink when needed. Triggers - "ad images", "image extension", "image assets", "add pictures to ads", "create_image_asset", "AD_IMAGE", "which images does this ad group show".
---

# Image Assets (own-site pictures, per ad group)

A Search campaign can show images linked to the campaign or, more specifically, to an ad group. The safest set-up is one set of
images per ad group, taken from the page that group sends traffic to.
Calls are written `tool(arg=...)`. In Antigravity/Gemini send them as `call_mcp_tool(ServerName="safe-google-ads-mcp", ToolName="<tool>", Arguments={...})`.

## Invariants
- The source must be an `https` address on a host of `site.allowed_hosts` of the policy; with no host set the tool refuses to run.
- The field type of a Search ad image is **`AD_IMAGE`**. There is no `IMAGE` in the API.
- `create_image_asset` creates the asset only; linking is a separate lever (`link_asset_to_entity`), one link per call.
- Look at every picture before it is linked; ask the owner before writing; change one ad group at a time and read it back.
- `get_assets_associations` without `campaign_id` / `ad_group_id` covers the whole account and is too large: always filter.

## Task -> tool
| Task | Tool |
|---|---|
| What is in the image library? | `get_campaign_images(name_contains="...")` |
| What does this ad group / campaign show? | `get_assets_associations(level="AD_GROUP", ad_group_id="...", field_type="AD_IMAGE")` |
| Check a crop, write nothing | `create_image_asset(source_url, ratio="LANDSCAPE" or "SQUARE", dry_run=True)` |
| Upload | `create_image_asset(source_url, ratio)` |
| Show it in a group | `link_asset_to_entity(level="AD_GROUP", entity_id, asset_id, field_type="AD_IMAGE")` |
| Take it away | `unlink_asset_from_entity(level, entity_id, asset_id, field_type="AD_IMAGE")` |
| Verify | `verify_changes_logged(days=1)` after ~15 minutes |

## Steps
1. `preflight_log_check()`, `get_change_history()`, then read what the group shows now.
2. Dry run each picture in the ratios you need; show the owner the crops.
3. After the owner's yes with the number of pictures: create, then link one by one, reading the group back after each.
4. Aim for at least one `LANDSCAPE` and one `SQUARE` per group.

## Read next
| File | When |
|---|---|
| [references/crop-and-ratios.md](references/crop-and-ratios.md) | Choosing a ratio, minimum sizes, what the crop does, naming and reuse |
| [references/link-verify-remove.md](references/link-verify-remove.md) | Linking, reading back, removing, campaign vs ad group precedence, pitfalls |
