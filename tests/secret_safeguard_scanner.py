"""Pre-publication secret-exclusion scanner (WI-P118-KI059-SECRET-SAFEGUARD).

Test-only code living under tests/ by design (ARC condition C3): this is a
team/development safeguard, NOT product capability, and must never be moved
into the plugin's helpers/ or shipped as part of the public contract.

What it does
------------
Scans the plugin repository's externalization-relevant surfaces for credential
material before any container-external action (push, PR, submission, or any
other transfer of repository-derived content — a non-exhaustive set):

1. ``.git/config`` remote URLs — the KI-059 exposure vector. A remote URL
   must contain no credential material (GitHub-style token prefixes,
   ``https://<credential>@`` userinfo patterns, or any value present in the
   runtime secrets file).
2. Tracked repository content — committed credential patterns.
3. Any caller-supplied text (so future externalization payloads — PR bodies,
   submission files — can be scanned by the same check).

Secrets-file interaction (ARC condition C1, HITL binding): the scanner reads
secret values solely as comparison inputs for exclusion checking. It never
modifies, wraps, or depends on changes to the Secrets Manager feature, and it
never writes to any secrets file.

Redaction (ARC condition C2): findings report location and pattern class
only — never the matched value. Test fixtures use synthetic credential
material only; live secret values never appear in any output, log, report,
or artifact.

Honesty discipline (ARC condition C5): this scanner is a test-backed
exclusion check. It is NOT a security boundary and NOT a complete data-leak
prevention mechanism. Pattern-based detection has inherent false negatives
(novel credential formats, non-GitHub tokens); the documented recovery
procedure in CONTRIBUTING.md is the backstop. It must not be described as
leak prevention on any surface.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Default location of the runtime secrets file, relative to the framework
# root. Read-only observation only (C1); overridable so tests can point the
# scanner at synthetic fixtures instead of the live file (C2).
DEFAULT_SECRETS_PATH = Path("/a0/usr/secrets.env")

# GitHub-style token prefixes (ghp_/gho_/ghs_/ghu_/ghr_ — the KI-059 vector
# was a ghp_ token). Matched case-sensitively; a 20+ char alphanumeric body
# is required so documentation mentions of the bare prefixes cannot trigger.
_TOKEN_PREFIX_RE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})\b")

# Credential-bearing userinfo in URLs: a token-prefix credential, or a
# run of >=20 token-ish characters, in the userinfo position of a URL.
# A bare ``https://<token>@host`` with a placeholder like ``<token>`` does
# not match (no token-ish run), which keeps documentation examples clean.
_USERINFO_RE = re.compile(r"https?://[A-Za-z0-9_\-.]*?(?:gh[pousr]_[A-Za-z0-9]{20,}|[A-Za-z0-9_\-]{20,})@")

# Generic assignment-style credential keys that should never carry a value
# in tracked content or config files (e.g. ``password = hunter2``).
_CRED_KEY_RE = re.compile(
    r"(?i)\b(password|passwd|api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)\b\s*[:=]\s*(?![\"']?\s*[\$<{\[])(\S{8,})"
)


@dataclass
class Finding:
    """A redacted finding: location and pattern class only — never values (C2)."""

    location: str
    pattern_class: str
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.pattern_class}] {self.location}: {self.detail}"


@dataclass
class ScanResult:
    """Aggregated scan result. ``findings`` are redacted by construction."""

    findings: list = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.clean:
            return "clean: no credential material detected"
        lines = [f"{len(self.findings)} finding(s):"]
        lines.extend(f"  - {f}" for f in self.findings)
        return "\n".join(lines)


def _load_secret_values(secrets_path: Path | None) -> list:
    """Read secret values as comparison inputs ONLY (read-only, C1).

    Returns a list of non-trivial values (len >= 8) found in the secrets
    file. Never logs, returns, or stores file contents beyond the in-memory
    comparison list. Missing or unreadable file yields an empty list.
    """

    path = secrets_path if secrets_path is not None else DEFAULT_SECRETS_PATH
    values = []
    try:
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                value = line.split("=", 1)[1].strip().strip('"\'')
                if len(value) >= 8:
                    values.append(value)
    except OSError:
        pass  # unreadable -> no comparison values; other layers still apply
    return values


def scan_text(text, location="<text>", secret_values=None):
    """Scan one text blob for credential material. Findings are redacted (C2)."""

    findings = []
    for i, line in enumerate(text.splitlines(), start=1):
        loc = f"{location}:{i}"
        if _TOKEN_PREFIX_RE.search(line):
            findings.append(Finding(loc, "token-prefix", "credential-pattern match (value redacted)"))
        if _USERINFO_RE.search(line):
            findings.append(Finding(loc, "url-userinfo-credential", "credential-bearing URL userinfo (value redacted)"))
        if _CRED_KEY_RE.search(line):
            findings.append(Finding(loc, "credential-key-value", "credential-style key with literal value (value redacted)"))
        for value in secret_values or []:
            if value in line:
                findings.append(Finding(loc, "secrets-value-match", "matches a runtime secret value (value redacted)"))
                break  # one redacted finding per line for this class
    return findings


def scan_git_config(git_config_path, secret_values=None):
    """Scan a git config file (the KI-059 vector) for credential material."""

    path = Path(git_config_path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return scan_text(text, location=str(path), secret_values=secret_values)


def scan_remote_urls(git_config_path, secret_values=None):
    """Scan only the ``url =`` entries of a git config for credential material."""

    path = Path(git_config_path)
    findings = []
    if not path.is_file():
        return findings
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("url") and "=" in stripped:
            url = stripped.split("=", 1)[1].strip()
            findings.extend(scan_text(url, location=f"{path}:{i} (remote url)", secret_values=secret_values))
    return findings


def scan_tracked_content(repo_root, secret_values=None, git_config_rel=".git/config"):
    """Scan all git-tracked files under ``repo_root`` for credential patterns.

    Uses ``git ls-files`` so exactly the tracked set is scanned. The repo's
    own ``.git/config`` is scanned separately by :func:`scan_git_config`.
    """

    import subprocess

    root = Path(repo_root)
    findings = []
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=str(root), capture_output=True, text=True, check=True, timeout=60
        )
        tracked = [line for line in out.stdout.splitlines() if line.strip()]
    except (subprocess.SubprocessError, OSError):
        findings.append(Finding(str(root), "scan-error", "could not enumerate tracked files"))
        return findings

    for rel in tracked:
        fpath = root / rel
        try:
            if not fpath.is_file():
                continue
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            findings.append(Finding(str(fpath), "scan-error", "unreadable tracked file"))
            continue
        findings.extend(scan_text(text, location=str(fpath), secret_values=secret_values))
    return findings


def scan_repository(repo_root, secrets_path=None):
    """Full pre-publication scan of a repository (Components 1's standing check).

    Covers: .git/config remote URLs (KI-059 vector), the full .git/config,
    and all tracked content. Secret values are used as comparison inputs
    only (C1); findings are redacted (C2).
    """

    root = Path(repo_root)
    secret_values = _load_secret_values(secrets_path)
    git_config = root / ".git" / "config"
    findings = []
    findings.extend(scan_remote_urls(git_config, secret_values))
    findings.extend(scan_git_config(git_config, secret_values))
    findings.extend(scan_tracked_content(root, secret_values))
    return ScanResult(findings=findings)
