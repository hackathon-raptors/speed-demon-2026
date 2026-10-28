# Conventions every track follows

Speed Demon 2026 has four tracks: A Ingest, B Index, C Join, D Traverse. Each one ships four files built to the same contract, so the harness can treat every track identically.

| Track | Spec | Generator | Reference | Tests |
|---|---|---|---|---|
| A | `specs/a-ingest.md` | `generators/a_ingest.py` | `reference/a_ingest.py` | `tests/test_a_ingest.py` |
| B | `specs/b-index.md` | `generators/b_index.py` | `reference/b_index.py` | `tests/test_b_index.py` |
| C | `specs/c-join.md` | `generators/c_join.py` | `reference/c_join.py` | `tests/test_c_join.py` |
| D | `specs/d-traverse.md` | `generators/d_traverse.py` | `reference/d_traverse.py` | `tests/test_d_traverse.py` |

## Runtime and dependencies

- Python 3.13 (`python:3.13-slim` in the container; 3.11 or newer works locally). Standard library only, for generators, references and tests.
- No `numpy`, no `pandas`, nothing from PyPI.

## Generator

```
python3.13 generators/<track>.py --seed <int> --scale {full,sample,tiny} --out <dir>
```

- Writes the track's input files, with the exact names the spec gives, into `<dir>`, creating it if needed.
- **Deterministic:** the same seed and scale give byte-identical files on any machine and any run. Use `random.Random(seed)` instances only, never the global `random` and never `hash()` of strings. Nothing may depend on time, environment, locale or dict ordering beyond insertion order.
- **Scales:**
  - `full` is the scoring size (the spec states it);
  - `sample` is 10% of full, from the same distribution;
  - `tiny` is about 1,000 records, for tests.
- Scales share the distribution, not the records: `sample` is not a prefix of `full`.
- **Every edge case in the spec appears at every scale, deterministically.** Inject each one on purpose (at least once in `tiny`, at a fixed rate in `sample` and `full`) rather than leaving it to chance. A team must never fail a gate on the holdout over a case its sample did not contain. A test asserts that every listed edge case is present in `tiny` and `sample`.
- **Seeds:** seed 1 is the development seed. The holdout seed is chosen privately at scoring time, and nothing in the generator may make one seed special.
- **Speed:** generating `full` should take under 5 minutes on a laptop.

## Reference implementation

```
python3.13 reference/<track>.py [--data /data] [--out /out]
```

- Defaults are `/data` and `/out`, which is the container contract. It reads only the spec's input files and writes exactly one output file, named by the spec, then exits 0.
- **Deliberately naive but correct.** It's single-threaded, written the obvious way the site describes for the track, and readable, because teams will read it. Every slow choice should be one a strong team can improve on.
- **Deterministic:** byte-identical output across runs. Its sha256 is the correctness gate.
- **Runtime target:** 20 to 60 s on this machine (Apple M4 Pro) at `full` scale.
  - The "about 2x slower on the scoring box" ratio is a guess. The real calibration happens on the box Oct 26-27, so hit the band loosely and report the measured number. Don't spend an hour chasing it.
  - If the site's published shape makes the naive reference far slower or faster, tune the generator's distribution parameters (locality, Zipf exponent, query mix) inside the published shape.
  - Change the published counts only as a last resort, and record any change in the spec's "Changes from the site" section.
- **Memory ceiling:** peak RSS at `full` scale must stay under 4 GB, measured with `/usr/bin/time -l`. The harness measures M_ref under the same 8 GB cap as every team.
- **Container:** the harness runs the reference exactly like a submission, through the shared `reference/Dockerfile` and `reference/run.py`, which dispatch on the `TRACK` environment variable. Don't write another Dockerfile; just make sure your module exposes `main(data_dir, out_dir)`.

## Output format rules (all tracks)

- UTF-8, LF line endings, one trailing newline, no BOM, no trailing spaces.
- **No binary floating point in any output.** Define every non-integer value with integer arithmetic and an explicit rounding rule. For example, a rate is printed as `floor(num * 1_000_000 / den)` parts per million, and money as integer cents formatted with exactly two decimals.
- Every ordering is total and explicit: the sort key, then bytewise ascending tie-breaks. Never "any order".
- **Fields are separated by a single space.** If any field can contain a space, the whole file uses a single TAB instead, and the spec says so. Numbers are in decimal with no leading zeros and no plus sign.

## Spec template (`specs/<track>.md`)

1. **Summary:** one paragraph.
2. **Input files:** exact names, encoding, a grammar or byte-level description, value ranges, and every edge case the generator can emit.
3. **Task:** precise semantics. Cover every case: empty groups, unknown keys, ties, unreachable.
4. **Output:** the file name, the exact line format, ordering and rounding, plus a worked example with a small input and its exact output bytes.
5. **Sizes:** `full` and `sample` record counts and bytes, as measured from your generator with seed 1.
6. **Banned libraries:** per the site's rule 02, named explicitly.
7. **Reference behaviour:** what the naive reference does and why it is slow, in a few bullets. Do not hint at optimisations beyond what the site already says.
8. **Changes from the site:** every place the spec differs from https://speeddemon.dev (the site's `src/data/tracks.json`), with the reason.

## Tests (`tests/test_<track>.py`, stdlib `unittest`)

- The generator is deterministic: run `tiny` twice and compare hashes; run it with two seeds and expect different output.
- The reference is deterministic on `tiny`.
- Hand-written edge-case inputs give the hand-computed expected outputs, covering every edge case the spec lists.
- Run with `python3.13 -m unittest discover -s tests`.

## Working data

Generated datasets go in `.data/<track>/<scale>-seed<N>/` inside this repo; the directory is gitignored. Delete `full` datasets when you're done measuring.
