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
# A forwarded positional path argument (e.g. tests/test_hooks.py) replaces the
# default tests/ target so the run is narrowed to that path (WI-P104-PM023);
# pytest options such as -k/-x are forwarded as-is and combine with it.
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

# Split forwarded arguments into a pytest target and remaining pytest options.
# The first positional (non-option) argument that is not the value of a
# value-taking option becomes the target, narrowing the run (default: tests/).
TARGET="tests/"
TARGET_SET=0
EXTRA_ARGS=()
PREV_ARG=""
for arg in "$@"; do
    case "$arg" in
        -*)
            EXTRA_ARGS+=("$arg")
            ;;
        *)
            case "$PREV_ARG" in
                -k|-m|-r|--tb|--maxfail|--deselect|-W)
                    # value of a value-taking option, never a path
                    EXTRA_ARGS+=("$arg")
                    ;;
                *)
                    if [ "$TARGET_SET" -eq 0 ]; then
                        TARGET="$arg"
                        TARGET_SET=1
                    else
                        EXTRA_ARGS+=("$arg")
                    fi
                    ;;
            esac
            ;;
    esac
    PREV_ARG="$arg"
done

exec "$A0_PY" -m pytest "$TARGET" -q ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
