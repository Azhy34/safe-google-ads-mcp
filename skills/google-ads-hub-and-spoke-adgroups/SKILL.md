---
name: google-ads-hub-and-spoke-adgroups
description: Standard operating procedure for structuring Google Ads Search campaigns into Hub & Spoke ad groups with dedicated category landing pages, exact/phrase keyword clusters, and pinned RSAs.
---

# Google Ads Hub & Spoke Ad Group Deployment SOP

A deterministic architectural guideline for organizing Google Ads search traffic into tightly focused Ad Groups (Spokes) under a consolidated budget container (Hub).

## Architectural Principle: Hub & Spoke

Instead of fragmenting budget into multiple small campaigns with starved learning phases, consolidate budget into a single high-intent campaign and segment traffic using targeted ad groups:

```text
Campaign (Hub: Budget & Geotargeting & Bidding Strategy)
├── Ad Group 1 (Spoke: Category Alpha) ──> Landing: /category-alpha (RSA 1 + Exact/Phrase Keys)
├── Ad Group 2 (Spoke: Category Beta)  ──> Landing: /category-beta  (RSA 2 + Exact/Phrase Keys)
└── Ad Group 3 (Spoke: General Intent) ──> Landing: /               (RSA 3 + Exact/Phrase Keys)
```

## Step 1: Pre-flight Verification
Before mutating or creating ad groups, verify the pre-flight gate and campaign health:
```python
preflight_log_check()
get_campaign_details(campaign_id="<CAMPAIGN_ID>")
```

## Step 2: Deploy Ad Group with Initial Keyword Cluster
Create the targeted ad group and supply initial positive keywords using exact `[kw]` or phrase `"kw"` syntax:
```python
create_ad_group(
    campaign_id="<CAMPAIGN_ID>",
    name="Category Alpha - Landing Category",
    status="ENABLED",
    keywords=[
        "[primary keyword]",
        ""primary keyword"",
        "[secondary keyword phrase]",
        ""secondary keyword phrase""
    ]
)
```

## Step 3: Add Targeted Keywords (Incremental Expansion)
When expanding search volume or adding long-tail qualifiers:
```python
add_ad_group_keywords(
    ad_group_id="<AD_GROUP_ID>",
    keywords=[
        "[specific feature keyword]",
        ""specific feature keyword""
    ]
)
```

## Step 4: Deploy Dedicated 15/4 Golden RSA
Every spoke must lead to its specific landing page and feature a pinned Headline 1 with Dynamic Keyword Insertion:
```python
deploy_golden_rsa(
    ad_group_id="<AD_GROUP_ID>",
    final_url="https://example.com/category-alpha",
    pinned_h1_dki="{KeyWord:Category Keyword}",
    headlines=[...15 strings <= 30 chars...],
    descriptions=[...4 strings <= 90 chars...],
    path1="Products",
    path2="Category"
)
```

## Step 5: Collision & Quality Shield Audit
Verify that no cross-group keyword duplicates exist and that the 4-Level Quality Shield scores 100/100:
```python
auto_fix_collisions_and_dupes()
audit_campaign_4level_shield(campaign_id="<CAMPAIGN_ID>")
```
