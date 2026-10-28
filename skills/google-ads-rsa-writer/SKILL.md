---
name: google-ads-rsa-writer
description: Standard operating procedure for writing, validating, and deploying Responsive Search Ads (RSAs) meeting the mandatory 15/4 copy matrix with DKI pinning and in-place mutation.
---

# Google Ads Golden RSA Writer & Deployment Protocol

Guideline for crafting high-CTR, high-relevance Responsive Search Ads (RSAs) following the 15/4 Golden Matrix standard.

## Mandatory Copy Matrix
Every production RSA must satisfy the strict **15/4 formula**:
- **Exactly 15 Headlines:** Maximum 30 characters each.
- **Exactly 4 Descriptions:** Maximum 90 characters each.
- Zero generic stubs or placeholders.

## Structural Rules & Pinning

### 1. Pinned Headline 1 (Dynamic Keyword Insertion)
Headline 1 must be pinned to Position 1 (`HEADLINE_1`) using Dynamic Keyword Insertion (DKI) to guarantee 100% query relevance:
- Format: `{KeyWord:Default Fallback Text}` (<= 30 chars total).
- Example: `{KeyWord:Premium Running Shoes}`

### 2. Headlines 2–4 (Core Unique Selling Propositions)
Specific, concrete value propositions without exclamation mark abuse:
- Free Shipping & Certified Quality
- Eco-Friendly & CE Certified
- Custom Sizing Available

### 3. Headlines 5–8 (Social Proof & Benefits)
- Rated 4.9/5 by 1,000+ Customers
- Easy Installation Guarantee

### 4. Headlines 9–15 (Long-tail Intent & CTAs)
- Order a Sample Today
- Discover New Collection

### 5. Descriptions 1–4 (90 Characters Max)
- Description 1: Primary benefit + shipping terms + warranty.
- Description 2: Social proof + material certifications (CE / Non-toxic).
- Description 3: Customization / sample credit options.
- Description 4: Strong, polite call to action.

## Deployment & In-Place Mutation via Smart Facade

### In-Place Update (Preserving Ad History)
If the ad group already has an active RSA, `deploy_golden_rsa` automatically updates the ad in-place via `AdService.mutate_ads` without creating duplicate ads or fragmenting Quality Score history:
```python
deploy_golden_rsa(
    ad_group_id="<AD_GROUP_ID>",
    final_url="https://example.com/product",
    headlines=[...15 strings...],
    descriptions=[...4 strings...],
    pinned_h1_dki="{KeyWord:Fallback}",
    ad_id="<OPTIONAL_EXISTING_AD_ID>"  # Auto-detected if omitted
)
```

### Managing Serving Status (`set_ad_status`)
To enable or pause individual ads in an ad group:
```python
set_ad_status(
    ad_group_id="<AD_GROUP_ID>",
    ad_id="<AD_ID>",
    enabled=False  # True to enable, False to pause
)
```
*Deterministic Safeguard: The tool strictly refuses to pause the last active RSA in an ad group, preventing accidental traffic loss.*
