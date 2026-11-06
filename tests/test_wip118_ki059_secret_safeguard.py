"""WI-P118-KI059-SECRET-SAFEGUARD — tests for the pre-publication secret-exclusion scanner.

All fixtures are synthetic and constructed at runtime (ARC condition C2): no live
secret value ever appears in this file, in test output, or in any artifact. The
synthetic tokens below are built from a prefix plus a generated body so this
tracked test file itself never contains a full matching token literal (which
would otherwise self-flag the baseline scan).

Honesty discipline (ARC condition C5): the scanner under test is a test-backed
exclusion check. It is NOT a security boundary and NOT a complete data-leak
prevention mechanism. These tests verify detection of known pattern classes and
redaction of findings — nothing more.
"""

from __future__ import annotations

import string
import sys
import uuid
from pathlib import Path

import pytest

# tests/-local importable module (ARC condition C3: test-only code under tests/).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secret_safeguard_scanner as scanner  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _synthetic_token(prefix="ghp_") -> str:
    """Build a synthetic GitHub-style token at runtime (never a real value)."""

    body = "".join(uuid.uuid4().hex for _ in range(2))[:36]
    return f"{prefix}{body}"


def _synthetic_secret_value() -> str:
    """Build a synthetic secret comparison value at runtime."""

    return "s3cr3t-" + uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Positive detection cases (synthetic fixtures)
# ---------------------------------------------------------------------------


def test_detects_token_prefix_in_text():
    token = _synthetic_token()
    findings = scanner.scan_text(f"url = https://{token}@github.com/example/repo.git", location="fixture")
    classes = {f.pattern_class for f in findings}
    assert "token-prefix" in classes
    assert "url-userinfo-credential" in classes


def test_detects_bare_token_prefix():
    token = _synthetic_token("ghs_")
    findings = scanner.scan_text(f"token = {token}", location="fixture")
    assert [f.pattern_class for f in findings] == ["token-prefix"]


def test_detects_userinfo_url_without_token_prefix():
    # A long non-prefix credential run in userinfo position is still flagged.
    cred = uuid.uuid4().hex + uuid.uuid4().hex  # 64 token-ish chars
    findings = scanner.scan_text(f"url = https://{cred}@github.com/example/repo.git", location="fixture")
    assert "url-userinfo-credential" in {f.pattern_class for f in findings}


def test_detects_secrets_value_match():
    value = _synthetic_secret_value()
    findings = scanner.scan_text(f"config: {value}", location="fixture", secret_values=[value])
    assert [f.pattern_class for f in findings] == ["secrets-value-match"]


def test_detects_credential_key_with_value():
    findings = scanner.scan_text('password = "sup3rs3cretvalue"', location="fixture")
    assert "credential-key-value" in {f.pattern_class for f in findings}


def test_detects_token_in_git_config_file(tmp_path):
    token = _synthetic_token()
    cfg = tmp_path / "config"
    cfg.write_text(f'[remote "origin"]\n\turl = https://{token}@github.com/example/repo.git\n')
    findings = scanner.scan_git_config(cfg)
    assert findings, "credential-bearing remote URL must be detected"
    assert all(token not in str(f) for f in findings)  # redaction (C2)


def test_scan_tracked_content_finds_committed_token(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    token = _synthetic_token()
    (repo / "leaked.txt").write_text(f"note: {token}\n")
    subprocess.run(["git", "add", "leaked.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=str(repo), check=True)
    findings = scanner.scan_tracked_content(str(repo))
    assert any(f.pattern_class == "token-prefix" for f in findings)
    assert all(token not in str(f) for f in findings)  # redaction (C2)


# ---------------------------------------------------------------------------
# Negative cases (must NOT flag)
# ---------------------------------------------------------------------------


def test_clean_remote_url_passes():
    findings = scanner.scan_text("url = https://github.com/example/repo.git", location="fixture")
    assert findings == []


def test_placeholder_userinfo_passes():
    # Documentation-style placeholder must not false-positive.
    findings = scanner.scan_text("url = https://<token>@github.com/example/repo.git", location="fixture")
    assert findings == []


def test_short_random_run_passes():
    findings = scanner.scan_text("url = https://abc123@github.com/example/repo.git", location="fixture")
    assert findings == []


def test_clean_text_passes():
    assert scanner.scan_text("plain documentation line", location="fixture") == []


# ---------------------------------------------------------------------------
# Redaction discipline (C2)
# ---------------------------------------------------------------------------


def test_findings_never_contain_values():
    token = _synthetic_token()
    value = _synthetic_secret_value()
    text = f"url = https://{token}@github.com/x.git\nkey: {value}\n"
    findings = scanner.scan_text(text, location="fixture", secret_values=[value])
    assert findings
    for f in findings:
        rendered = str(f)
        assert token not in rendered, "token value leaked into finding"
        assert value not in rendered, "secret value leaked into finding"
        assert f.detail.endswith("(value redacted)")


# ---------------------------------------------------------------------------
# Secrets-file comparison path (read-only, C1) — synthetic fixture file
# ---------------------------------------------------------------------------


def test_secrets_file_values_used_as_comparison(tmp_path):
    value = _synthetic_secret_value()
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text(f"# comment\nSYNTHETIC_KEY={value}\n\nOTHER=short\n")
    values = scanner._load_secret_values(secrets_file)
    assert value in values
    assert "short" not in values  # below the trivial-value threshold


def test_missing_secrets_file_is_tolerated(tmp_path):
    assert scanner._load_secret_values(tmp_path / "absent.env") == []


# ---------------------------------------------------------------------------
# Standing baseline check: the live repository must be clean
# ---------------------------------------------------------------------------


def test_live_repository_is_clean():
    """The standing pre-publication check over the live plugin repository.

    Runs on every suite execution (the mandatory regression baseline), giving
    the KI-059 safeguard standing enforcement. Uses the runtime secrets file
    read-only as comparison input (C1); findings would be redacted (C2).
    """

    result = scanner.scan_repository(str(PLUGIN_ROOT))
    assert result.clean, f"credential material detected:\n{result}"


def test_scanner_module_is_test_only():
    """C3: the scanner must live under tests/, never under plugin helpers/."""

    module_path = Path(scanner.__file__).resolve()
    assert module_path.parent.name == "tests"
    assert "helpers" not in module_path.parts
    assert not (PLUGIN_ROOT / "helpers" / "secret_safeguard_scanner.py").exists()
