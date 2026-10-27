"""Tests for Track D (Traverse): generator, reference and the spec's edge cases.

    python3.13 -m unittest discover -s tests -p 'test_d_*.py'
"""

import collections
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def load(name, relpath):
    """Import a file under a unique name: the generator and reference share a basename."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = load("d_traverse_generator", "generators/d_traverse.py")
ref = load("d_traverse_reference", "reference/d_traverse.py")

UINT32_MAX = 2**32 - 1

# The worked example from specs/d-traverse.md, section 4.
EXAMPLE_GRAPH = """\
8 12
0 1 4
1 2 3
0 2 9
2 0 5
2 1 6
1 3 0
3 4 0
4 4 7
4 5 4294967295
2 1 2
5 6 4294967295
0 1 4
"""
EXAMPLE_QUERIES = """\
0 2
2 1
1 4
4 4
7 7
0 6
4 5
6 0
0 7
1 0
2 1
"""
EXAMPLE_OUTPUT = """\
7
2
0
0
0
8589934594
4294967295
-1
-1
8
2
"""


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write_case(directory, graph, queries):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "graph.txt"), "w", newline="\n") as f:
        f.write(graph)
    with open(os.path.join(directory, "queries.txt"), "w", newline="\n") as f:
        f.write(queries)


def run_reference(data_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    ref.main(data_dir, out_dir)
    with open(os.path.join(out_dir, "distances.txt"), "rb") as f:
        return f.read().decode("ascii")


def read_edges(data_dir):
    with open(os.path.join(data_dir, "graph.txt"), encoding="ascii") as f:
        n, m = map(int, f.readline().split())
        edges = [tuple(map(int, line.split())) for line in f]
    return n, m, edges


def read_queries(data_dir):
    with open(os.path.join(data_dir, "queries.txt"), encoding="ascii") as f:
        return [tuple(map(int, line.split())) for line in f]


def bellman_ford(n, edges, s):
    """Independent check: distances from s by repeated relaxation, no heap, no early exit."""
    dist = {s: 0}
    changed = True
    while changed:
        changed = False
        for u, v, w in edges:
            if u in dist and dist[u] + w < dist.get(v, dist[u] + w + 1):
                dist[v] = dist[u] + w
                changed = True
    return dist


class HandWrittenCases(unittest.TestCase):
    def check(self, graph, queries, expected):
        with tempfile.TemporaryDirectory() as tmp:
            write_case(os.path.join(tmp, "data"), graph, queries)
            out = run_reference(os.path.join(tmp, "data"), os.path.join(tmp, "out"))
            self.assertEqual(out, expected)
            self.assertEqual(os.listdir(os.path.join(tmp, "out")), ["distances.txt"])

    def test_worked_example(self):
        # parallel edges (heavier listed first), weight-0 path, self-loop, isolated
        # node, node with no out-edges, 2^32 - 1 weights, sums above 2^32 - 1,
        # duplicate edge line, duplicate query.
        self.check(EXAMPLE_GRAPH, EXAMPLE_QUERIES, EXAMPLE_OUTPUT)

    def test_parallel_edges_lighter_listed_first(self):
        graph = "3 4\n0 1 5\n0 1 50\n1 2 1\n0 2 100\n"
        self.check(graph, "0 1\n0 2\n", "5\n6\n")

    def test_parallel_edges_three_way_and_exact_duplicates(self):
        graph = "2 4\n0 1 9\n0 1 9\n0 1 3\n0 1 7\n"
        self.check(graph, "0 1\n1 0\n", "3\n-1\n")

    def test_zero_weight_cycle_and_zero_distance(self):
        graph = "3 3\n0 1 0\n1 0 0\n1 2 0\n"
        self.check(graph, "0 2\n2 0\n0 0\n", "0\n-1\n0\n")

    def test_source_with_no_out_edges_and_target_with_no_in_edges(self):
        graph = "4 3\n0 1 1\n1 2 1\n3 0 1\n"
        self.check(graph, "2 0\n0 3\n3 2\n3 3\n", "-1\n-1\n3\n0\n")

    def test_direction_matters(self):
        graph = "2 2\n0 1 37\n1 0 41\n"
        self.check(graph, "0 1\n1 0\n", "37\n41\n")

    def test_long_chain_of_max_weights_fits_in_int64(self):
        k = 5
        lines = "".join(f"{i} {i + 1} {UINT32_MAX}\n" for i in range(k))
        self.check(f"{k + 1} {k}\n{lines}", f"0 {k}\n", f"{k * UINT32_MAX}\n")


class GeneratedData(unittest.TestCase):
    """Edge cases the spec lists must be present in tiny and sample, deterministically."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.built = {}
        for scale in ("tiny", "sample"):
            n, arcs, queries, kinds, manifest = gen.build(1, scale)
            directory = os.path.join(cls.tmp.name, scale)
            gen.write(directory, n, arcs, queries)
            cls.built[scale] = (directory, kinds, manifest)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_generator_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            hashes = []
            for run, seed in (("a", 1), ("b", 1), ("c", 2)):
                n, arcs, queries, _, _ = gen.build(seed, "tiny")
                gen.write(os.path.join(tmp, run), n, arcs, queries)
                hashes.append(tuple(sha256(os.path.join(tmp, run, f)) for f in ("graph.txt", "queries.txt")))
            self.assertEqual(hashes[0], hashes[1])
            self.assertNotEqual(hashes[0][0], hashes[2][0])
            self.assertNotEqual(hashes[0][1], hashes[2][1])
            tiny = self.built["tiny"][0]
            self.assertEqual(hashes[0], tuple(sha256(os.path.join(tiny, f)) for f in ("graph.txt", "queries.txt")))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, out = os.path.join(tmp, "data"), os.path.join(tmp, "out")
            os.makedirs(out)
            subprocess.run([sys.executable, os.path.join(ROOT, "generators", "d_traverse.py"),
                            "--seed", "1", "--scale", "tiny", "--out", data], check=True)
            subprocess.run([sys.executable, os.path.join(ROOT, "reference", "d_traverse.py"),
                            "--data", data, "--out", out], check=True)
            self.assertEqual(sorted(os.listdir(data)), ["graph.txt", "queries.txt"])
            tiny = self.built["tiny"][0]
            self.assertEqual(sha256(os.path.join(data, "graph.txt")), sha256(os.path.join(tiny, "graph.txt")))
            self.assertEqual(os.listdir(out), ["distances.txt"])

    def test_reference_is_deterministic_and_matches_bellman_ford_on_tiny(self):
        directory = self.built["tiny"][0]
        with tempfile.TemporaryDirectory() as tmp:
            first = run_reference(directory, os.path.join(tmp, "1"))
            second = run_reference(directory, os.path.join(tmp, "2"))
        self.assertEqual(first, second)
        n, _, edges = read_edges(directory)
        expected = []
        cache = {}
        for s, t in read_queries(directory):
            if s not in cache:
                cache[s] = bellman_ford(n, edges, s)
            expected.append(cache[s].get(t, -1))
        self.assertEqual(first, "".join(f"{d}\n" for d in expected))

    def test_sizes_and_format(self):
        for scale, (directory, kinds, manifest) in self.built.items():
            with self.subTest(scale=scale):
                n_want, m_want, q_want = gen.SCALES[scale]
                with open(os.path.join(directory, "graph.txt"), "rb") as f:
                    raw = f.read()
                self.assertTrue(raw.endswith(b"\n") and b"\r" not in raw and b"  " not in raw)
                n, m, edges = read_edges(directory)
                self.assertEqual((n, m, len(edges)), (n_want, m_want, m_want))
                queries = read_queries(directory)
                self.assertEqual(len(queries), q_want)
                for u, v, w in edges:
                    self.assertTrue(0 <= u < n and 0 <= v < n and 0 <= w <= UINT32_MAX)
                for s, t in queries:
                    self.assertTrue(0 <= s < n and 0 <= t < n)
                self.assertNotEqual([u for u, _, _ in edges], sorted(u for u, _, _ in edges))

    def test_graph_edge_cases_present(self):
        for scale, (directory, _, _) in self.built.items():
            with self.subTest(scale=scale):
                n, _, edges = read_edges(directory)
                indeg, outdeg = [0] * n, [0] * n
                by_pair = collections.defaultdict(list)  # (u, v) -> weights in file order
                for u, v, w in edges:
                    outdeg[u] += 1
                    indeg[v] += 1
                    by_pair[(u, v)].append(w)
                self.assertTrue(any(indeg[x] == 0 and outdeg[x] == 0 for x in range(n)), "isolated node")
                self.assertTrue(any(indeg[x] == 0 and outdeg[x] > 0 for x in range(n)), "no in-edges")
                self.assertTrue(any(outdeg[x] == 0 and indeg[x] > 0 for x in range(n)), "no out-edges")
                groups = [ws for ws in by_pair.values() if len(set(ws)) > 1]
                self.assertTrue(any(ws[0] == min(ws) for ws in groups), "parallel, lighter listed first")
                self.assertTrue(any(ws[0] != min(ws) for ws in groups), "parallel, heavier listed first")
                self.assertTrue(any(len(ws) != len(set(ws)) for ws in by_pair.values()), "duplicate line")
                self.assertTrue(any(u == v for u, v, _ in edges), "self-loop")
                self.assertTrue(any(w == 0 for _, _, w in edges), "weight 0")
                self.assertTrue(any(w == UINT32_MAX for _, _, w in edges), "weight 2^32 - 1")
                simple = [p for p in by_pair if p[0] != p[1]]
                self.assertTrue(any((v, u) not in by_pair for u, v in simple), "one-way edge")
                self.assertTrue(any((v, u) in by_pair and by_pair[(v, u)] != by_pair[(u, v)]
                                    for u, v in simple), "reverse edge with a different weight")

    def test_query_edge_cases_present(self):
        for scale, (directory, kinds, manifest) in self.built.items():
            with self.subTest(scale=scale):
                queries = read_queries(directory)
                self.assertEqual(len(kinds), len(queries))
                for kind, _, minimum in gen.QUERY_KINDS:
                    self.assertGreaterEqual(kinds.count(kind), minimum, kind)
                self.assertGreater(kinds.count("normal"), 0)
                self.assertLess(len(set(queries)), len(queries), "duplicate query")
                self.assertTrue(any(s == t for s, t in queries), "s == t")

                adj = ref.read_graph(os.path.join(directory, "graph.txt"))
                answers = {}
                for (s, t), kind in zip(queries, kinds):
                    if kind != "normal":
                        answers.setdefault(kind, []).append(ref.shortest_path(adj, s, t))
                for kind in ("same_node", "same_isolated", "zero_path"):
                    self.assertTrue(all(d == 0 for d in answers[kind]), kind)
                for kind in ("unreachable_island", "unreachable_sink", "unreachable_isolated",
                             "unreachable_source"):
                    self.assertTrue(all(d == -1 for d in answers[kind]), kind)
                self.assertTrue(all(d == UINT32_MAX for d in answers["max_weight"]))
                self.assertTrue(all(d > UINT32_MAX for d in answers["over_uint32"]))
                self.assertTrue(all(d > 0 for d in answers["to_sink"] + answers["from_source"]))
                self.assertTrue(all(d >= UINT32_MAX - (1 << 20) for d in answers["from_source"]))

    def test_parallel_minimum_decides_some_answers(self):
        # Keeping only the first-listed, or only the last-listed, edge of each
        # parallel group must change at least one answer at every scale.
        for scale, (directory, kinds, _) in self.built.items():
            with self.subTest(scale=scale):
                n, _, edges = read_edges(directory)
                queries = [q for q, kind in zip(read_queries(directory), kinds) if kind == "parallel_min"]
                truth = ref.read_graph(os.path.join(directory, "graph.txt"))
                want = [ref.shortest_path(truth, s, t) for s, t in queries]
                for keep in ("first", "last"):
                    chosen = {}
                    for u, v, w in edges:
                        if keep == "last" or (u, v) not in chosen:
                            chosen[(u, v)] = w
                    adj = [[] for _ in range(n)]
                    for (u, v), w in chosen.items():
                        adj[u].append((v, w))
                    got = [ref.shortest_path(adj, s, t) for s, t in queries]
                    self.assertNotEqual(got, want, keep)


if __name__ == "__main__":
    unittest.main()
