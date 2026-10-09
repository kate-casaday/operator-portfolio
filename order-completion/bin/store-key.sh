#!/bin/zsh
# One-time: store the Chief of Health Demo API key in the macOS login Keychain.
# Prompts for the key; nothing is echoed, nothing is written to a file or shell history.
set -e
SERVICE="chief-of-health-demo-anthropic"
security delete-generic-password -a "$USER" -s "$SERVICE" >/dev/null 2>&1 || true
echo "Paste the API key from the Chief of Health Demo workspace, then press Return (it will not be shown):"
security add-generic-password -a "$USER" -s "$SERVICE" -w
echo "Stored in Keychain as '$SERVICE'.  Run bin/run.sh to use it."
