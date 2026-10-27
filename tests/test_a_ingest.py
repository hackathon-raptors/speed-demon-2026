"""Tests for Track A (Ingest): generator, reference and the spec's worked example.

    python3.13 -m unittest discover -s tests -p 'test_a_*.py'
"""
import hashlib
import heapq
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATOR = os.path.join(ROOT, "generators", "a_ingest.py")
REFERENCE = os.path.join(ROOT, "reference", "a_ingest.py")
SPEC = os.path.join(ROOT, "specs", "a-ingest.md")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = load("a_ingest_generator", GENERATOR)
ref = load("a_ingest_reference", REFERENCE)

LINE_RE = re.compile(
    rb"(\d{4}-\d{2}-\d{2}T\d{2}):(\d{2}):(\d{2})\.(\d{6})Z"
    rb" (GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)"
    rb" (/[A-Za-z0-9/_.{}:-]*)"
    rb" ([1-5][0-9][0-9])"
    rb" (0|[1-9][0-9]{0,9})\n"
)
OUT_RE = re.compile(
    rb"(\d{4}-\d{2}-\d{2}T\d{2}):00:00Z (/[A-Za-z0-9/_.{}:-]*)"
    rb"((?: (?:0|[1-9][0-9]*)){6})\n"
)

# The spec's worked example. test_spec_contains_worked_example keeps the two in step.
WORKED_INPUT = """\
2026-10-30T19:00:00.000000Z GET /v2/orders/{id} 200 1500
2026-10-30T18:59:59.999999Z GET /v2/orders/{id} 200 1841
2026-10-30T18:00:00.000000Z POST /v2/checkout 503 204117
2026-10-30T18:00:00.000000Z POST /v2/checkout 201 30000
2026-10-30T18:00:00.115332Z GET /health 200 62
2026-10-30T18:00:00.115332Z GET /health 200 62
2026-10-30T18:42:17.000001Z HEAD /health 200 4294967295
2026-10-30T18:30:00.000001Z OPTIONS / 200 0
2026-10-30T18:12:00.500000Z POST /v2/checkout 500 0
2026-10-30T18:12:00.600000Z POST /v2/checkout 499 1500
2026-10-30T18:12:00.700000Z POST /v2/checkout 599 2500
2026-10-30T18:20:00.000000Z GET /v2/orders/{id} 500 900
2026-10-30T18:21:00.000000Z DELETE /v2/orders/{id} 404 77
2026-10-30T19:05:00.000000Z GET /v2/orders_legacy 200 60
2026-10-30T19:05:00.000001Z POST /v2/orders-export 202 20
2026-10-30T19:05:00.000002Z GET /v2/orders 200 10
2026-10-31T23:59:59.999999Z POST /v2/checkout 502 7
2026-11-01T00:00:00.000000Z POST /v2/checkout 503 8
"""
WORKED_OUTPUT = """\
2026-10-30T18:00:00Z / 1 0 0 0 0 0
2026-10-30T18:00:00Z /health 3 62 4294967295 4294967295 0 0
2026-10-30T18:00:00Z /v2/checkout 5 2500 204117 204117 3 600000
2026-10-30T18:00:00Z /v2/orders/{id} 3 900 1841 1841 1 333333
2026-10-30T19:00:00Z /v2/orders 1 10 10 10 0 0
2026-10-30T19:00:00Z /v2/orders-export 1 20 20 20 0 0
2026-10-30T19:00:00Z /v2/orders/{id} 1 1500 1500 1500 0 0
2026-10-30T19:00:00Z /v2/orders_legacy 1 60 60 60 0 0
2026-10-31T23:00:00Z /v2/checkout 1 7 7 7 1 1000000
2026-11-01T00:00:00Z /v2/checkout 1 8 8 8 1 1000000
"""


