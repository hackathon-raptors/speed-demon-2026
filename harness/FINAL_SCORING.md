# Final scoring (B1) — the post-freeze runbook

Final scoring runs **after code freeze (Nov 2 15:00 UTC)**, on the box **to itself**, on a **private
holdout seed**. It scores every team's **frozen** commit, re-runs the reference before and after every
batch, and throws out + rescores any batch the box drifted through (the site's promise). It emits the
**machine side** only — judge scores (0.35 weight) and the blended `track_total`/`overall` are combined
in a separate step after judging.

`harness/final_scoring.py` has two subcommands: `snapshot` (pin the commits, at freeze) and `score`.

## 1. At freeze (Nov 2 15:00): pin the frozen commits

Run this **at** the freeze so a later push cannot change what is scored. It only does `ls-remote`, so the
board can still be up.

```
python3 /opt/speed-demon-2026/harness/final_scoring.py snapshot \
    --registry /srv/speeddemon/board/registry.json \
    --github-app-id 5456832 --github-app-key /etc/speeddemon/github-app.pem \
    --out /srv/speeddemon/final/freeze.json
```

It prints one line per team and exits non-zero if any team has no `speed-demon` tip (chase those before
scoring). `freeze.json` is the record of exactly what gets scored.

## 2. Stop the board, give the box to itself

```
systemctl disable --now speeddemon-board          # kills the in-flight run; the publisher follows
systemctl stop sd-rehearsal sd-publish            # if the rehearsal is still up
```

Confirm the determinism regime is intact (B1 trusts it):

```
cat /sys/devices/system/cpu/intel_pstate/no_turbo        # 1
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor # performance
```

## 3. Pick a private holdout seed

Choose a random integer and **keep it private** until results are published. Nothing in the generators
makes a seed special, so any seed works. (Published with the results on Nov 13, per the gist.)

## 4. Score

```
python3 /opt/speed-demon-2026/harness/final_scoring.py score \
    --registry /srv/speeddemon/board/registry.json \
    --github-app-id 5456832 --github-app-key /etc/speeddemon/github-app.pem \
    --freeze /srv/speeddemon/final/freeze.json \
    --holdout-seed <PRIVATE_SEED> \
    --out /srv/speeddemon/final/final_scores.json
```

What it does, in order:
1. **Holdout calibration** — reuses `calibrate.py` to generate the full holdout data per track and run
   the reference 5× (this is the K anchor **and** the output-hash oracle). Generating ~790 MB + the
   reference runs take a while; that is expected. Reuse a prior calibration with `--calibration <file>`.
2. **S prerequisite** — ensures the `speeddemon/static-tools:2026-10-28` image exists (builds it if not).
   S (source size) is **measured**, never stubbed; a track whose S cannot be measured is flagged
   (`stage: "axes"`), not silently scored.
3. **Per track, in batches of `--batch-size` (default 10):** clone each frozen commit (pinned, not the
   tip), check + build + run 5× on the holdout (capped at `--timeout-factor`×T_ref, default 3× = the
   board's published cap), measure S/D, score K + points against the holdout anchor. After each batch,
   re-run the reference; if it drifted >2% from the batch's "before", discard the batch and **rescore up
   to `--rescore-limit` (2) times**.

Useful flags: `--tracks a` (score/re-score one track), `--batch-size`, `--calibration <file>` (skip
re-generating the holdout data on a re-run).

## 5. Read the output

`final_scores.json`:
- `holdout_reference[track]` — the anchor: `T_ref_s`, `M_ref_bytes`, `S_ref`, `D_ref`, `output`,
  `expected_sha256`.
- `tracks[track].drift_log` — every batch's before/after reference T, the drift %, and `ok`.
- `teams[slug].tracks[track]` — `{commit, stage, ok, gates, T_s, M_bytes, S, D, K, points, walls_s
  (raw per-run), batch, drift_ok, failure}`. `stage` is `scored` / `unchanged` (untouched starter, ~0) /
  `check` / `build` / `run` (a gate failed) / `axes` (S unmeasurable, manual review) / `clone` / `absent`.

`points` here is the **machine** points (0–100). Blend with judge scores afterwards:
`scoring.track_total(points, judge)` = 0.65·points + 0.35·judge; `scoring.overall(track_totals)` is the
geometric mean of a team's best three track totals.

## 6. If it HALTS (exit code 2)

A batch still drifted >2% after the rescores — the box's timing is genuinely moving. `final_scores.json`
holds the partial results and a `halted` marker naming the track and batch. **Do not ship those numbers.**
Check the clocks (governor reverted? turbo back on? thermal throttling? a stray process on the P-cores?),
fix it, then re-run `score` (reuse `--calibration` so it does not regenerate the holdout data). Exit 0 =
clean, 1 = a usage/input error, 2 = halted on drift.
