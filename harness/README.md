# The scoring harness

This is what scores every submission and the reference on the scoring box, and you can run it yourself. Everything that decides your number is in this directory.

## What a scored run does

1. **Allowlist check:** every `FROM` in your Dockerfile must be an image from [`allowlist.json`](allowlist.json), pinned by digest, or an earlier stage of the same Dockerfile.
2. **Build:** `docker build --network=none`, so `RUN` steps are offline. The build gets 10 minutes and the image may be at most 2 GB. Build time is not scored. The build runs on a separate build machine whose CPU differs from the scoring box and may have AVX-512, so never compile for native; target the scoring CPU with `-march=alderlake` / `-C target-cpu=alderlake`, or use `x86-64-v3`.
3. **Pre-warm:** the dataset is loaded into the page cache with `vmtouch` before timing.
4. **Five runs,** strictly one at a time, with nothing else on the box. Each run:
   - starts your image with the published limits (6 P-cores and 12 threads, 8 GB of memory with no swap, no network, read-only root, a 1 GB tmpfs at `/tmp`, `/data` read-only, `/out` writable);
   - runs your `ENTRYPOINT`/`CMD` under [`timer/sd-timer.c`](timer/sd-timer.c). The timer is PID 1 and starts your program as an unprivileged user (uid 65534). It times exec to exit with a monotonic clock, so container start and teardown never count. The record goes to a root-only mount your program can't write;
   - reads peak memory from the kernel's `memory.peak` of a parent cgroup created for that run alone, after the container is gone. This is the high-water mark of everything your process charged, including tmpfs;
   - checks that `/out` holds exactly the one file the spec names, that its sha256 matches, and that `docker diff` shows nothing written anywhere else.
5. **Aggregation:**
   - **T** is the mean of the middle three wall times (the highest and lowest are dropped).
   - **M** is the highest of the five peaks.
   - If the middle three spread by more than max(5%, 50 ms), the batch is re-run, at most twice.
   - A run over the timeout (10x the reference's T) fails, and so does any non-zero exit or wrong hash. The first failure stops the batch.

**S** (code lines by `scc` after your language's formatter) and **D** (1 + direct dependencies in your manifest) come from your repository at the recorded commit. K and points are computed in [`scoring.py`](scoring.py).

## Run it yourself

```
python3 harness/run_submission.py --context ./track-a --track a \
    --data ./.data/a/sample-seed1 --output <spec output name> \
    --expected-sha256 <published sample hash> --timeout-s <10x reference> \
    --timer harness/timer/sd-timer --cpuset 0-3 --work ~/sd-work --results result.json
```

`--timer` must be an `sd-timer` built for your container's architecture: a `linux/arm64` build ships as [`timer/sd-timer-arm64`](timer/sd-timer-arm64) for Apple Silicon, and [`setup-box.sh`](setup-box.sh) compiles `timer/sd-timer` on the box. Set `--cpuset` to cores your machine actually has (the default assumes 12), and `--work` to a directory the Docker VM can write, which on Colima must be under your home directory. A single `--runs 1` is fine for a quick check. Reading `memory.peak` needs root and Docker's `cgroupfs` driver on cgroup v2 (see [`setup-box.sh`](setup-box.sh)). On Colima, pass `--cgroups colima`. Your laptop's numbers are for your own iteration; only the scoring box's numbers count.

## S and D

[`static_axes.py`](static_axes.py) measures both. scc and the formatters run in a tools image ([`tools/Dockerfile`](tools/Dockerfile), every version pinned), built once with network and then run with `--network=none`. It needs Python 3.11 or newer.

```
docker build -t speeddemon/static-tools:2026-10-28 harness/tools
python3 harness/static_axes.py --track-dir ./track-a --json static.json
```

On the scoring box, add `--cpuset 12-19` so the tools container stays off the P-cores that timed runs use. Your track directory is copied, and only the copy is formatted and counted. The report lists everything that decided the two numbers.

- **S** is scc's code lines, with blank and comment lines left out, after each file goes through its formatter with that formatter's defaults: rustfmt (with the edition from your `Cargo.toml`), gofmt, clang-format `--style=LLVM`, ruff format, google-java-format, prettier and fourmolu. A `rustfmt.toml`, `.clang-format`, `.prettierrc`, `ruff.toml` or `.editorconfig` in your repo changes nothing. A file its formatter rejects is counted as written, and so is every language with no formatter in the image (Swift, C#, Kotlin and Zig, for example). gofmt and fourmolu never wrap a line, so lines over 200 characters, and Go lines holding several statements, are flagged for the organiser to look at.
- **Published copies are free.** A file byte-identical to one of our published generators or references (the generator copied in for build-time training, say) is listed under `not_counted`. An edited copy is your code and counts.
- **Prose and data are not code.** Markdown, plain text, JSON, YAML, TOML, XML, CSV and lockfiles are listed under `not_counted` and cost nothing, so PERF.md, your manifests and any sample data you commit are free.
- **Vendored code counts unless its own metadata shows it is a registry package:** a `node_modules/<package>/package.json`, a crate that `cargo vendor` wrote with a registry checksum, a module listed in Go's `vendor/modules.txt`, or a package pip installed with its `.dist-info/RECORD`. Those are listed under `excluded_paths`. A git dependency, code with no package metadata, or a file you add to a vendored package counts.
- **D** is 1 plus the runtime dependencies your manifests declare. [`dependencies.py`](dependencies.py) says what counts in each manifest format: dev, test and build dependencies never do, and a path dependency inside your track is your own code, so it counts in S instead. With no manifest at all, D comes from `dependency_count` in `.speed-demon.toml`, marked unverified.
- `--check-registry` is the organiser's spot-check and needs the network. It looks up when each direct dependency and each excluded package was first published, and flags anything first published on or after October 1, 2026.
- `python3 harness/static_axes.py --reference` recomputes S_ref, each track's reference file measured the same way, into [`reference_axes.json`](reference_axes.json).

On macOS the temporary copy goes under `~/.cache/speeddemon-static-axes`, because Docker's VM can always mount your home directory. Pass `--work` to put it somewhere else.