def edge_input():
    """Every edge case in the spec, in a deliberately unsorted file."""
    lines = [
        "2026-10-30T19:00:00.000000Z GET /v2/orders/{id} 200 1500",
        "2026-10-30T18:59:59.999999Z GET /v2/orders/{id} 200 1841",
        "2026-10-30T18:00:00.000000Z POST /v2/checkout 503 204117",
        "2026-10-30T18:00:00.000000Z POST /v2/checkout 201 30000",
        "2026-10-30T18:00:00.115332Z GET /health 200 62",
        "2026-10-30T18:00:00.115332Z GET /health 200 62",
        "2026-10-30T18:42:17.000001Z HEAD /health 200 4294967295",
        "2026-10-30T18:30:00.000001Z OPTIONS / 200 0",
        "2026-10-30T18:12:00.500000Z POST /v2/checkout 500 0",
        "2026-10-30T18:12:00.600000Z POST /v2/checkout 499 1500",
        "2026-10-30T18:12:00.700000Z POST /v2/checkout 599 2500",
        "2026-10-30T18:12:00.800000Z POST /v2/checkout 200 40000",
        "2026-10-30T18:20:00.000000Z GET /v2/orders/{id} 500 900",
        "2026-10-30T18:21:00.000000Z DELETE /v2/orders/{id} 404 77",
        # Byte-order family, scrambled.
        "2026-10-30T19:05:00.000000Z GET /v2/orderstatus 504 70",
        "2026-10-30T19:05:00.000001Z PATCH /v2/orders_legacy 410 60",
        "2026-10-30T19:05:00.000002Z GET /v2/ordersByDate 200 50",
        "2026-10-30T19:05:00.000003Z PUT /v2/orders/{id}/items 200 40",
        "2026-10-30T19:05:00.000004Z GET /v2/orders.csv 200 30",
        "2026-10-30T19:05:00.000005Z POST /v2/orders-export 202 20",
        "2026-10-30T19:05:00.000006Z GET /v2/orders 200 10",
        # Day and month rollovers.
        "2026-11-01T00:00:00.000000Z POST /v2/checkout 503 8",
        "2026-10-31T23:59:59.999999Z POST /v2/checkout 502 7",
        "2026-10-31T00:00:00.000000Z GET /v2/checkout 200 6",
        "2026-10-30T23:59:59.999999Z GET /v2/checkout 200 5",
        # Extreme latencies as a pair, an all-error group, a floor-rounded rate.
        "2026-11-02T17:59:59.999999Z GET /v1/users/{id} 200 4294967295",
        "2026-11-02T17:00:00.000000Z GET /v1/users/{id} 200 0",
        "2026-11-02T17:10:00.000000Z POST /v2/payments 503 120000",
        "2026-11-02T17:11:00.000000Z POST /v2/payments 500 90000",
        "2026-11-02T17:20:00.000000Z POST /v2/carts 500 700",
        "2026-11-02T17:20:01.000000Z POST /v2/carts 200 100",
        "2026-11-02T17:20:02.000000Z POST /v2/carts 200 600",
        "2026-11-02T17:20:03.000000Z POST /v2/carts 204 200",
        "2026-11-02T17:20:04.000000Z POST /v2/carts 200 500",
        "2026-11-02T17:20:05.000000Z POST /v2/carts 200 300",
        "2026-11-02T17:20:06.000000Z POST /v2/carts 599 400",
        "2026-11-02T17:30:00.000000Z GET " + gen.LONGEST_ENDPOINT + " 200 123456",
    ]
    # Nearest rank where ceil, round and floor disagree: n = 20, 100, 101.
    for i in range(20):
        lines.append(f"2026-11-02T16:{i:02d}:00.000000Z GET /v2/rank20 200 {20 - i}")
    for i in range(100):
        lines.append(f"2026-11-02T16:30:{i % 60:02d}.{i:06d}Z GET /v2/rank100 200 {i * 37 % 100 + 1}")
    for i in range(101):
        lines.append(f"2026-11-02T16:45:{i % 60:02d}.{i:06d}Z GET /v2/rank101 200 {(i * 38) % 101 + 1}")
    return "".join(line + "\n" for line in lines)


