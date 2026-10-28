# Watchdog & Autonomous Process Evaluations (SOP)

The FastMCP server includes an embedded deterministic evaluator (`evaluate_agent_process`) that audits whether the agent obeyed the Standard Operating Procedure (SOP) across the entire session.

---

## 4 Deterministic SOP Rules

| Rule | SOP Step | What the Evaluator Checks | Failure Penalty |
| :--- | :---: | :--- | :--- |
| **`READ_FIRST`** | Step 2 | Before mutating an entity, `get_change_history` was called to avoid overwriting uncommitted changes. | Session verdict `FAIL`. |
| **`ONE_LEVER`** | Step 6 | Mutations executed within 10 minutes belong strictly to a **single lever group** (`LEVER_GROUPS`). Bids and budgets cannot be modified simultaneously. | Flagged as `ATTENTION` in next session preflight. |
| **`AUTO_FIX_ALONE`** | Step 6 | `auto_fix_collisions_and_dupes` runs isolated, never in the middle of manual mutations. | Session verdict `FAIL`. |
| **`VERIFY_AFTER`** | Step 7 | Within 15 minutes to 24 hours after mutations, `verify_changes_logged` was executed. | Marked `PENDING` for 15 mins, then `FAIL` if skipped. |

---

## Lever Groups (`LEVER_GROUPS`)

```python
LEVER_GROUPS = {
    "bid": ("step_campaign_cpc_ceiling", "update_ad_group_bid"),
    "budget": ("update_campaign_budget",),
    "status": ("update_campaign_status", "update_keyword_status"),
    "negatives": ("apply_negative_shield", "add_negative_keywords", "remove_negative_keyword"),
    "ads": ("deploy_golden_rsa", "update_ad_status"),
    "assets": ("link_asset_to_entity", "unlink_asset_from_entity"),
}
```
*Tools not listed count as their own independent lever. Mixing different lever groups within a single session batch is flagged by the watchdog.*
