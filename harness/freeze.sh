#!/bin/bash
# Speed Demon 2026 code freeze, run once at 2026-11-02 15:00:00 UTC by a transient systemd timer:
#   systemd-run --unit sd-freeze --on-calendar "2026-11-02 15:00:00 UTC" /opt/speed-demon-2026/harness/freeze.sh
# 1. registry fixed (tally-sync stopped, frozen copy)  2. snapshot of every team's tip -> freeze.json
# 3. board evidence copied  4. wait for the board queue to drain (board.py stops polling at event_ends)
# 5. board stopped. FREEZE_DRY=1: snapshot into a temp dir, stop nothing, skip the drain.
set -u
OPT=/opt/speed-demon-2026
BOARD=/srv/speeddemon/board
OUT=${FREEZE_OUT:-/srv/speeddemon/final}
DRY=${FREEZE_DRY:-0}
[ "$DRY" = 1 ] && OUT=$(mktemp -d /tmp/sd-freeze-dry.XXXX)
mkdir -p "$OUT"
LOG="$OUT/freeze.log"
say() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }

say "freeze start (dry=$DRY, out=$OUT)"
if [ "$DRY" != 1 ]; then
  systemctl stop speeddemon-tally-sync.timer speeddemon-tally-sync.service && say "tally-sync stopped"
fi
cp "$BOARD/registry.json" "$OUT/registry.frozen.json"
say "registry frozen: $(python3 -c "import json;print(len(json.load(open('$OUT/registry.frozen.json'))))") teams"

python3 "$OPT/harness/final_scoring.py" snapshot \
  --registry "$OUT/registry.frozen.json" \
  --github-app-id 5456832 --github-app-key /etc/speeddemon/github-app.pem \
  --out "$OUT/freeze.json" >>"$LOG" 2>&1
rc=$?
say "snapshot exit $rc ($( [ -f "$OUT/freeze.json" ] && wc -c <"$OUT/freeze.json" || echo 0) bytes)"

for f in state/history.jsonl state/watch.json public/board.json; do
  [ -f "$BOARD/$f" ] && cp "$BOARD/$f" "$OUT/$(basename "$f" .json | sed 's/\.jsonl$//').at-freeze.${f##*.}"
done
say "evidence copied"
[ "$DRY" = 1 ] && { say "dry run done"; exit 0; }

# board.py takes freeze.json in one final poll (within a poll interval), then closes its watch.
for i in $(seq 1 30); do
  journalctl -u speeddemon-board --since "$(date -u -d '-10 min' '+%F %T') UTC" --no-pager 2>/dev/null \
    | grep -q "final poll" && { say "board took the frozen pins, watch closed"; break; }
  sleep 10
done
sleep 10                                   # let the feed show what the final poll queued
# Drain: queued pre-freeze pushes still run.
# The 15-minute per-team rule can hold a 14:59 push until ~15:14, so cap at 45 min.
deadline=$(( $(date +%s) + 2700 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  busy=$(python3 - "$BOARD/public/board.json" <<'EOF'
import json, sys
try:
    b = json.load(open(sys.argv[1]))
except Exception:
    print(-1); sys.exit()
print(sum(1 for t in b.get("teams", []) if (t.get("queue") or {}).get("in_flight") or (t.get("queue") or {}).get("queued")))
EOF
)
  say "drain: $busy team(s) still queued or running"
  [ "$busy" = 0 ] && break
  sleep 30
done
cp "$BOARD/state/history.jsonl" "$OUT/history.after-drain.jsonl" 2>/dev/null
sleep 15                                   # let the publisher push the last board.json
systemctl disable --now speeddemon-board && say "board stopped and disabled"
say "freeze done"