EDGE_OUTPUT = """\
2026-10-30T18:00:00Z / 1 0 0 0 0 0
2026-10-30T18:00:00Z /health 3 62 4294967295 4294967295 0 0
2026-10-30T18:00:00Z /v2/checkout 6 2500 204117 204117 3 500000
2026-10-30T18:00:00Z /v2/orders/{id} 3 900 1841 1841 1 333333
2026-10-30T19:00:00Z /v2/orders 1 10 10 10 0 0
2026-10-30T19:00:00Z /v2/orders-export 1 20 20 20 0 0
2026-10-30T19:00:00Z /v2/orders.csv 1 30 30 30 0 0
2026-10-30T19:00:00Z /v2/orders/{id} 1 1500 1500 1500 0 0
2026-10-30T19:00:00Z /v2/orders/{id}/items 1 40 40 40 0 0
2026-10-30T19:00:00Z /v2/ordersByDate 1 50 50 50 0 0
2026-10-30T19:00:00Z /v2/orders_legacy 1 60 60 60 0 0
2026-10-30T19:00:00Z /v2/orderstatus 1 70 70 70 1 1000000
2026-10-30T23:00:00Z /v2/checkout 1 5 5 5 0 0
2026-10-31T00:00:00Z /v2/checkout 1 6 6 6 0 0
2026-10-31T23:00:00Z /v2/checkout 1 7 7 7 1 1000000
2026-11-01T00:00:00Z /v2/checkout 1 8 8 8 1 1000000
2026-11-02T16:00:00Z /v2/rank100 100 50 95 99 0 0
2026-11-02T16:00:00Z /v2/rank101 101 51 96 100 0 0
2026-11-02T16:00:00Z /v2/rank20 20 10 19 20 0 0
2026-11-02T17:00:00Z """ + gen.LONGEST_ENDPOINT + """ 1 123456 123456 123456 0 0
2026-11-02T17:00:00Z /v1/users/{id} 2 0 4294967295 4294967295 0 0
2026-11-02T17:00:00Z /v2/carts 7 400 700 700 2 285714
2026-11-02T17:00:00Z /v2/payments 2 90000 120000 120000 2 1000000
"""


