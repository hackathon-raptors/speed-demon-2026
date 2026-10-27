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
#   publish.sh <feed-path> <s3-prefix> <interval-s>   (defaults: prod board.json, board, 5)
set -u
umask 077                                   # any file we create (the err log) is owner-only
feed="${1:-/srv/speeddemon/board/public/board.json}"
prefix="${2:-board}"
iv="${3:-5}"
errlog="${4:-/srv/speeddemon/board/state/publish.err}"   # NOT world-readable /tmp: aws errors can echo signed headers
mkdir -p "$(dirname "$errlog")" 2>/dev/null || true
while true; do
  [ -f "$feed" ] && aws s3 cp "$feed" "s3://raptors.dev/speeddemon.dev/2026/$prefix/board.json" \
    --cache-control max-age=5 --content-type application/json --only-show-errors 2>>"$errlog"
  sleep "$iv"
done
