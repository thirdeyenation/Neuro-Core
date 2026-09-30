#!/usr/bin/env bash
# Neuro Core (NC1) standing test-suite entry point.
#
# Establishes the correct invocation structurally (WI-P49-RUNNER-WRAPPER):
#   - prepends PYTHONPATH=/a0 (preserving any pre-existing value), so tests
#     importing usr.plugins.neuro_core.* always resolve;
#   - uses the agent execution runtime at /opt/venv/bin/python;
#   - always runs from the plugin root, wherever the wrapper is called from;
#   - forwards any extra arguments to pytest (e.g. -k name, tests/file.py).
#
# Usage (from anywhere):
#   /a0/usr/plugins/neuro_core/tests/run_suite.sh
#   /a0/usr/plugins/neuro_core/tests/run_suite.sh tests/test_api.py -k graph
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ ! -f "$PLUGIN_ROOT/plugin.yaml" ] || [ ! -d "$PLUGIN_ROOT/tests" ]; then
    echo "run_suite.sh: cannot locate the neuro_core plugin root" >&2
    echo "  (expected tests/run_suite.sh to live inside <plugin_root>/tests/)" >&2
    exit 64
fi

cd "$PLUGIN_ROOT"

if [ -n "${PYTHONPATH:-}" ]; then
    export PYTHONPATH="/a0:${PYTHONPATH}"
else
    export PYTHONPATH="/a0"
fi

A0_PY=/opt/venv/bin/python
if [ ! -x "$A0_PY" ]; then
    echo "run_suite.sh: required interpreter not found: $A0_PY" >&2
    exit 65
fi

exec "$A0_PY" -m pytest tests/ -q "$@"
