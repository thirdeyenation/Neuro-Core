"""WI-P71 — pre-release naming/leak sweep pin tests (release-blocking, RV-001).

Durable regression guard for the ready-for-public-release claim surface
(D-NC1-150): committed public-facing documentation must not leak

* internal control-plane / scratch-plane paths (``.a0proj/team/``,
  ``.a0proj/decision_log``, ``notepad_temp``),
* instance-specific project paths (``/a0/usr/projects/nc1/``), or
* the internal team name ``NC1`` in user-facing prose.

Recorded scoped exclusions (WI-P71 intake rev 3, folded into PM-017):

* ``docs/decisions/`` ADR/D-NC1 record identifiers and mirror-notice /
  canonical-reference internal paths are ACCEPTED per the ADR-NC1-003
  mirror precedent (traceability, governance-critical notices). The
  decisions directory is therefore excluded from the control-plane-path
  and prose-name checks — but is still guarded against scratch-plane
  (``notepad_temp``) leaks, which no exclusion covers.
* Generic framework paths (e.g. ``/a0/usr/projects/<project>/...``) are
  in-policy and not flagged.

Fixture discipline: reads plugin-plane files only; no live db/sidecar access.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = PLUGIN_ROOT / "docs"

# Public-facing doc surfaces (plugin root + docs/, excluding docs/decisions/).
PUBLIC_DOC_PATHS = sorted(
    [PLUGIN_ROOT / "README.md", PLUGIN_ROOT / "CHANGELOG.md"]
    + [p for p in DOCS_ROOT.glob("*.md")]
)
# ADR mirrors: guarded against scratch-plane leaks only (scoped exclusions above).
DECISION_DOC_PATHS = sorted((DOCS_ROOT / "decisions").glob("*.md"))

FORBIDDEN_PATHS = (
    ".a0proj/team/",
    ".a0proj/decision_log",
    "notepad_temp",
    "/a0/usr/projects/nc1/",
)

# 'NC1' NOT part of a record identifier (ADR-NC1-<digits> / D-NC1-<digits>).
# A bare 'NC1' token (word boundary on the left, not preceded by 'ADR-'/'D-',
# not followed by '-<digit>') is a prose occurrence.
_PROSE_NC1 = re.compile(r"(?<![\w-])NC1(?!-\d)")
_RECORD_ID = re.compile(r"(?:ADR|D)-NC1-\d")


def _doc_id(path: Path) -> str:
    return str(path.relative_to(PLUGIN_ROOT))


def _forbidden_path_hits(text: str) -> list[str]:
    return [pat for pat in FORBIDDEN_PATHS if pat in text]


def _prose_nc1_hits(text: str) -> list[str]:
    hits = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for m in _PROSE_NC1.finditer(line):
            if _RECORD_ID.search(line):
                # Line contains a record identifier; verify this occurrence
                # is not part of one by checking the immediate context.
                start = max(0, m.start() - 4)
                context = line[start : m.end() + 8]
                if _RECORD_ID.search(context):
                    continue
            hits.append(f"line {line_no}: {line.strip()[:120]}")
            break
    return hits


@pytest.mark.parametrize("doc_path", PUBLIC_DOC_PATHS, ids=_doc_id)
def test_public_doc_no_internal_control_plane_paths(doc_path: Path):
    """No committed public-facing doc contains internal control-plane or
    instance-specific paths (WI-P71 leak policy)."""
    text = doc_path.read_text(encoding="utf-8")
    hits = _forbidden_path_hits(text)
    assert not hits, f"{_doc_id(doc_path)} leaks internal paths: {hits}"


@pytest.mark.parametrize("doc_path", PUBLIC_DOC_PATHS, ids=_doc_id)
def test_public_doc_no_prose_nc1_naming(doc_path: Path):
    """No committed public-facing doc uses the internal team name 'NC1' in
    prose (public-naming rule, D-NC1-146: public prose uses 'Neuro Core').
    ADR-/D-NC1- record identifiers are exempt."""
    text = doc_path.read_text(encoding="utf-8")
    hits = _prose_nc1_hits(text)
    assert not hits, f"{_doc_id(doc_path)} prose NC1 occurrences: {hits}"


@pytest.mark.parametrize("doc_path", DECISION_DOC_PATHS, ids=_doc_id)
def test_decision_doc_no_scratch_plane_leaks(doc_path: Path):
    """ADR mirrors must not leak scratch-plane paths (no exclusion covers
    notepad_temp; control-plane reference paths are accepted per intake
    rev 3 scoped exclusion)."""
    text = doc_path.read_text(encoding="utf-8")
    assert "notepad_temp" not in text, (
        f"{_doc_id(doc_path)} leaks a scratch-plane (notepad_temp) path"
    )