def run_reference(text):
    with tempfile.TemporaryDirectory() as tmp:
        data, out = os.path.join(tmp, "data"), os.path.join(tmp, "out")
        os.makedirs(data)
        with open(os.path.join(data, ref.INPUT_NAME), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        ref.main(data, out)
        with open(os.path.join(out, ref.OUTPUT_NAME), encoding="utf-8", newline="") as f:
            return f.read()


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def oracle(path):
    """The task computed a second way: bytes, slicing and heap selection, no datetime."""
    groups = {}
    with open(path, "rb") as f:
        for raw in f:
            ts, _method, endpoint, status, latency = raw.split(b" ")
            group = groups.setdefault((ts[:13], endpoint), [[], 0])
            group[0].append(int(latency))
            if status[:1] == b"5":
                group[1] += 1
    out = []
    for hour, endpoint in sorted(groups):
        values, errs = groups[(hour, endpoint)]
        n = len(values)
        ranks = [-(-p * n // 100) for p in (50, 95, 99)]
        picks = [heapq.nsmallest(r, values)[-1] for r in ranks]
        out.append(b"%s:00:00Z %s %d %d %d %d %d %d\n"
                   % (hour, endpoint, n, *picks, errs, errs * 1_000_000 // n))
    return b"".join(out)


class Datasets:
    """tiny and sample for seed 1, generated once per test run."""
    tmp = None

    @classmethod
    def path(cls, scale, seed=1):
        if cls.tmp is None:
            cls.tmp = tempfile.TemporaryDirectory()
        out = os.path.join(cls.tmp.name, f"{scale}-seed{seed}")
        path = os.path.join(out, gen.FILE_NAME)
        if not os.path.exists(path):
            gen.generate(seed, scale, out)
        return path


def tearDownModule():
    if Datasets.tmp is not None:
        Datasets.tmp.cleanup()


class GeneratorDeterminism(unittest.TestCase):
    def test_same_seed_same_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = gen.generate(1, "tiny", os.path.join(tmp, "a"))
            b = gen.generate(1, "tiny", os.path.join(tmp, "b"))
            self.assertEqual(sha256(a), sha256(b))

    def test_cli_matches_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "cli")
            subprocess.run([sys.executable, GENERATOR, "--seed", "1", "--scale", "tiny",
                            "--out", out], check=True, capture_output=True)
            self.assertEqual(os.listdir(out), [gen.FILE_NAME])
            self.assertEqual(sha256(os.path.join(out, gen.FILE_NAME)),
                             sha256(Datasets.path("tiny")))

    def test_different_seeds_differ(self):
        self.assertNotEqual(sha256(Datasets.path("tiny", 1)), sha256(Datasets.path("tiny", 2)))

    def test_sample_is_not_a_prefix_of_full(self):
        # Records come from a per-scale stream: the first lines of sample and full differ.
        _, _, sample_streams = gen.build(1, "sample")
        _, _, full_streams = gen.build(1, "full")
        first_sample = next(heapq.merge(*sample_streams))[3].splitlines()[:50]
        first_full = next(heapq.merge(*full_streams))[3].splitlines()[:50]
        self.assertNotEqual(first_sample, first_full)

    def test_catalogue_depends_on_seed_only(self):
        self.assertEqual(gen.build(1, "tiny")[0].names, gen.build(1, "sample")[0].names)
        self.assertNotEqual(gen.build(1, "tiny")[0].names, gen.build(2, "tiny")[0].names)


class GeneratedData(unittest.TestCase):
    """Grammar and every listed edge case, in tiny and in sample."""

    def check(self, scale):
        path = Datasets.path(scale)
        cat, plan, streams = gen.build(1, scale)
        with open(path, "rb") as f:
            data = f.read()
        self.assertTrue(data.endswith(b"\n"))
        self.assertNotIn(b"\r", data)
        lines = data.splitlines(keepends=True)
        self.assertEqual(len(lines), gen.LINES[scale])

        prefixes = {p[:-1].encode() for p in gen.HOUR_PREFIX}
        groups = {}
        methods, statuses, latencies, endpoints = set(), set(), set(), set()
        max_hour = b""
        inversion = adjacent_equal = duplicate = False
        seen_ts = {}
        nonadjacent_equal = False
        for i, line in enumerate(lines):
            m = LINE_RE.fullmatch(line)
            self.assertIsNotNone(m, line)
            hour, minute, second, _micro, method, endpoint, status, latency = m.groups()
            self.assertIn(hour, prefixes)
            self.assertLess(int(minute), 60)
            self.assertLess(int(second), 60)
            self.assertLessEqual(len(endpoint), gen.MAX_ENDPOINT_LEN)
            self.assertLessEqual(int(latency), gen.LATENCY_MAX)
            ts = line[:27]
            groups.setdefault((hour, endpoint), []).append((int(status), int(latency)))
            methods.add(method)
            statuses.add(int(status))
            latencies.add(int(latency))
            endpoints.add(endpoint)
            if hour < max_hour:
                inversion = True
            max_hour = max(max_hour, hour)
            if i and lines[i - 1][:27] == ts:
                if lines[i - 1] == line:
                    duplicate = True
                else:
                    adjacent_equal = True
            if ts in seen_ts and seen_ts[ts] != i - 1:
                nonadjacent_equal = True
            seen_ts[ts] = i

        def key(h, e):
            return gen.HOUR_PREFIX[h][:-1].encode(), cat.names[e].encode()

        for h, e in plan.single_groups:
            self.assertEqual(len(groups[key(h, e)]), 1)
        for h, e in plan.extreme_groups:
            self.assertEqual(sorted(lat for _, lat in groups[key(h, e)]), [0, gen.LATENCY_MAX])
        for h, e in plan.status_groups:
            self.assertEqual(sorted(st for st, _ in groups[key(h, e)]), [200, 499, 500, 599])
        self.assertGreaterEqual(len(plan.single_groups), 3)
        self.assertGreaterEqual(len(plan.extreme_groups), 1)
        self.assertGreaterEqual(len(plan.status_groups), 1)

        self.assertIn(b"2026-10-30T18:00:00.000000Z", seen_ts)
        self.assertIn(b"2026-11-02T17:59:59.999999Z", seen_ts)
        self.assertIn(b"2026-11-01T00:00:00.000000Z", seen_ts)
        self.assertIn(b"2026-10-31T23:59:59.999999Z", seen_ts)
        self.assertTrue(any(ts.endswith(b":00:00.000000Z") for ts in seen_ts))
        self.assertTrue(any(ts.endswith(b":59:59.999999Z") for ts in seen_ts))
        self.assertIn(0, latencies)
        self.assertIn(gen.LATENCY_MAX, latencies)
        self.assertTrue({499, 500, 599} <= statuses)
        self.assertTrue(adjacent_equal, "equal timestamps on consecutive lines")
        self.assertTrue(nonadjacent_equal, "equal timestamps on lines far apart")
        self.assertTrue(duplicate, "byte-identical duplicate lines")
        self.assertTrue(inversion, "a line earlier than a previous line's hour")
        self.assertEqual(methods, {m.encode() for m in gen.METHODS})
        for name in gen.CORE_ENDPOINTS:
            self.assertIn(name.encode(), endpoints)
        catalogue = {name.encode() for name in cat.names}
        self.assertTrue(endpoints <= catalogue)
        self.assertEqual(len(catalogue), gen.N_ENDPOINTS)
        if scale != "tiny":
            self.assertEqual(len(endpoints), gen.N_ENDPOINTS)

        # Each shard's own stream is non-decreasing in time.
        for stream in streams:
            last = b""
            for _, _, _, text in stream:
                for line in text.encode().splitlines():
                    self.assertGreaterEqual(line[:27], last)
                    last = line[:27]

    def test_tiny(self):
        self.check("tiny")
        with open(Datasets.path("tiny"), encoding="ascii") as f:
            for line in f:
                datetime.fromisoformat(line[:27])   # a real calendar date and time

    def test_sample(self):
        self.check("sample")


class Reference(unittest.TestCase):
    def test_worked_example(self):
        self.assertEqual(run_reference(WORKED_INPUT), WORKED_OUTPUT)

    def test_spec_contains_worked_example(self):
        with open(SPEC, encoding="utf-8") as f:
            spec = f.read()
        self.assertIn("```text\n" + WORKED_INPUT + "```", spec)
        self.assertIn("```text\n" + WORKED_OUTPUT + "```", spec)

    def test_edge_cases(self):
        self.assertEqual(run_reference(edge_input()), EDGE_OUTPUT)

    def test_empty_input_gives_empty_output(self):
        self.assertEqual(run_reference(""), "")

    def test_deterministic_on_tiny(self):
        path = Datasets.path("tiny")
        with tempfile.TemporaryDirectory() as tmp:
            outs = []
            for run in ("1", "2"):
                out = os.path.join(tmp, run)
                ref.main(os.path.dirname(path), out)
                self.assertEqual(os.listdir(out), [ref.OUTPUT_NAME])
                outs.append(sha256(os.path.join(out, ref.OUTPUT_NAME)))
            self.assertEqual(outs[0], outs[1])

    def test_cli_writes_one_file(self):
        path = Datasets.path("tiny")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            done = subprocess.run([sys.executable, REFERENCE, "--data", os.path.dirname(path),
                                   "--out", out], capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(os.listdir(out), [ref.OUTPUT_NAME])

    def check_against_oracle(self, scale):
        path = Datasets.path(scale)
        with tempfile.TemporaryDirectory() as tmp:
            ref.main(os.path.dirname(path), tmp)
            with open(os.path.join(tmp, ref.OUTPUT_NAME), "rb") as f:
                got = f.read()
        self.assertEqual(got, oracle(path))
        # Output rules: grammar, total order, no repeated key, LF only, final newline.
        self.assertTrue(got.endswith(b"\n"))
        self.assertNotIn(b"\r", got)
        previous = None
        for line in got.splitlines(keepends=True):
            m = OUT_RE.fullmatch(line)
            self.assertIsNotNone(m, line)
            key = (m.group(1), m.group(2))
            if previous is not None:
                self.assertLess(previous, key)
            previous = key
            count, p50, p95, p99, errs, ppm = (int(x) for x in m.group(3).split())
            self.assertGreaterEqual(count, 1)
            self.assertTrue(p50 <= p95 <= p99)
            self.assertLessEqual(errs, count)
            self.assertEqual(ppm, errs * 1_000_000 // count)

    def test_tiny_matches_oracle(self):
        self.check_against_oracle("tiny")

    def test_sample_matches_oracle(self):
        self.check_against_oracle("sample")


if __name__ == "__main__":
    unittest.main()
