#!/usr/bin/env bash
# Serve this folder on http://localhost:8000 and open the demo.
#
# Opening demo_testing.html straight from disk does not work: Chrome sends no
# Origin header from a file:// page, so CloudFront never returns the CORS header
# hls.js needs and video playback fails. Any http:// origin fixes it.
set -euo pipefail

cd "$(dirname "$0")"
PORT="${1:-8000}"
URL="http://localhost:$PORT/demo_testing.html"

echo "serving on $URL"
command -v open >/dev/null && (sleep 1 && open "$URL") &
exec python3 -m http.server "$PORT"
