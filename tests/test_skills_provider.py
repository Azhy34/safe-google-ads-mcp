"""Tests for core.skills_provider (Skills over MCP / SEP-2640 structure)."""
import asyncio
import json
from pathlib import Path
import sys

import pytest
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.skills_provider import (
    INDEX_URI,
    build_get_skill_result,
    build_list_skills_result,
    discover_skills,
    parse_frontmatter,
    read_skill_file,
    register_skills_over_mcp,
)


def _make_sample_skills(tmp_path: Path) -> Path:
    skills_dir = tmp_path / "skills"
    s1 = skills_dir / "demo-bid-ladder"
    (s1 / "references").mkdir(parents=True)
    (s1 / "SKILL.md").write_text(
        "---\n"
        "name: demo-bid-ladder\n"
        "description: >-\n"
        "  Deterministic bid stepping workflow for Maximize Clicks campaigns.\n"
        "  Uses fixed increments.\n"
        "---\n\n"
        "# Demo Bid Ladder\n",
        encoding="utf-8",
    )
    (s1 / "references" / "rules.md").write_text("# Rules\nAlways check 14-day baseline.\n", encoding="utf-8")

    s2 = skills_dir / "demo-auditor"
    s2.mkdir(parents=True)
    (s2 / "SKILL.md").write_text(
        "---\n"
        'name: "demo-auditor"\n'
        "description: Audits campaign structure.\n"
        "---\n\n"
        "# Auditor\n",
        encoding="utf-8",
    )
    # Hidden / cache folders must be ignored
    (skills_dir / "__pycache__").mkdir()
    (skills_dir / "__pycache__" / "SKILL.md").write_text("---\nname: bad\n---\n", encoding="utf-8")
    return skills_dir


def test_parse_frontmatter_handles_folded_and_quoted():
    text = (
        "---\n"
        'name: "sample-skill"\n'
        "description: >-\n"
        "  First line\n"
        "  second line.\n"
        "---\n# Body\n"
    )
    fm = parse_frontmatter(text)
    assert fm["name"] == "sample-skill"
    assert fm["description"] == "First line second line."


def test_discover_and_sep2640_payloads(tmp_path: Path):
    skills_dir = _make_sample_skills(tmp_path)
    skills = discover_skills(skills_dir)
    assert [s.name for s in skills] == ["demo-auditor", "demo-bid-ladder"]

    listed = build_list_skills_result(skills)
    assert len(listed["skills"]) == 2
    bid = build_get_skill_result(skills, "skill://demo-bid-ladder/SKILL.md")
    assert bid is not None
    assert bid["skill"]["frontmatter"]["name"] == "demo-bid-ladder"
    assert "Maximize Clicks" in bid["skill"]["frontmatter"]["description"]

    res_uris = [r["uri"] for r in bid["skill"]["resources"]]
    assert "skill://demo-bid-ladder/SKILL.md" in res_uris
    assert "skill://demo-bid-ladder/references/rules.md" in res_uris
    for r in bid["skill"]["resources"]:
        assert r["digest"].startswith("sha256:")
        assert len(r["digest"]) == len("sha256:") + 64
        assert r["size"] > 0


def test_discover_missing_dir_returns_empty(tmp_path: Path):
    assert discover_skills(tmp_path / "does-not-exist") == []


def test_read_skill_file_blocks_traversal(tmp_path: Path):
    skills_dir = _make_sample_skills(tmp_path)
    (tmp_path / ".env").write_text("SECRET=123\n", encoding="utf-8")
    skills = discover_skills(skills_dir)

    assert "Demo Bid Ladder" in read_skill_file(skills, "demo-bid-ladder", "SKILL.md")
    assert "14-day baseline" in read_skill_file(skills, "demo-bid-ladder", "references/rules.md")

    with pytest.raises(ValueError):
        read_skill_file(skills, "demo-bid-ladder", "../../.env")
    with pytest.raises(ValueError):
        read_skill_file(skills, "unknown-skill", "SKILL.md")


def test_register_skills_over_mcp_exposes_resources_and_instructions(tmp_path: Path):
    skills_dir = _make_sample_skills(tmp_path)
    mcp = FastMCP("Test Ads")
    registered = register_skills_over_mcp(mcp, skills_dir)
    assert len(registered) == 2

    resources = asyncio.run(mcp.list_resources())
    uris = {str(r.uri) for r in resources}
    assert INDEX_URI in uris
    assert "skill://demo-bid-ladder/SKILL.md" in uris
    assert "skill://demo-bid-ladder/references/rules.md" in uris
    assert "skill://demo-auditor/SKILL.md" in uris

    index_raw = asyncio.run(mcp.read_resource(INDEX_URI))
    index_text = index_raw[0].content if isinstance(index_raw, list) else str(index_raw)
    payload = json.loads(index_text)
    assert len(payload["skills"]) == 2

    doc_raw = asyncio.run(mcp.read_resource("skill://demo-bid-ladder/references/rules.md"))
    doc_text = doc_raw[0].content if isinstance(doc_raw, list) else str(doc_raw)
    assert "14-day baseline" in doc_text

    instructions = mcp._mcp_server.instructions or ""
    assert "demo-bid-ladder" in instructions
    assert "skill://demo-bid-ladder/SKILL.md" in instructions
