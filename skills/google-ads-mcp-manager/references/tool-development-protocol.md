# Missing Tool Protocol: 5 Steps to Extend Safe Google Ads FastMCP

This protocol governs how autonomous agents and developers safely add new Google Ads API capabilities to the FastMCP server without introducing unconstrained blast radius.

> ⛔ **ZERO SCRATCH SCRIPTS MANDATE:**  
> Direct Google Ads API interaction outside the FastMCP server process is physically blocked in `core/client.py` via `RuntimeError: DIRECT_GOOGLE_ADS_API_BLOCKED`.  
> Never write ad-hoc scripts (`scratch_*.py`), inline Python commands, or unmonitored API calls.

---

## The 5-Step Extension Lifecycle

### Step 1: Operator Notification
Before implementing any new tool, notify the human operator:
> "FastMCP server lacks a dedicated tool for `<action>`. Initiating new tool creation via project protocol."

### Step 2: Request Model & Business Logic
1. **Pydantic V2 Request Model (`shield/models_*.py`):**
   - Create a typed request model with strict validation via `Field(...)` and `@field_validator`.
   - Constrain all entity IDs to numeric regex: `pattern=r"^\d+$"`.
2. **Business Function with Error Shield (`tools/*.py`):**
   - Wrap the function with `@handle_google_ads_errors` (captures Google `request_id`, tracks execution time, handles gRPC errors).
   - Implement deterministic safeguards (e.g., refusing to pause the last active RSA in an ad group, enforcing bid increment bounds).
3. **Register in Audit Shield:**
   - If the function mutates state (`action_type="MUTATION"`), add its internal name to `MUTATION_FUNCTIONS` in `shield/error_shield.py`.
   - Add the function to the appropriate lever group in `LEVER_GROUPS` (`bid`, `budget`, `ads`, `negatives`, `assets`) in `tools/eval_tools.py` so the watchdog evaluator (`evaluate_agent_process`) enforces the `ONE_LEVER` constraint.

### Step 3: FastMCP Registration
1. **Expose with `@mcp_tool()` in `server.py`:**
   - Import the request model and business function.
   - Decorate with `@mcp_tool()` and write a comprehensive docstring (FastMCP automatically compiles this into the LLM tool definition).
   - Use `build_request("function_name", RequestModelClass, ...)` to reject malformed input early as `REQUEST_REJECTED` (0 Google Ads API cost).
2. **Compile Check:**
   - Verify Python syntax: `python -m py_compile server.py`.

### Step 4: Unit Testing
Add a deterministic unit test in `tests/` verifying:
- Request model validation (valid input passes, invalid input throws validation error).
- Business function execution with mocked Google Ads client.
- FastMCP tool registration count updated.

### Step 5: Verification & Audit Reconciliation
1. Call the tool via FastMCP (`call_mcp_tool`).
2. Verify latency and gRPC span breakdowns via `get_cloud_trace`.
3. Verify that the mutation appears in `verify_changes_logged(days=1)`.
