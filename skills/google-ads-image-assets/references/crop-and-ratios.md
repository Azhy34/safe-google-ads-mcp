# Crop and ratios

| `ratio` | Shape | Minimum size | Use for |
|---|---|---|---|
| `LANDSCAPE` | 1.91:1 | 600x314 | Wide pictures (hero banners, social previews) |
| `SQUARE` | 1:1 | 300x300 | Product photos, portrait photos |

## What the tool does with the file
1. Re-checks the URL (https, no credentials, standard port, a path to a file) against `site.allowed_hosts`; **every redirect is
   checked again**, so an allowed host cannot send the tool elsewhere.
2. Downloads with a time limit and a size cap.
3. A picture within 1 % of the ratio is used as it is; any other is cut from the centre. WebP is converted to JPEG (the API accepts GIF, JPEG and PNG).
4. Checks the minimum size and uploads the bytes, then reads the asset back.

`dry_run=True` stops after step 3 and writes nothing; it reports the final size and format.

## Choosing
- A portrait photo cut to 1.91:1 loses most of the picture: use `SQUARE` for it.
- Ask the owner to look at the crops (`dry_run`) before you write; the centre is not always the interesting part.
- Prefer pictures that show the product the ad group sells. A decorative or generic picture is worse than none.

## Naming and reuse
The asset name is `<site.asset_name_prefix> <file name> <ratio>` plus a hash of the final bytes (or your `name` plus the hash).
Same content, same name: a repeat finds the asset again instead of filling the library with copies. Changing the prefix in the
policy changes the names, so the next upload of the same picture creates a copy. To find what exists:
`get_campaign_images(name_contains="<part of the file name>")`.
