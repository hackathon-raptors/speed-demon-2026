# The live board

From kickoff (Oct 30 15:00 UTC) to code freeze (Nov 2 15:00 UTC), the board runs every team's latest push once on the full public dataset and publishes the result. It's for iteration and for the judges' run-history check; final scoring is separate (five runs on the hidden holdout seed, with the box to itself).

## What it does

1. **Watch.** It reads the registry (the organiser's Tally export, `[{team, repo, tracks, discord}]`) and polls each repo's `speed-demon` branch with `git ls-remote` every 60 s. It reads private repos through the Speed Demon Board GitHub App (`board/github_app.py`): teams install it on their repo, and for each repo the board gets an hour-long token limited to that one repository and read-only contents. The token reaches git only as an HTTP header in git's environment config: never in argv, a URL or `.git/config`, and it is redacted from any git error.
2. **Queue.** A new commit queues one job for the team, and a newer push replaces a queued one. A team has at most one job in flight and gets at most one dispatch per 15 minutes. Among the teams that may start, the one served least recently goes first, and a team never served goes before all others. Push time is when the board first saw the commit, not the commit date.
3. **Check and build.** The job takes a shallow clone of the branch tip. If the tip moved since `ls-remote`, the run records the commit actually cloned. For each `track-a` to `track-d` directory, it runs `harness/dockerfile_check.py` against `harness/allowlist.json`, then builds it with `--network=none`. The live config builds on Depot (`builder.backend: "depot"`; the code default is `local`): `depot build --network none --load` on an isolated off-box machine, so untrusted builds never run on the scoring box. If Depot cannot start the build at all (the CLI is missing, a bad token, no machine), it falls back to `docker buildx build --network=none --load` on the board's own BuildKit builder. A track whose files ask for the build host's CPU (`-march=native`, `-mcpu=native`, `target-cpu=native`, `IlcInstructionSet` native) skips Depot and builds on the box, because Depot's CPU has AVX-512 and the box's does not. If a Depot-built run still dies with SIGILL, the board records it, marks that team's track to build on the box from then on (`box_tracks` in `watch.json`), and requeues the same commit at once, without charging the 15-minute interval. The box builder is:
   - a docker-container builder pinned to the E-cores (`cpuset 12-19`, 16 GB);
   - a hard 10-minute timeout, after which the build is cancelled inside BuildKit;
   - a 2 GB image cap.

   At most two builds run at a time. Symlinked track directories and Dockerfiles are refused, so a check can never read a file outside the repo.

   A track directory that is the starter as shipped is not built or run. That means its Dockerfile and every other file, apart from Markdown and `.speed-demon.toml`, are byte-identical copies of files in `reference/` or `generators/`. It is the reference itself and would score about 0, so the board records it at stage `unchanged` and spends no runner time on it. A single changed byte in any code file, or a new file, sends the track through the normal build and run. Set `skip_unchanged_reference` to false to turn this off.
4. **Run.** Each built track gets one run under `harness/run_submission.py`'s contract: `run_once` itself, so the limits, timer, cgroup `memory.peak`, `/out` check and `docker diff` sandbox check are identical. It runs on `cpuset 0-11`, capped at 3x the reference's T. Runs are strictly one at a time.
5. **Record.** Every attempt, including check failures, appends one fsynced line to `history.jsonl`. Each line holds run_id, team, track, commit, push time, dispatch, build and run times, the gates, T, M, S, D, K, points and a short failure reason. A failed build's record keeps the last 40 lines of that team's own build output, and a failed run's the last 20 lines of its stderr; nothing else is ever kept. BuildKit's own build history is switched off in `buildkitd.toml`.
6. **Publish.** `board.json` is replaced atomically after every record and every 10 s. Per team and track it holds the best and latest run, the queue position, and how long the team's oldest unrun push has waited. Discord handles and host paths never appear in it. An optional `mirror_command` runs after every history append, to copy the history and the feed off the box.

K and points come from `harness/scoring.py`. S and D come from `harness/static_axes.py`. If that measurement fails for a run, `axes.py` sets them to the reference's values so K stays computable, and the record says `"static_axes": "stubbed"`.

## Files

| File | What it is |
|---|---|
| `board.py` | The daemon and the CLI: `serve`, `once`, `setup-builder`, `feed`, `reference-check` |
| `fairqueue.py` | The queue rules: one queued commit per team, the 15-minute interval, least-recently-served first |
| `sources.py` | Registry parsing, GitHub URL normalisation, git with the bot token |
| `steps.py` | Check, build on the builder, the single run, gates, K, image pruning |
| `records.py` | The append-only history, the mirror hook, `board.json` |
| `guards.py` | The disk watchdog |
| `axes.py` | S and D (stubbed until `harness/static_axes.py` exists) |
| `buildkitd.toml` | The builder's gc policy and history settings |
| `board.example.json` | The box config, with reference numbers to fill in from calibration |
| `speeddemon-board.service` | The board systemd unit (Type=notify + WatchdogSec) |
| `publish.sh`, `speeddemon-board-publish.service` | Pushes board.json to S3 every 5 s; coupled to the board |

Tests are in `tests/test_board.py`. The Docker ones build and run the Track A reference from a bare git repo, and skip without Docker.

## Run it locally (Colima)

```
python3.13 -m unittest tests.test_board -v          # Docker tests make and remove a builder "sdboard-test"
python3.13 board/board.py once --config <local.json>  # poll once, run what may start, write board.json, exit
python3.13 board/board.py once --config <local.json> --wait-pending --set min_interval_s=60
```

A local config needs these values:

- `"cgroups": "colima"` and the timer `harness/timer/sd-timer-arm64`;
- `"run_cpuset"` and `"builder.cpuset"` inside the VM's CPUs (for example `0-1` and `2-3`);
- `"builder.memory"` under the VM's memory;
- `"allow_local_repos": true`, so the registry can name bare repos;
- `"disk.min_free_gb"` under what the VM has free.

Keep data and work directories under your home directory, where Colima can see them. `--set key=JSON` overrides any config value for one invocation.

Don't run the Docker tests on the box while the board serves. They bootstrap their own builder, and their run containers share the `sd-run-` name prefix that the board kills when it stops.

## Running it on the box

The box is the Hetzner i5-13500 in FSN1. P-cores are CPUs 0-11 and E-cores 12-19, and `setup-box.sh` checks this.

1. **Prepare the box:** `harness/harden-box.sh`, then `harness/setup-box.sh`. They set up cgroupfs on cgroup v2, `"bridge": "none"`, container logs capped at 10 MB, `/srv/speeddemon/work` as an 8 GB tmpfs persisted in `/etc/fstab` (`nofail`), `/srv/speeddemon/bin/sd-timer`, and the pre-pulled allowlist.
2. **Code:** put this repo at `/opt/speed-demon-2026` (the systemd units reference that path).
3. **Data and calibration:** `python3 harness/calibrate.py --seed 1 --out /srv/speeddemon/calibration.json`. It generates the full seed-1 data into `/srv/speeddemon/data/track-<x>/full-seed1`, the kickoff dataset the board runs on, and runs the reference five times per track through `run_submission.py`.
4. **Reference numbers:** the config's `"calibration"` key points at that file. The board takes each track's `output`, `expected_sha256`, `ref.T_s` and `ref.M_bytes` from it, and a value written in the config wins.
   - A track whose calibration failed a gate, or whose five runs disagreed on the output hash, is not filled, and the board refuses to start until it is fixed.
   - `ref.S` is the reference's code-line count from `static_axes`. While S is stubbed, any positive number works, because the factor is 1.
   - `ref.D` is 1.
5. **Config:**
   - `/etc/speeddemon/board.json`, from `board.example.json`;
   - `/etc/speeddemon/github-app.pem` (mode 0600), the Speed Demon Board app's private key, with `github_app_id` set in the config (5456832);
   - the registry at `/srv/speeddemon/board/registry.json`.

   The board re-reads the registry whenever the file changes, so late registrations need no restart.
6. **Builder:** run `python3 board/board.py setup-builder --config /etc/speeddemon/board.json --prewarm`.
   - It creates `speeddemon-builder` (docker-container driver, `moby/buildkit:v0.34.1`, cpuset 12-19, 16 GB, host network for base-image pulls only) with `buildkitd.toml`, and caps the builder container's pids at 1024 (`docker update --pids-limit`) so an untrusted build `RUN` cannot fork-bomb the host. The cap persists in HostConfig and is re-asserted on every `setup-builder`.
   - It then pulls every allowlisted base into the builder. The builder has its own image store, so `setup-box.sh`'s pulls into dockerd don't reach it.
   - Run `docker login` as root first, so these pulls don't hit Docker Hub's anonymous limits.
   - To change any builder setting, run `docker buildx rm speeddemon-builder` and then run setup-builder again.
   - Final scoring builds with dockerd's own BuildKit (`docker build`), so set `builder.image` to the version `docker buildx inspect default` reports. Board builds and final-scoring builds then share one Dockerfile frontend.
7. **Keep the daemons off the P-cores (recommended):** add an `AllowedCPUs=12-19` drop-in to `docker.service` and `containerd.service` (`systemctl edit docker`). Image loads and container shims then stay on the E-cores. Run containers are unaffected, because they live in their own `/speeddemon/<run>` cgroups. Check it once with `docker run --rm --cpuset-cpus=0-11 <image> grep Cpus_allowed_list /proc/self/status`, which must print `0-11`.
8. **Start:** install both units and enable only the board (the publisher follows it via `Wants=`/`BindsTo=`):
   ```
   install -m 0644 board/speeddemon-board.service board/speeddemon-board-publish.service /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now speeddemon-board
   ```
   Watch with `journalctl -u speeddemon-board -u speeddemon-board-publish -f`. The board runs `Type=notify` with `WatchdogSec=120`, pinging systemd each cycle while its workers are alive, so a hang or a dead worker restarts it.
9. **Publish:** the hardened box takes no inbound HTTP, so two paths push state out.
   - **`board.json`** → the public `board/` prefix every ~5 s, by `speeddemon-board-publish.service` (`publish.sh`). A separate loop is needed because `mirror_command` fires only on a completed run, so queue transitions (building, running, `oldest_wait_s`) would go stale between records.
   - **`history.jsonl`** (+ `watch.json`) hold teams' own build-log and stderr tails and must NOT reach the public `raptors.dev/*` bucket. They mirror to the PRIVATE bucket `raptors-speeddemon-board-history` (public access blocked, versioned) via `mirror_command`, which runs on every record in the config file's directory with `$SD_HISTORY`, `$SD_FEED` and `$SD_WATCH` set. Never point it at the public board prefix.
10. **Stop at code freeze:** `systemctl disable --now speeddemon-board`. The board kills its builds and the run in flight. A job stopped mid-flight is requeued once on the next start and not recorded until it completes.

### Survival guards

- **Images:** after each run, every other tag of that team-track's image is removed, so the box holds one image per team and track (`sdboard/<team>-<track>:<commit12>`).
- **BuildKit cache:** gc runs inside the builder (`buildkitd.toml`):
  - the whole cache is capped at 100 GB;
  - gc trims harder when the disk has under 150 GB free;
  - team build contexts go after a day;
  - anything unused for three days goes while the cache is above 60 GB.

  dockerd's own builder, used by final scoring, has the separate 100 GB gc from `setup-box.sh`.
- **Disk watchdog:** every 5 s it checks free space on `/var/lib/docker` and `/srv/speeddemon`.
  - Under 100 GB, it kills every running build and clone and pauses new ones. The killed builds are recorded with a "disk guard" reason, and the builder's cache is pruned back under its cap.
  - Builds resume at 110 GB. Runs continue throughout, because they write only to the tmpfs.
- **Timeouts:** builds get 10 minutes, after which they are cancelled in BuildKit, with no step left running. `git clone` gets 5 minutes and `ls-remote` 30 s.
- **Restarts:** the queue, the last-served times and the job in flight are saved in `state/watch.json` on every change. If that file is lost, the 15-minute state is rebuilt from the history.
- **The board's own memory:** `run_submission.run_once` buffers a run's whole stderr, and a run that floods stderr could grow the board. The unit caps the board at 8 GB (`MemoryMax`), and the interrupted job is retried once, then recorded as interrupted.
- **Reference canary (`reference_check`):** every `interval_s` (3 h) the runner re-runs the reference on one configured track (`tracks`, rotated one per cycle; default `["a"]`) through `run_submission.py` with `runs` set to 5, the same middle-3-mean estimator as calibration, and compares the measured T to `calibration.json`. It runs **only through the runner**, so it never overlaps a team run (determinism holds); it yields while a build is in flight or the disk guard is up. It is **alert-only**: it never changes any team's `T_ref`, timeout cap or score. Full detail (measured T, drift %, per-run walls) is appended to `state/reference_checks.jsonl` and logged (a WARNING over `drift_threshold`, default 2%). `board.json` carries only a numbers-free health view, `reference_check: {ok, checked_at, track, note, stale_after_s}`, for the off-box alerter. The site's drift *promise* is a final-scoring mechanism (reference before/after each scoring batch, discard+rescore on >2%); this canary is box-health early warning for the live board, not that promise. Run `board.py reference-check --config ...` to fire one cycle by hand (P-core work: only when the board is idle, or it contends with a live run).

### Status (Oct 25)

- Calibration done Oct 24: `/srv/speeddemon/calibration.json`, seed 1, five runs per track.
- The ~60-team load test ran Oct 24 — throughput, sandbox containment and chaos all passed. Watch `queue.oldest_wait_s` in `board.json` during the event.
- `harness/static_axes.py` computes S and D (see `axes.py`); if any record still shows `"static_axes": "stubbed"`, confirm the board wiring.
