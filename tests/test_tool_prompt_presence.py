"""KI-055 (WI-P76): all six Neuro Core tools must be exposed to the agent.

Tool exposure is prompt-file driven: the framework's system-prompt extension
(``/a0/extensions/python/system_prompt/_11_tools_prompt.py``) collects
``agent.system.tool.*.md`` files from plugin ``prompts/`` directories and
injects them into the agent's tool list. A missing prompt file means the
tool is invisible to the agent even though its class dispatches by name.

This test pins the presence and non-emptiness of all six expected tool
prompt files so the exposure surface cannot silently regress.
"""

from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PLUGIN_ROOT / "prompts"

EXPECTED_TOOL_PROMPT_FILES = [
    "agent.system.tool.memory_reflect.md",
    "agent.system.tool.memory_relate.md",
    "agent.system.tool.memory_score.md",
    "agent.system.tool.neuro_capture.md",
    "agent.system.tool.neuro_retrieve.md",
    "agent.system.tool.neuro_validate.md",
]


@pytest.mark.parametrize("filename", EXPECTED_TOOL_PROMPT_FILES)
def test_tool_prompt_file_present_and_non_empty(filename):
    """Every documented tool must have a non-empty prompt file."""
    path = PROMPTS_DIR / filename
    assert path.is_file(), f"missing tool prompt file: {filename}"
    content = path.read_text(encoding="utf-8")
    assert content.strip(), f"tool prompt file is empty: {filename}"


def test_no_unexpected_tool_prompt_files():
    """Pin the exact exposure surface: no extra tool prompt files exist."""
    actual = sorted(p.name for p in PROMPTS_DIR.glob("agent.system.tool.*.md"))
    assert actual == sorted(EXPECTED_TOOL_PROMPT_FILES), (
        "unexpected tool prompt surface — update EXPECTED_TOOL_PROMPT_FILES "
        "if a new tool was intentionally added"
    )


def test_capture_prompt_documents_required_args():
    """The neuro_capture prompt must document its actual required args."""
    content = (PROMPTS_DIR / "agent.system.tool.neuro_capture.md").read_text(
        encoding="utf-8"
    )
    assert "`text`" in content
    assert "`project`" in content


def test_validate_prompt_documents_state_values():
    """The neuro_validate prompt must document the four state values."""
    content = (PROMPTS_DIR / "agent.system.tool.neuro_validate.md").read_text(
        encoding="utf-8"
    )
    for state in ("unreviewed", "validated", "disputed", "superseded"):
        assert f"`{state}`" in content
