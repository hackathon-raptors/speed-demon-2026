#!/usr/bin/env bash
# Cold-recovery: take a freshly-provisioned Hetzner EX44 (Intel i5-13500, Debian 13) to a working
# Speed Demon scoring box in one run. Run as root. Idempotent (safe to re-run). See harness/RECOVERY.md.
#
# It automates everything scriptable (harden, setup, docker pinning, calibrate, builder, units) and
# STOPS with instructions for the parts that cannot be scripted: the three secrets and `docker login`.
set -euo pipefail

REPO_URL="${SD_REPO_URL:-https://github.com/hackathon-raptors/speed-demon-2026.git}"
REPO_REF="${SD_REPO_REF:-main}"
CODE="${SD_CODE:-/opt/speed-demon-2026}"
HISTORY_BUCKET="${SD_HISTORY_BUCKET:-s3://raptors-speeddemon-board-history/2026}"

say() { echo; echo "== $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"

# Never re-bootstrap the LIVE box: re-calibration would stomp the running anchor.
if systemctl is-active --quiet speeddemon-board 2>/dev/null && [ "${1:-}" != "--force" ]; then
  die "speeddemon-board is ACTIVE — this looks like the live box. Stop it first, or pass --force."
fi

say "0. secrets the operator must place first (cannot be scripted)"
# github-app.pem + the S3 key are mandatory; board.env only matters for the Depot build backend.
missing=0
for f in /etc/speeddemon/github-app.pem /root/.aws/credentials; do
  [ -s "$f" ] || { echo "  MISSING: $f"; missing=1; }
done
[ -s /etc/speeddemon/board.env ] || echo "  note: /etc/speeddemon/board.env absent (only needed for the Depot backend)"
[ -s /etc/speeddemon/board.json ] || { echo "  MISSING: /etc/speeddemon/board.json (the prod config)"; missing=1; }
[ "$missing" = 0 ] || die "place the files above (see RECOVERY.md), then re-run"
chmod 700 /etc/speeddemon
chmod 600 /etc/speeddemon/github-app.pem /root/.aws/* 2>/dev/null || true
[ -f /etc/speeddemon/board.env ] && chmod 600 /etc/speeddemon/board.env

say "1. code -> $CODE (ref $REPO_REF)"
if [ -d "$CODE/.git" ]; then
  git -C "$CODE" fetch --quiet --all && git -C "$CODE" checkout --quiet "$REPO_REF" && git -C "$CODE" pull --quiet --ff-only || true
elif [ -d "$CODE" ]; then
  echo "  $CODE already present (non-git copy); leaving it as is"
else
  git clone --quiet --branch "$REPO_REF" "$REPO_URL" "$CODE"
fi

say "2. harden (sshd, firewall, no background jobs, security updates)"
bash "$CODE/harness/harden-box.sh"
echo "  firewall armed with a 180 s rollback at $(date -u +%H:%M:%SZ) UTC. Keep it by cancelling from a NEW ssh session:"
echo "      systemctl stop sd-fw-rollback.timer"
if [ -t 0 ]; then
  read -r -p "  Do that now, then press Enter (bootstrap runs ~30 min past here; the rollback would flush mid-run). "
else
  echo "  NON-INTERACTIVE: cancel the rollback NOW from another session, or the firewall opens in 180 s."
fi

say "3. setup (docker cgroupfs, bridge:none, clocks, /srv/speeddemon/work tmpfs via fstab, sd-timer, allowlist)"
bash "$CODE/harness/setup-box.sh"

say "4. keep dockerd/containerd shims off the P-cores (README step 7)"
# Pulled allowlist images survive this restart; nothing is running on a fresh box.
for svc in docker containerd; do
  mkdir -p "/etc/systemd/system/$svc.service.d"
  printf '[Service]\nAllowedCPUs=12-19\n' > "/etc/systemd/system/$svc.service.d/10-speeddemon-cpus.conf"
done
systemctl daemon-reload
systemctl restart containerd 2>/dev/null || true
systemctl restart docker

say "5. registry present?"
reg="$(python3 -c 'import json;print(json.load(open("/etc/speeddemon/board.json"))["registry"])')"
mkdir -p "$(dirname "$reg")"
[ -s "$reg" ] || echo "  note: registry $reg absent — restore the Tally export before starting the board (it is not in the mirror)"

say "6. docker login (base-image pull limits)"
if docker info 2>/dev/null | grep -q Username; then
  echo "  already logged in"
elif [ -t 0 ]; then
  echo "  NOT logged in. Run 'docker login' as root in another session, then press Enter (prewarm pulls bases)."
  read -r _
else
  die "docker login required: log in first, then re-run (or run bootstrap interactively)"
fi

say "7. calibrate the reference on THIS box (seed 1 = the live-board public dataset)"
# Final scoring calibrates its OWN holdout seed via final_scoring.py; this seed-1 calibration is the live board's anchor.
cal="$(python3 -c 'import json;print(json.load(open("/etc/speeddemon/board.json"))["calibration"])')"
if [ -s "$cal" ] && [ "${SD_RECALIBRATE:-}" != "1" ]; then
  echo "  $cal present; set SD_RECALIBRATE=1 to re-measure. A REPLACEMENT box has different timing and"
  echo "  MUST re-calibrate (see RECOVERY.md); a reinstall of the same box may reuse it."
else
  python3 "$CODE/harness/calibrate.py" --seed 1 --out "$cal"
fi

say "8. builder (docker-container, pids-capped 1024, bases prewarmed)"
python3 "$CODE/board/board.py" setup-builder --config /etc/speeddemon/board.json --prewarm

say "9. restore off-box history from the A2 private mirror (best-effort)"
sd="$(python3 -c 'import json;print(json.load(open("/etc/speeddemon/board.json"))["state_dir"])')"
mkdir -p "$sd"
for f in history.jsonl watch.json; do
  if [ -s "$sd/$f" ] && [ "${SD_RESTORE_HISTORY:-}" != "1" ]; then
    echo "  local $sd/$f present; NOT overwriting (set SD_RESTORE_HISTORY=1 to pull the mirror copy)"
  elif aws s3 cp "$HISTORY_BUCKET/$f" "$sd/$f" --only-show-errors 2>/dev/null; then
    echo "  restored $f from the mirror"
  else
    echo "  no $f on disk or in the mirror (fine: the board rebuilds its 15-min state from history, or starts fresh)"
  fi
done

say "10. install units (enable, do NOT start — you start at the right moment)"
install -m 0644 "$CODE/board/speeddemon-board.service" "$CODE/board/speeddemon-board-publish.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable speeddemon-board >/dev/null

say "bootstrap complete. Before starting:"
echo "  - confirm clocks:  cat /sys/devices/system/cpu/intel_pstate/no_turbo (1), .../cpu0/cpufreq/scaling_governor (performance)"
echo "  - confirm builder: docker inspect buildx_buildkit_speeddemon-builder0 --format '{{.HostConfig.PidsLimit}}' (1024)"
echo "  - baseline canary: python3 $CODE/board/board.py reference-check --config /etc/speeddemon/board.json"
echo "  LIVE board:  systemctl start speeddemon-board   (publisher follows)"
echo "  FINAL score: python3 $CODE/harness/final_scoring.py ...  (board stopped; see FINAL_SCORING.md)"
