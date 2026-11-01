"""
Read-only connection to the Merchant API: the catalog behind the Shopping campaigns.

Google Ads cannot say why a product does not show, the answer lives in Merchant Center. This module is the one door to it,
as client.py is the one door to Google Ads. It only reads: GET, and POST for the reports:search query. There is no key file:
the owner's gcloud session impersonates the Merchant service account, the token lives an hour, is kept in memory for 50
minutes and is never printed (shield.telemetry masks ya29.* in every log line).

The API is plain HTTP, so the gRPC interceptor of client.py does not see it. Each request is therefore a child span of the
tool call (merchant_api.<label>) and a line in the call's `merchant_api` journal block (shield.telemetry.record_merchant_call).
"""
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from shield import cloud_sink
from shield.telemetry import record_merchant_call
from .config import MERCHANT_API_BASE, MERCHANT_SCOPE, MERCHANT_SERVICE_ACCOUNT

TOKEN_TTL_SECONDS = 50 * 60
MINT_TIMEOUT_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = (3, 30)
SEARCH_PATH_SUFFIX = "/reports:search"  # the one POST that reads

# Markers of a 401 that no new token fixes: the Cloud project or the service account is not known to Merchant Center
NOT_REGISTERED_MARK = "is not registered with the merchant account"
NO_ACCESS_MARK = "does not have access to the accounts"

# Failures that mean "nothing in Merchant Center can be read until a human fixes access": a read tool skips all of its
# Merchant checks and says why, instead of reporting every one of them as broken
ACCESS_FAILURE_CODES = frozenset({
    "GCLOUD_NOT_FOUND", "IMPERSONATION_DENIED", "GCLOUD_REAUTH_NEEDED", "TOKEN_MINT_FAILED",
    "GCP_NOT_REGISTERED", "PERMISSION_DENIED_ACCOUNTS", "AUTH_EXPIRED", "PERMISSION_DENIED",
    "REQUEST_TIMEOUT", "NETWORK_ERROR",
})

_token: Dict[str, Any] = {"value": None, "until": 0.0}
_lock = threading.Lock()


