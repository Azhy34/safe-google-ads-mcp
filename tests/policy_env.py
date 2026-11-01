"""Imported first by every test: pins the fictional test policy, so the suite never reads a real advertiser's policy."""
import os
from pathlib import Path

os.environ["MCP_POLICY_FILE"] = str(Path(__file__).resolve().parent / "policy_test.json")
