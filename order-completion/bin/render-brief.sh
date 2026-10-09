#!/bin/bash
# Render docs/technical-brief.md to a dated PDF (pandoc → HTML → headless Chrome).
# Usage: bin/render-brief.sh [YYYY-MM-DD]   (defaults to today)
set -euo pipefail
cd "$(dirname "$0")/.."
DATE="${1:-$(date +%F)}"
TMP="$(mktemp -d)"
pandoc docs/technical-brief.md -f gfm -t html5 -s --metadata title="StealthCo technical brief" -c "$PWD/docs/brief.css" -o "$TMP/brief.html"
python3 - "$TMP/brief.html" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p, encoding="utf-8").read()
s = re.sub(r'<header id="title-block-header">.*?</header>', '', s, flags=re.S)
s = s.replace('</head>', '<style>pre, pre code { white-space: pre !important; }</style></head>')
open(p, "w", encoding="utf-8").write(s)
PY
OUT="docs/StealthCo-technical-brief-$DATE.pdf"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf="$OUT" "file://$TMP/brief.html" 2>/dev/null
echo "$OUT"