class MerchantApiError(Exception):
    """A Merchant API request that gave no answer. `code` is what the journal and the tool answers show."""

    def __init__(self, code: str, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _mint_token() -> str:
    gcloud = os.getenv("GCLOUD_PATH") or shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not gcloud:
        raise MerchantApiError("GCLOUD_NOT_FOUND", "gcloud is not on PATH (install Google Cloud CLI or set GCLOUD_PATH in .env)")
    command = [gcloud, "auth", "print-access-token",
               f"--impersonate-service-account={MERCHANT_SERVICE_ACCOUNT}", f"--scopes={MERCHANT_SCOPE}"]
    # Under MCP the server's stdin is the JSON-RPC pipe of the client: a child that inherits it can block on it, so the
    # child gets no stdin at all and, on Windows, no console window
    options: Dict[str, Any] = {"capture_output": True, "text": True, "timeout": MINT_TIMEOUT_SECONDS,
                               "stdin": subprocess.DEVNULL, "env": dict(os.environ, CLOUDSDK_CORE_DISABLE_PROMPTS="1")}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.run(command, **options)
    except subprocess.TimeoutExpired:
        raise MerchantApiError("TOKEN_MINT_FAILED", f"gcloud did not answer in {MINT_TIMEOUT_SECONDS}s")
    token = (process.stdout or "").strip()
    if process.returncode != 0 or not token:
        error = (process.stderr or "").strip()
        tail, low = error[-300:], error.lower()
        if "permission_denied" in low or "iam.serviceaccounts.gettoken" in low or "getaccesstoken" in low:
            raise MerchantApiError("IMPERSONATION_DENIED", f"no right to impersonate {MERCHANT_SERVICE_ACCOUNT}: {tail}")
        if "reauth" in low or "invalid_grant" in low or "login" in low:
            raise MerchantApiError("GCLOUD_REAUTH_NEEDED", f"the gcloud session is stale, the owner runs `gcloud auth login`: {tail}")
        raise MerchantApiError("TOKEN_MINT_FAILED", tail or "gcloud returned no token")
    return token


def _get_token(force: bool = False) -> str:
    """
    Token for the Merchant API, cached for 50 minutes; force=True mints a fresh one (after a 401).
    Minting takes 2-3 s (gcloud impersonation) and is the first thing a tool call pays after the server starts, so it is a span
    and a line of the `merchant_api` journal block of its own, not time that looks like our code.
    """
    with _lock:
        if not force and _token["value"] and time.time() < _token["until"]:
            return _token["value"]
        started = time.perf_counter()
        outcome = "OK"
        with cloud_sink.child_span(cloud_sink.MERCHANT_API_SPAN_PREFIX + "access_token") as span:
            try:
                token = _mint_token()
            except MerchantApiError as e:
                outcome = e.code
                if span is not None:
                    span.set_attribute("error.type", outcome)
                raise
            finally:
                record_merchant_call("access_token", (time.perf_counter() - started) * 1000, outcome)
        _token["value"], _token["until"] = token, time.time() + TOKEN_TTL_SECONDS
        return token


def _error_message(response: requests.Response) -> str:
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = response.text
    return (message or f"HTTP {response.status_code}")[:300]


def _classify(status: int, message: str) -> str:
    """Error code for an HTTP failure of the Merchant API."""
    low = message.lower()
    if status == 401 and NOT_REGISTERED_MARK in low:
        return "GCP_NOT_REGISTERED"
    if status == 401 and NO_ACCESS_MARK in low:
        return "PERMISSION_DENIED_ACCOUNTS"
    codes = {401: "AUTH_EXPIRED", 403: "PERMISSION_DENIED", 404: "RESOURCE_NOT_FOUND", 400: "BAD_REQUEST", 429: "RATE_LIMIT_EXCEEDED"}
    return codes.get(status) or ("UPSTREAM_ERROR" if status >= 500 else f"HTTP_{status}")


def _send(method: str, url: str, label: str, token: str, params: Optional[Dict[str, Any]],
          json_body: Optional[Any], timeout) -> requests.Response:
    """One HTTP attempt: a span and a journal line. Only a network failure raises here, an HTTP error is the caller's to read."""
    started = time.perf_counter()
    response: Optional[requests.Response] = None
    outcome = "ERROR"
    with cloud_sink.child_span(cloud_sink.MERCHANT_API_SPAN_PREFIX + label, {"http.request.method": method}) as span:
        try:
            response = requests.request(method, url, headers={"Authorization": f"Bearer {token}"}, params=params,
                                        json=json_body, timeout=timeout)
            outcome = "OK" if response.status_code < 400 else f"HTTP_{response.status_code}"
        except requests.exceptions.Timeout:
            outcome = "REQUEST_TIMEOUT"
        except requests.exceptions.RequestException as e:
            outcome = "NETWORK_ERROR"
            detail = type(e).__name__
        finally:
            record_merchant_call(label, (time.perf_counter() - started) * 1000, outcome)
            if span is not None:
                if response is not None:
                    span.set_attribute("http.response.status_code", response.status_code)
                if outcome != "OK":
                    span.set_attribute("error.type", outcome)
    if response is None:
        raise MerchantApiError(outcome, f"{label}: {detail if outcome == 'NETWORK_ERROR' else 'no answer in time'}")
    return response


def merchant_request(method: str, path: str, label: str, params: Optional[Dict[str, Any]] = None,
                     json_body: Optional[Any] = None) -> Dict[str, Any]:
    """
    One request to the Merchant API (paths like accounts/v1/accounts/<id>/programs/shopping-ads). `label` names it in the
    trace and the journal (programs.get, reports.search): never an ID or a page token. Returns the parsed JSON ({} for an
    empty body). Any HTTP error, timeout or failed token raises MerchantApiError. A 401 that is not a registration or
    access problem is retried once with a fresh token.
    """
    if not (method == "GET" or (method == "POST" and path.endswith(SEARCH_PATH_SUFFIX))):
        raise ValueError(f"merchant_client is read-only: {method} {label} is not allowed")
    url = f"{MERCHANT_API_BASE}/{path.lstrip('/')}"
    refreshed = False
    while True:
        response = _send(method, url, label, _get_token(force=refreshed), params, json_body, REQUEST_TIMEOUT_SECONDS)
        if response.status_code < 400:
            try:
                return response.json() if response.text else {}
            except ValueError:
                raise MerchantApiError("BAD_RESPONSE", f"{label}: the answer is not JSON", response.status_code)
        message = _error_message(response)
        code = _classify(response.status_code, message)
        if code == "AUTH_EXPIRED" and not refreshed:
            refreshed = True
            continue
        raise MerchantApiError(code, message, response.status_code)


def merchant_pages(path: str, key: str, label: str, params: Optional[Dict[str, Any]] = None,
                   page_size: int = 250, max_pages: int = 10) -> List[Dict[str, Any]]:
    """All items of a list endpoint (follows nextPageToken). More pages than max_pages is an error, never a silent cut."""
    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    for _ in range(max_pages):
        query = dict(params or {}, pageSize=page_size)
        if page_token:
            query["pageToken"] = page_token
        page = merchant_request("GET", path, label, params=query)
        items.extend(page.get(key, []) or [])
        page_token = page.get("nextPageToken")
        if not page_token:
            return items
    raise MerchantApiError("TOO_MANY_PAGES", f"{label}: more than {max_pages} pages of {page_size}")
