#!/bin/sh
# WP-02: does this repo work from a fresh clone?
#
# CI is not this question. CI installs into a checkout that came from a repo which already has every
# directory in it, on an OS whose paths were proven once, in a workflow file that was itself edited to stop
# failing. What CI cannot see is a dependency that resolves only because of an untracked directory on the
# author's disk. That is not hypothetical: `experiments/RealtimeSTT` was gitignored and load-bearing, so
# `uv sync` failed outright on a fresh clone with an error naming a missing distribution instead of the
# missing clone step.
#
# So: clone into a throwaway directory, install from nothing, run everything, and check that a server with
# no credentials refuses to start instead of starting open.
#
# Usage:
#   tools/verify_fresh_clone.sh [target-dir]
#
# Environment:
#   REPO        repo to clone (default: the repo this script lives in)
#   KEEP=1      leave the clone in place afterwards
#
# Needs network for `npm ci` and for the pinned git dependencies, and a few minutes on a cold cache. It
# clones HEAD, so uncommitted edits in the source tree are NOT what gets tested; the script says so when it
# notices them. Run it on a pushed commit to verify what other people will get.

set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO=${REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}
DIR=${1:-"${TMPDIR:-/tmp}/pi-chat-fresh-clone"}
FAILURES=0

step() {
    name=$1
    shift
    printf '\n=== %s\n' "$name"
    if "$@"; then
        printf -- '--- PASS: %s\n' "$name"
    else
        printf -- '--- FAIL: %s\n' "$name"
        FAILURES=$((FAILURES + 1))
    fi
}

BRANCH=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
echo "source repo: $REPO (branch $BRANCH, HEAD $(git -C "$REPO" rev-parse --short HEAD))"
if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
    echo "NOTE: the source tree has uncommitted changes. This run verifies HEAD, not those edits."
fi

echo "clone dir:   $DIR"
rm -rf "$DIR"

step "git clone" git clone -q --branch "$BRANCH" "$REPO" "$DIR"

if [ -d "$DIR/experiments" ]; then
    echo "unexpected: experiments/ exists in the clone, so the gitignore is not doing its job"
    FAILURES=$((FAILURES + 1))
fi

cd "$DIR"
# Pick the interpreter by platform, not by which file happens to exist. Testing for `.venv/Scripts/python.exe`
# looks like the clever version and is wrong here: this runs before `uv sync`, so the venv does not exist yet,
# the test is false on every platform, and Windows silently falls through to a Linux path. That bug cost one
# full run of this script: the clone synced fine, `npm ci` passed, and every Python step failed with "No such
# file or directory" naming a path that had nothing to do with the machine.
case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN* | Windows_NT) PY=".venv/Scripts/python.exe" ;;
    *)                                      PY=".venv/bin/python" ;;
esac

step "uv sync --group dev (no extras, nothing vendored)" uv sync --group dev

step "python version" "$PY" -c "import sys; print(sys.version)"

step "pytest (default markers)" "$PY" -m pytest -q -m "not audio_gpu and not audio_cpu" \
    --timeout=120 -p no:cacheprovider

step "config reference is current" "$PY" tools/config_reference.py --check
step "fixture census" sh -c "$PY tools/capture.py --census 'data-samples/*.jsonl'"
step "no agent memory in fixtures" "$PY" tools/scrub_captures.py --check

step "npm ci" npm ci --silent
step "frontend/protocol parity tests" npm test

step "websocket smoke test" sh -c "PI_MEMORY_NOW=off '$PY' tools/smoke_test_ws.py"

# The refusal check has to look like a real fresh install: no .env, no exported hashes. The clone has no
# .env by construction, so dropping the two variables from the environment is the whole setup. A server that
# boots without credentials is the worst possible default, and it is the default a first-time reader gets if
# this check is ever softened. The expected outcome is a non-zero exit that says why, so a non-zero exit for
# some other reason has to fail too - otherwise any crash at startup would look like a pass.
printf '\n=== refuses to start with no password hash\n'
# `timeout` because the failure mode this check guards against is the server staying up, and a script that
# hangs is not a check anyone runs twice.
if timeout 180 env -u PI_CHAT_B_PASSWORD_HASH -u PI_CHAT_R_PASSWORD_HASH PI_MEMORY_NOW=off \
        PI_CHAT_HOST=127.0.0.1 PI_CHAT_PORT=9097 "$PY" server.py >"$DIR/refusal.log" 2>&1; then
    echo "--- FAIL: the server started with no credentials"
    FAILURES=$((FAILURES + 1))
elif grep -q "refusing to start" "$DIR/refusal.log"; then
    echo "--- PASS: refused, and said how to configure a hash"
    sed -n '1,6p' "$DIR/refusal.log"
else
    echo "--- FAIL: it exited non-zero but not for the credential check:"
    tail -12 "$DIR/refusal.log"
    FAILURES=$((FAILURES + 1))
fi

printf '\n=========================================\n'
if [ "$FAILURES" -eq 0 ]; then
    echo "fresh clone: all checks passed ($(git rev-parse --short HEAD) on $BRANCH)"
    [ "${KEEP:-0}" = "1" ] || rm -rf "$DIR"
    exit 0
fi
echo "fresh clone: $FAILURES check(s) failed. Clone left at $DIR for inspection."
exit 1
