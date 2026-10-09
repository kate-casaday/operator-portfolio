#!/bin/zsh
# Run the prototype.  Reads the API key from the macOS Keychain only when a live mode is requested.
#   bin/run.sh                 -> dashboard in mock mode (no API calls, no cost)
#   bin/run.sh live            -> dashboard with live Claude (Opus 5) for patient replies
#   bin/run.sh eval [args...]          -> the paid CLASSIFICATION evaluation (eval/run_eval.py; needs --yes-i-accept-paid-calls)
#   bin/run.sh eval-compose [args...]  -> the paid WORDING evaluation (eval/run_compose_eval.py)
#   bin/run.sh openers [args...]       -> the paid 20-opener sample set (eval/opener_samples.py)
#   bin/run.sh test                    -> full test suite, mock mode
set -e
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || { echo "no .venv; run: python3.11 -m venv .venv && .venv/bin/pip install anthropic"; exit 1; }
SERVICE="chief-of-health-demo-anthropic"
mode="${1:-mock}"; shift || true
load_key() {
  # Fail here, with a credential error, rather than starting a live mode with an empty key.
  local key
  if ! key="$(security find-generic-password -a "$USER" -s "$SERVICE" -w 2>/dev/null)" || [ -z "$key" ]; then
    echo "credential error: Keychain item '$SERVICE' not found or empty. Run bin/store-key.sh first." >&2
    exit 3
  fi
  export ANTHROPIC_API_KEY="$key"
}
case "$mode" in
  mock)  exec $PY -m ocp serve ;;
  test)  exec $PY -m unittest discover -s tests -t . ;;
  live)  load_key; exec $PY -m ocp serve --model anthropic ;;
  eval)  load_key; exec $PY eval/run_eval.py --adapter anthropic "$@" ;;
  eval-compose) load_key; exec $PY eval/run_compose_eval.py --composer anthropic "$@" ;;
  openers) load_key; exec $PY eval/opener_samples.py --composer anthropic "$@" ;;
  *) echo "usage: bin/run.sh [mock|test|live|eval|eval-compose|openers ...]"; exit 2 ;;
esac
