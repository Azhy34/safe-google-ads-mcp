---
name: google-ads-conversion-tracking
description: Standard operating procedure for auditing Google Ads Enhanced Conversions, configuring Primary/Secondary bidding goals, and uploading server-side click conversions.
---

# Google Ads Conversion Tracking & Goal Optimization SOP

Deterministic procedure for managing Google Ads conversion actions, auditing Enhanced Conversions compliance under EU DMA, and steering Smart Bidding algorithms.

## Step 1: Audit Conversion Tracking & Enhanced Conversions
Audit current customer conversion configuration, data terms acceptance, and active conversion actions:
```python
check_enhanced_conversions_status()
```
### Checklist:
- `enhanced_conversions_enabled` is `True`.
- `accepted_customer_data_terms` is `True`.
- At least one E-Commerce transaction goal (e.g., `purchase`) is configured as `Primary`.

## Step 2: Configure Primary vs. Secondary Bidding Goals
To focus Smart Bidding (or Maximize Clicks with Smart Signals) on actual high-value purchases while keeping secondary interactions for observation:
```python
# Promote main store purchase to Primary
set_conversion_action_goal(
    conversion_action_id="<PURCHASE_ACTION_ID>",
    primary_for_goal=True,
    status="ENABLED",
    include_in_conversions_metric=True
)

# Demote legacy or micro-conversions to Secondary
set_conversion_action_goal(
    conversion_action_id="<LEGACY_ACTION_ID>",
    primary_for_goal=False,
    status="ENABLED",
    include_in_conversions_metric=False
)
```

## Step 3: Upload Server-Side / Offline Conversions
For server-side order confirmation, webhook events, or Stripe checkout purchases:
```python
upload_click_conversion(
    conversion_action_id="<PURCHASE_ACTION_ID>",
    conversion_date_time="2026-10-26 14:30:00+02:00",
    conversion_value=129.50,
    currency_code="EUR",
    order_id="ORDER_987654",
    gclid="Cj0KCQjwy...",
    email="customer@example.com",
    ad_user_data_consent="GRANTED",
    ad_personalization_consent="GRANTED"
)
```
*Plain text customer email/phone is automatically normalized (Gmail dot removal, E.164 phone formatting) and SHA-256 hashed before transmission to the Google Ads API.*
