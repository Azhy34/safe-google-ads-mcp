"""Business policy: everything that belongs to one advertiser and not to the tool.

The shield (shield/), the tools (tools/) and core/config.py ask this module for the account identity, the URL and
coupon rules, the hosts and words allowed in the site's ads, the Shopping campaign prefix and the target countries,
instead of carrying them in code. The code is the same for everyone; the policy file is yours.

Which file is read (the first that exists wins, nothing is merged between files):
    1. the file named by the environment variable MCP_POLICY_FILE
    2. config/policy.local.json   (private to your clone, ignored by git in the public repository)
    3. config/policy.json         (shipped, neutral: no rules, no identities)

Anything the file does not set is closed by default: no allowed hosts means the site tools refuse to run, no URL rules
means no URL is checked. See docs/POLICY.md for every key.
"""
import copy
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SHIPPED_FILE = CONFIG_DIR / "policy.json"
LOCAL_FILE = CONFIG_DIR / "policy.local.json"

DEFAULTS: Dict[str, Any] = {
    "account": {
        "customer_id": "",
        "login_customer_id": "",
        "merchant_center_id": "",
        "feed_label": "",
        "merchant_service_account": "",
        "planner_fallback": {"login_customer_id": "", "customer_id": ""},
    },
    "ads": {"url_rules": [], "display_paths": ["Shop", "Sale"], "default_geo": [], "default_language": [], "priority_campaign_marker": ""},
    "site": {"allowed_hosts": [], "forbidden_terms": [], "asset_name_prefix": "Site image"},
    "shopping": {"name_prefix": "", "default_geo": [2276], "default_ad_group_name": "All products"},
    "geo": {"target_country_codes": [], "allowed_target_ids": []},
    "promotion": {"default_code": ""},
}
URL_RULE_KEYS = {"host_contains", "require_query", "forbid_query", "forbid_text", "message"}


class PolicyError(ValueError):
    """The policy file cannot be used; raised when the server starts, never in the middle of a call."""


@dataclass(frozen=True)
class UrlRule:
    host_contains: str
    require_query: str = ""
    forbid_query: str = ""
    forbid_text: Tuple[str, ...] = ()
    message: str = ""


_cache: Optional[Dict[str, Any]] = None
_source: Optional[str] = None


def _merge(base: Dict[str, Any], extra: Dict[str, Any], where: str = "") -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in extra.items():
        if key.startswith("_"):  # "_comment" and the like
            continue
        if key not in base:
            raise PolicyError(f"unknown policy key '{where}{key}'")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise PolicyError(f"policy key '{where}{key}' must be an object")
            merged[key] = _merge(base[key], value, f"{where}{key}.")
        else:
            if not isinstance(value, type(base[key])):
                raise PolicyError(f"policy key '{where}{key}' must be {type(base[key]).__name__}")
            merged[key] = copy.deepcopy(value)
    return merged


def _check(policy: Dict[str, Any]) -> None:
    for index, rule in enumerate(policy["ads"]["url_rules"]):
        if not isinstance(rule, dict) or not isinstance(rule.get("host_contains"), str) or not rule["host_contains"]:
            raise PolicyError(f"ads.url_rules[{index}] needs a non-empty 'host_contains'")
        unknown = set(rule) - URL_RULE_KEYS
        if unknown:
            raise PolicyError(f"ads.url_rules[{index}] has unknown keys {sorted(unknown)}")
    for name in ("default_geo", "default_language"):
        if not all(isinstance(item, int) and item > 0 for item in policy["ads"][name]):
            raise PolicyError(f"ads.{name} must be a list of geo/language constant IDs")
    paths = policy["ads"]["display_paths"]
    if len(paths) != 2 or not all(isinstance(item, str) and 0 < len(item) <= 15 for item in paths):
        raise PolicyError("ads.display_paths must be two texts of 1 to 15 characters (the two display URL paths of an RSA)")
    for name in ("allowed_hosts", "forbidden_terms"):
        if not all(isinstance(item, str) for item in policy["site"][name]):
            raise PolicyError(f"site.{name} must be a list of text")
    if not all(isinstance(item, int) for item in policy["shopping"]["default_geo"]):
        raise PolicyError("shopping.default_geo must be a list of geo target IDs")
    if not all(isinstance(item, str) for item in policy["geo"]["target_country_codes"]):
        raise PolicyError("geo.target_country_codes must be a list of 2-letter codes")
    if not all(isinstance(item, int) and item > 0 for item in policy["geo"]["allowed_target_ids"]):
        raise PolicyError("geo.allowed_target_ids must be a list of geo target IDs")


def source_path() -> Optional[Path]:
    """The policy file in use, or None when there is none (everything is then closed by default)."""
    explicit = os.environ.get("MCP_POLICY_FILE", "").strip()
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise PolicyError(f"MCP_POLICY_FILE points to a missing file: {explicit}")
        return path
    for path in (LOCAL_FILE, SHIPPED_FILE):
        if path.is_file():
            return path
    return None


def _load() -> Dict[str, Any]:
    global _cache, _source
    if _cache is None:
        path = source_path()
        policy = copy.deepcopy(DEFAULTS)
        if path is not None:
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PolicyError(f"cannot read policy file {path}: {exc}") from exc
            if not isinstance(raw, dict):
                raise PolicyError(f"policy file {path} must contain a JSON object")
            policy = _merge(policy, raw)
        _check(policy)
        _cache, _source = policy, str(path) if path else None
    return _cache


def reload() -> None:
    """Forget the cached policy; the next call reads the file again (tests, and after editing the file)."""
    global _cache, _source
    _cache, _source = None, None


def loaded_from() -> Optional[str]:
    _load()
    return _source


@contextmanager
def override(values: Dict[str, Any]) -> Iterator[None]:
    """Use this policy (merged over the neutral defaults) inside a with-block; for tests."""
    global _cache, _source
    saved = (_cache, _source)
    policy = _merge(copy.deepcopy(DEFAULTS), values)
    _check(policy)
    _cache, _source = policy, "<override>"
    try:
        yield
    finally:
        _cache, _source = saved


def get(path: str, default: Any = None) -> Any:
    """Value by dotted path, e.g. get('account.merchant_center_id'); the default when the path does not exist."""
    node: Any = _load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return copy.deepcopy(node)


def text(path: str) -> str:
    """A text value, stripped; '' when it is not set."""
    value = get(path, "")
    return value.strip() if isinstance(value, str) else ""


def url_rules() -> List[UrlRule]:
    return [
        UrlRule(
            host_contains=rule["host_contains"].lower(),
            require_query=rule.get("require_query", ""),
            forbid_query=rule.get("forbid_query", ""),
            forbid_text=tuple(rule.get("forbid_text", ())),
            message=rule.get("message", ""),
        )
        for rule in _load()["ads"]["url_rules"]
    ]


def display_paths() -> Tuple[str, str]:
    """The two default display URL paths of an RSA (path1, path2) when the call does not name them."""
    first, second = _load()["ads"]["display_paths"]
    return first, second


def site_hosts() -> Tuple[str, ...]:
    return tuple(host.lower() for host in _load()["site"]["allowed_hosts"])


def site_forbidden_terms() -> Tuple[str, ...]:
    return tuple(term.lower() for term in _load()["site"]["forbidden_terms"])


def require_site_hosts(what: str) -> Tuple[str, ...]:
    """The allowed hosts, or a ValueError that says how to enable the tool (closed by default)."""
    hosts = site_hosts()
    if not hosts:
        raise ValueError(f"{what} needs the advertiser's own site: set site.allowed_hosts in the policy file (docs/POLICY.md)")
    return hosts
