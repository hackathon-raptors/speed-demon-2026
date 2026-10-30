#!/bin/bash
# Speed Demon 2026 board publisher. The hardened box takes no inbound HTTP, and the board's
# mirror_command fires only on a completed run (record()), so queue transitions (building,
# running, oldest_wait_s) would go stale between records. This pushes board.json to S3 every
# few seconds so the public /board stays live.
#
# board.json ONLY: history.jsonl holds teams' log tails and must never reach the public
# raptors.dev bucket (it is public-read). History goes to a private store (A2), not here.
#
# Run by speeddemon-board-publish.service, coupled to the board. Args:
#   publish.sh <feed-path> <s3-prefix> <interval-s> [err-log] [security-summary] [private-dest|none]
#   (defaults: prod board.json, board, 5, <state>/publish.err, <state>/security_summary.json, the
#   private history bucket)
set -u
umask 077                                   # any file we create (the err log) is owner-only
feed="${1:-/srv/speeddemon/board/public/board.json}"
prefix="${2:-board}"
iv="${3:-5}"
errlog="${4:-/srv/speeddemon/board/state/publish.err}"   # NOT world-readable /tmp: aws errors can echo signed headers
mkdir -p "$(dirname "$errlog")" 2>/dev/null || true
# The security summary (state dir, private) goes to the PRIVATE history bucket only, and only
# when it changed: the off-box monitor's private view reads it there.
sec="${5:-$(dirname "$(dirname "$feed")")/state/security_summary.json}"
sec_dest="${6:-s3://raptors-speeddemon-board-history/2026/security_summary.json}"
sec_seen=""
health="$(dirname "$feed")/health.json"
while true; do
  [ -f "$feed" ] && aws s3 cp "$feed" "s3://raptors.dev/speeddemon.dev/2026/$prefix/board.json" \
    --cache-control max-age=5 --content-type application/json --only-show-errors 2>>"$errlog"
  # Box health (health.py, machine numbers only) for the /board panel, when it is being written.
  [ -f "$health" ] && aws s3 cp "$health" "s3://raptors.dev/speeddemon.dev/2026/$prefix/health.json" \
    --cache-control max-age=5 --content-type application/json --only-show-errors 2>>"$errlog"
  if [ -f "$sec" ] && [ "$sec_dest" != "none" ]; then
    m=$(stat -c %Y "$sec" 2>/dev/null || stat -f %m "$sec")
    if [ "$m" != "$sec_seen" ] && aws s3 cp "$sec" "$sec_dest" --content-type application/json \
        --only-show-errors 2>>"$errlog"; then
      sec_seen="$m"
    fi
  fi
  sleep "$iv"
done
