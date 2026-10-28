# Speed Demon 2026

Track specs, dataset generators, naive references and the scoring harness for [Speed Demon 2026](https://speeddemon.dev), October 30 to November 2, 2026. To enter, start from the [starter template](https://github.com/hackathon-raptors/speed-demon-starter).

| Track | Spec | Generator | Reference |
|---|---|---|---|
| A Ingest | [`specs/a-ingest.md`](specs/a-ingest.md) | [`generators/a_ingest.py`](generators/a_ingest.py) | [`reference/a_ingest.py`](reference/a_ingest.py) |
| B Index | [`specs/b-index.md`](specs/b-index.md) | [`generators/b_index.py`](generators/b_index.py) | [`reference/b_index.py`](reference/b_index.py) |
| C Join | [`specs/c-join.md`](specs/c-join.md) | [`generators/c_join.py`](generators/c_join.py) | [`reference/c_join.py`](reference/c_join.py) |
| D Traverse | [`specs/d-traverse.md`](specs/d-traverse.md) | [`generators/d_traverse.py`](generators/d_traverse.py) | [`reference/d_traverse.py`](reference/d_traverse.py) |

- [`harness/`](harness/README.md) scores every submission, and you can run it yourself.
- [`board/`](board/README.md) is the live board that runs each push from kickoff to code freeze.
- [`CONVENTIONS.md`](CONVENTIONS.md) is the contract every track follows.

## Running it

Python 3.11 or newer, standard library only.

```
python3 generators/a_ingest.py --seed 1 --scale sample --out .data/a/sample-seed1
python3 -m unittest discover -s tests
```

The same seed and scale give byte-identical files on any machine. Seed 1 at `full` scale is the live board's dataset; the full datasets and their expected hashes are also published on speeddemon.dev at kickoff, October 30, 15:00 UTC.

MIT licensed.
