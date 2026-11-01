import os
from pathlib import Path
from dotenv import load_dotenv

from . import policy

# This folder is the server's own root; it may also sit inside a larger project whose root .env is shared.
SERVER_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = SERVER_DIR.parent

# Load the shared project .env only when this server really lives inside such a project (a "google-ads" folder),
# never an unrelated .env that happens to sit above a standalone clone.
if SERVER_DIR.name == "google-ads":
    load_dotenv(ROOT_DIR / ".env", override=True)

# Also check local .env if present
LOCAL_ENV = SERVER_DIR / ".env"
if LOCAL_ENV.exists():
    load_dotenv(LOCAL_ENV, override=True)

# The business policy (account identity, URL rules, allowed site hosts, ...) is read from config/policy*.json:
# see core/policy.py and docs/POLICY.md. A broken policy file stops the server here, at start.

# Primary operating account: the policy first, then GOOGLE_ADS_CUSTOMER_ID
CUSTOMER_ID = policy.text("account.customer_id") or os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")

# Manager (MCC) account the API calls are made through: the environment first, then the policy
LOGIN_CUSTOMER_ID = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or policy.text("account.login_customer_id")

# Keyword Planner needs a manager account with an approved developer token; until yours is approved the tools fall back
# to this (test) manager and customer, which answer without search volumes
PLANNER_FALLBACK_LOGIN_ID = policy.text("account.planner_fallback.login_customer_id")
PLANNER_FALLBACK_CUSTOMER_ID = policy.text("account.planner_fallback.customer_id")

# Merchant Center of the shop (the feed behind the Shopping campaigns) and its read-only API (core/merchant_client.py).
# No key file: the operator's gcloud session impersonates this service account (Admin in Merchant Center).
MERCHANT_CENTER_ID = policy.text("account.merchant_center_id")
FEED_LABEL = policy.text("account.feed_label")
MERCHANT_API_BASE = "https://merchantapi.googleapis.com"
MERCHANT_SERVICE_ACCOUNT = policy.text("account.merchant_service_account")
MERCHANT_SCOPE = "https://www.googleapis.com/auth/content"
