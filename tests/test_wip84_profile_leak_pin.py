"""WI-P84 - profile-surface leak-pin tests (D-NC1-146 / CAP-31).

Extends the WI-P71 pre-release naming/leak sweep to the plugin-distributed
agent profile surface (agents/neuro-core-memory-operator/): the profile
metadata and prompt text are public-facing and must not leak internal
team names ("NC1" in prose), internal control-plane / instance paths, or
prohibited automaticity claims (end-to-end automatic memory management -
the ratified CAP-31 boundary is assisted-manual curation over an
automatically maintained store).

Fixture discipline: reads plugin-plane files only; no live db/sidecar access.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = PLUGIN_ROOT / "agents" / "neuro-core-memory-operator"
PROFILE_FILES = sorted(
    [PROFILE_DIR / "agent.yaml"]
    + [p for p in (PROFILE_DIR / "prompts").glob("*.md")]
)

FORBIDDEN_PATHS = (
    ".a0proj/team/",
    ".a0proj/decision_log",
    "notepad_temp",
    "/a0/usr/projects/nc1/",
)

# Prohibited automaticity-claim markers (CAP-31 boundary; ARC condition C12
# discipline: no end-to-end automatic management claims on any surface).
PROHIBITED_CLAIM_PATTERNS = (
    re.compile(r"end-to-end automatic memory management", re.IGNORECASE),
    re.compile(r"fully automatic memory management", re.IGNORECASE),
    re.compile(r"automatically (?:manages|manage) (?:your )?memory(?: for you)?", re.IGNORECASE),
)

# 'NC1' NOT part of a record identifier (ADR-NC1-<digits> / D-NC1-<digits>).
_PROSE_NC1 = re.compile(r"(?<![\w-])NC1(?!-\d)")
_RECORD_ID = re.compile(r"(?:ADR|D)-NC1-\d")


def _rel(path: Path) -> str:
    return str(path.relative_to(PLUGIN_ROOT))


def _prose_nc1_hits(text: str) -> list[str]:
    hits = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for m in _PROSE_NC1.finditer(line):
            if _RECORD_ID.search(line):
                start = max(0, m.start() - 4)
                context = line[start : m.end() + 8]
                if _RECORD_ID.search(context):
                    continue
            hits.append(f"line {line_no}: {line.strip()[:120]}")
            break
    return hits


def _prohibited_claim_hits(text: str) -> list[str]:
    hits = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for pat in PROHIBITED_CLAIM_PATTERNS:
            if pat.search(line):
                hits.append(f"line {line_no}: {line.strip()[:120]}")
                break
    return hits


@pytest.mark.parametrize("profile_file", PROFILE_FILES, ids=_rel)
def test_profile_file_no_internal_control_plane_paths(profile_file: Path):
    """No profile file leaks internal control-plane or instance paths."""
    text = profile_file.read_text(encoding="utf-8")
    hits = [pat for pat in FORBIDDEN_PATHS if pat in text]
    assert not hits, f"{_rel(profile_file)} leaks internal paths: {hits}"


@pytest.mark.parametrize("profile_file", PROFILE_FILES, ids=_rel)
def test_profile_file_no_prose_nc1_naming(profile_file: Path):
    """No profile file uses the internal team name 'NC1' in prose
    (public-naming rule, D-NC1-146: public prose uses 'Neuro Core')."""
    text = profile_file.read_text(encoding="utf-8")
    hits = _prose_nc1_hits(text)
    assert not hits, f"{_rel(profile_file)} prose NC1 occurrences: {hits}"


@pytest.mark.parametrize("profile_file", PROFILE_FILES, ids=_rel)
def test_profile_file_no_prohibited_automaticity_claims(profile_file: Path):
    """No profile file claims end-to-end automatic memory management
    (CAP-31 automaticity boundary: assisted-manual curation over an
    automatically maintained store)."""
    text = profile_file.read_text(encoding="utf-8")
    hits = _prohibited_claim_hits(text)
    assert not hits, f"{_rel(profile_file)} prohibited automaticity claims: {hits}"


def test_profile_agent_yaml_schema_fields():
    """agent.yaml validates against the framework SubAgentListItem fields:
    title/description/context present; profile directory name is the
    profile key; public title follows D-NC1-146."""
    import yaml

    raw = (PROFILE_DIR / "agent.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert data["title"] == "Neuro Core Memory Operator"
    assert isinstance(data["description"], str) and data["description"].strip()
    assert "context" in data
    assert PROFILE_DIR.name == "neuro-core-memory-operator"
