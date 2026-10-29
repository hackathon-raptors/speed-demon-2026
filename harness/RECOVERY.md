# Cold recovery — the scoring box is lost

If the Hetzner box dies or becomes unreachable during the event, this brings a fresh one up to a working
scoring box in ~45 minutes. The automation is `harness/bootstrap-box.sh`; this is the procedure around it.

## Read this first: re-calibration changes the reference

The box's reference times (`calibration.json`: `T_ref`, `M_ref`) are **specific to that physical machine**.
A replacement is a different i5-13500, so it will measure slightly different numbers. That has consequences:

- **Live board (Oct 30 – Nov 2):** board runs are for iteration and do **not** count, so re-calibrate on the
  new box and carry on. If the new `T_ref` moved a published figure (the gist states the 10-min/2 GB caps,
  six P-cores, turbo off — not T_ref itself, so usually nothing moves), `gh gist edit 7cc5c01d2841b0cccadd1fcb71e0cec9`.
- **Final scoring (Nov 3 – Nov 12):** everything runs on one box in one session against one holdout anchor.
  If the box dies mid-final-scoring, **re-run `final_scoring.py score` from scratch on the new box** — it
  re-calibrates the holdout reference and re-scores every team against that new anchor, which is internally
  consistent. Never splice team results measured on two different boxes into one final result.

So: the A2 mirror restores the **history** (the record of what ran), never the calibration. Calibration is
always re-measured on the box you are actually scoring on.

## What you need in hand

1. **A fresh box:** Hetzner EX44, Intel i5-13500, Debian 13, in FSN1, root SSH. Confirm P-cores are CPUs
   0-11 (`cat /sys/devices/cpu_core/cpus`); `setup-box.sh` refuses otherwise.
2. **Three secrets** (not in the repo, not in any mirror — keep them in your password manager):
   - `/etc/speeddemon/github-app.pem` (0600) — the Speed Demon Board GitHub App private key. Your copy:
     `~/.config/speeddemon/github-app.pem`.
   - `/root/.aws/credentials` (+ `config`) (0600) — the `board-put` S3 key (publisher + history mirror).
   - `/etc/speeddemon/board.env` (0600) — `DEPOT_TOKEN` + `DEPOT_PROJECT_ID`. Only needed if the builder
     backend is Depot, which the live board uses (`builder.backend: "depot"`), so place it; without it every build falls back to the on-box builder.
3. **The prod config** `/etc/speeddemon/board.json` (0600) and the **registry** it points at
   (`/srv/speeddemon/board/registry.json`, the Tally export). Keep a copy of both off-box; the registry is
   **not** in the mirror.

## Procedure

```
# 1. Provision the box, get root SSH, then place the secrets + config:
ssh root@NEWBOX 'mkdir -p /etc/speeddemon /root/.aws /srv/speeddemon/board'
scp ~/.config/speeddemon/github-app.pem  root@NEWBOX:/etc/speeddemon/github-app.pem
scp board.json                           root@NEWBOX:/etc/speeddemon/board.json         # your off-box copy of the prod config
scp registry.json                        root@NEWBOX:/srv/speeddemon/board/registry.json # the Tally export
scp ~/.aws/credentials ~/.aws/config     root@NEWBOX:/root/.aws/                          # the board-put key minted Oct 25
# (board.env with DEPOT_TOKEN only if the builder backend is Depot)

# 2. Get the code + run the bootstrap (as root on the box):
git clone https://github.com/hackathon-raptors/speed-demon-2026.git /opt/speed-demon-2026   # or let bootstrap do it
bash /opt/speed-demon-2026/harness/bootstrap-box.sh

# 3. KEEP THE FIREWALL: from a SECOND ssh session, within 180 s of the harden step:
systemctl stop sd-fw-rollback.timer
```

`bootstrap-box.sh` then, in order: checks the secrets, pulls the code, hardens, runs `setup-box.sh`
(docker/cgroupfs, clocks, the `/srv/speeddemon/work` tmpfs via fstab, the timer, the allowlist pull),
pins dockerd/containerd to the E-cores, prompts for `docker login`, **calibrates** (~20 min, seed 1),
builds the pids-capped builder and prewarms the bases (~10 min), restores `history.jsonl`/`watch.json`
from the A2 private bucket (`raptors-speeddemon-board-history`), and installs + enables (not starts) the
units. Re-run it safely if a step fails; set `SD_RECALIBRATE=1` to force a fresh calibration.

## Verify, then start

```
cat /sys/devices/system/cpu/intel_pstate/no_turbo                      # 1
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor              # performance
docker inspect speeddemon-builder-buildkitd --format '{{.HostConfig.Privileged}} {{.HostConfig.PidsLimit}}'   # false 1024
findmnt /srv/speeddemon/work                                           # tmpfs, size=8g
python3 /opt/speed-demon-2026/board/board.py reference-check --config /etc/speeddemon/board.json   # ok, drift <2%
```

- **Live board:** `systemctl start speeddemon-board` (the publisher follows via `Wants=`/`BindsTo=`).
  Watch: `journalctl -u speeddemon-board -u speeddemon-board-publish -f`.
- **Final scoring:** leave the board stopped and follow `FINAL_SCORING.md`.

DNS and the S3 buckets are unaffected by a box swap — the box pushes `board.json` out to the same S3 prefix,
nothing points inbound at the box, so there is no DNS or Cloudflare change to make.

## Time budget (~45 min)

harden + setup ~5, docker login + prewarm ~10, calibration ~20 (data gen + 5 runs × 4 tracks), builder +
units + restore ~5, verification ~5. Calibration dominates; it cannot be skipped on a replacement box.
