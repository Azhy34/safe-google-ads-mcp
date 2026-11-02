"""Imported first by every test: pins the fictional test policy and keeps the suite out of the real journals.

- MCP_POLICY_FILE points at tests/policy_test.json, so the suite never reads a real advertiser's policy.
- The audit file is a temporary one and cloud logging is switched off for the whole run: a test that sends a bad request
  through build_request would otherwise write a rejected call into the real logs/audit.jsonl and the online journal,
  where a health check takes it for a real agent hammering the shield.
"""
import atexit
import os
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

os.environ["MCP_POLICY_FILE"] = str(Path(__file__).resolve().parent / "policy_test.json")

_tmp = tempfile.TemporaryDirectory(prefix="mcp-tests-")
atexit.register(_tmp.cleanup)

from shield import cloud_sink, error_shield  # noqa: E402

error_shield.AUDIT_FILE = Path(_tmp.name) / "audit.jsonl"
cloud_sink._state.update({"started": True, "logger": None, "tracer": None})
