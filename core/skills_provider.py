"""Skills over MCP provider (SEP-2640 compatible structure over MCP Resources).

Discovers Agent Skills folders (`skills/<name>/SKILL.md` + `references/*.md`) next to the server
and exposes them automatically through the MCP server:
  - Level 1 (discovery): compact skill catalog in `server.instructions` + `skill://_index.json`
    in the SEP-2640 `ListSkillsResult` shape (`uri`, `frontmatter`, `resources` with SHA-256 digests).
  - Level 2 & 3 (progressive disclosure): every `SKILL.md` and `references/*.md` file is registered
    as a read-only MCP Resource (`skill://<name>/<relative_path>`), loaded on demand via `resources/read`.

Zero external dependencies (no PyYAML required) and zero hardcoded business logic: the same code
serves whatever skills live in `skills/` (e.g., the 10 generic playbooks in safe-google-ads-mcp
or private playbooks in a local deployment).
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILL_SCHEME = "skill://"
INDEX_URI = f"{SKILL_SCHEME}_index.json"
ALLOWED_SUFFIXES = frozenset({".md", ".txt", ".json"})


@dataclass(frozen=True)
class SkillResource:
    uri: str
    rel_path: str
    digest: str
    size: int


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    uri: str
    frontmatter: Dict[str, Any]
    root: Path
    resources: List[SkillResource]

    def to_sep2640(self) -> Dict[str, Any]:
        return {
            "uri": self.uri,
            "frontmatter": dict(self.frontmatter),
            "resources": [
                {"uri": r.uri, "digest": r.digest, "size": r.size}
                for r in self.resources
            ],
        }


def parse_frontmatter(text: str) -> Dict[str, Any]:
    """Parse minimal YAML frontmatter (`--- ... ---`) without third-party dependencies.

    Handles single-line `key: value`, quoted strings, and folded/literal blocks (`>`, `>-`, `|`, `|-`).
    """
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}
    end = normalized.find("\n---", 4)
    if end == -1:
        return {}

    block = normalized[4:end].splitlines()
    data: Dict[str, Any] = {}
    i = 0
    while i < len(block):
        line = block[i]
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            i += 1
            continue
        key, raw_val = line.split(":", 1)
        key = key.strip()
        val = raw_val.strip()
        if val in (">", ">-", "|", "|-"):
            fold = val.startswith(">")
            chunks: List[str] = []
            i += 1
            while i < len(block) and (block[i].startswith((" ", "\t")) or not block[i].strip()):
                chunks.append(block[i].strip())
                i += 1
            joined = " ".join(c for c in chunks if c) if fold else "\n".join(chunks).strip()
            data[key] = joined
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        data[key] = val
        i += 1
    return data


def _file_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def discover_skills(skills_dir: Path) -> List[SkillEntry]:
    """Scan `skills_dir/*/SKILL.md` and build SEP-2640 skill manifests."""
    if not skills_dir.is_dir():
        return []

    entries: List[SkillEntry] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        folder = skill_md.parent
        if folder.name.startswith((".", "_")):
            continue
        text = skill_md.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        name = str(fm.get("name") or folder.name).strip()
        description = str(fm.get("description") or "").strip()
        fm_out: Dict[str, Any] = dict(fm)
        fm_out["name"] = name
        fm_out["description"] = description

        resources: List[SkillResource] = []
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            if any(part.startswith((".", "_")) for part in path.relative_to(folder).parts):
                continue
            rel = path.relative_to(folder).as_posix()
            raw = path.read_bytes()
            resources.append(
                SkillResource(
                    uri=f"{SKILL_SCHEME}{name}/{rel}",
                    rel_path=rel,
                    digest=_file_digest(raw),
                    size=len(raw),
                )
            )

        entries.append(
            SkillEntry(
                name=name,
                description=description,
                uri=f"{SKILL_SCHEME}{name}/SKILL.md",
                frontmatter=fm_out,
                root=folder.resolve(),
                resources=resources,
            )
        )
    return entries


def build_list_skills_result(skills: List[SkillEntry]) -> Dict[str, Any]:
    """Build a SEP-2640 `skills/list` payload."""
    return {"skills": [s.to_sep2640() for s in skills]}


def build_get_skill_result(skills: List[SkillEntry], uri_or_name: str) -> Optional[Dict[str, Any]]:
    """Build a SEP-2640 `skills/get` payload by skill URI or skill name."""
    for s in skills:
        if s.uri == uri_or_name or s.name == uri_or_name:
            return {"skill": s.to_sep2640()}
    return None


def read_skill_file(skills: List[SkillEntry], skill_name: str, rel_path: str) -> str:
    """Safely read a declared skill file, blocking path traversal and undeclared files."""
    target_skill = next((s for s in skills if s.name == skill_name), None)
    if target_skill is None:
        raise ValueError(f"Unknown skill: {skill_name}")

    allowed = {r.rel_path for r in target_skill.resources}
    normalized = rel_path.replace("\\", "/").lstrip("/")
    if normalized not in allowed:
        raise ValueError(f"Resource not in skill manifest: {skill_name}/{rel_path}")

    candidate = (target_skill.root / normalized).resolve()
    if not candidate.is_relative_to(target_skill.root):
        raise ValueError(f"Path traversal blocked: {rel_path}")

    return candidate.read_text(encoding="utf-8")


def build_server_instructions(skills: List[SkillEntry]) -> str:
    """Build a compact Level-1 catalog for MCP InitializeResult.instructions."""
    if not skills:
        return ""
    lines = [
        "Available MCP Skills (read via MCP resources/read before multi-step workflows; "
        f"full SEP-2640 manifest at {INDEX_URI}):"
    ]
    for s in skills:
        summary = s.description if len(s.description) <= 220 else s.description[:217] + "..."
        lines.append(f"- {s.name} ({s.uri}): {summary}")
    return "\n".join(lines)


def register_skills_over_mcp(mcp: Any, skills_dir: Path) -> List[SkillEntry]:
    """Expose discovered skills as MCP Resources and attach a Level-1 catalog to server instructions."""
    skills = discover_skills(skills_dir)
    if not skills:
        return []

    def _register_file(skill_name: str, res: SkillResource, description: str) -> None:
        mime = "application/json" if res.rel_path.endswith(".json") else "text/markdown"

        @mcp.resource(res.uri, name=f"{skill_name}/{res.rel_path}", description=description, mime_type=mime)
        def _read() -> str:
            return read_skill_file(skills, skill_name, res.rel_path)

    for skill in skills:
        for res in skill.resources:
            desc = skill.description if res.rel_path == "SKILL.md" else f"Reference file for skill '{skill.name}': {res.rel_path}"
            _register_file(skill.name, res, desc)

    @mcp.resource(
        INDEX_URI,
        name="skills-index",
        description="SEP-2640 catalog of all skills shipped by this MCP server (URIs, frontmatter, SHA-256 manifests).",
        mime_type="application/json",
    )
    def _read_index() -> str:
        return json.dumps(build_list_skills_result(skills), indent=2, ensure_ascii=False)

    catalog = build_server_instructions(skills)
    lowlevel = getattr(mcp, "_mcp_server", None)
    if lowlevel is not None and catalog:
        existing = getattr(lowlevel, "instructions", None) or ""
        lowlevel.instructions = f"{existing}\n\n{catalog}".strip() if existing else catalog

    return skills
