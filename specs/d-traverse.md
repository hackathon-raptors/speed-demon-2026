# Track D: Traverse

The algorithms track. Generator `generators/d_traverse.py`, reference `reference/d_traverse.py`, tests `tests/test_d_traverse.py`.

## 1. Summary

The input is a directed graph with non-negative integer weights, built like a road network: 1,740,512 nodes and 5,000,000 edges, followed by 2,000 source-target queries. For each query, print the length of the shortest directed path from source to target, or `-1` if there is none. Preprocessing is explicitly allowed and is counted inside the timed run: the clock covers reading the files, building whatever you build, answering every query and writing the answers.

## 2. Input files

Two files in `/data`, both read-only: `graph.txt` and `queries.txt`.

Both are ASCII (and so valid UTF-8), with LF line endings, a single space between fields, one trailing newline, no blank lines, no BOM and no trailing spaces. Every number is an unsigned decimal integer with no sign and no leading zeros (zero is written `0`).

### `graph.txt`

```
graph   = header edge*
header  = n SP m LF
edge    = u SP v SP w LF        ; exactly m of these
```

| Field | Range | Meaning |
|---|---|---|
| `n` | 1,740,512 at `full` | Number of nodes. Node ids are dense: every integer in `[0, n)` is a node, including ids that appear on no edge line. |
| `m` | 5,000,000 at `full` | Number of edge lines that follow. |
| `u` | `0 <= u < n` | Tail of a directed edge. |
| `v` | `0 <= v < n` | Head of the edge. |
| `w` | `0 <= w <= 4294967295` | Weight, a uint32. **Zero is a legal weight.** |

Edge lines are in no particular order: not sorted by `u`, `v`, `w` or anything else. Each line is one directed edge from `u` to `v`. These all occur, at every scale (counts per scale in section 5):

- **Reverse edges.** If both `u -> v` and `v -> u` exist they are separate lines, usually with different weights (`0 918442 37` and `918442 0 41`). About 3% of directed node pairs have no reverse edge at all (one-way streets).
- **Parallel edges.** The same `(u, v)` appears on two or more lines with different weights. In some groups the lighter line comes first in the file, in others the heavier one does.
- **Exact duplicate lines.** The same `u v w` appears more than once.
- **Self-loops.** Lines with `u == v`.
- **Weight 0.** Including paths made only of weight-0 edges, so `s != t` can be at distance 0.
- **Very large weights.** Weights of exactly 4,294,967,295 and weights within 2^20 of it.
- **Nodes with no edges at all**, **nodes with no in-edges** and **nodes with no out-edges**.
- **Parts of the graph that nothing else connects to**, and so pairs of nodes with no path between them.

### `queries.txt`

```
queries = query*                 ; 2,000 lines at full scale, no header
query   = s SP t LF
```

`0 <= s < n` and `0 <= t < n`. The query count is the number of lines. These all occur, at every scale:

- `s == t`, including on a node that has no edges.
- `s != t` with distance 0.
- Unreachable targets, answered `-1`: from a source with no out-edges, from a node with no edges, from a component that cannot reach the target, and to a target with no in-edges.
- Distances of exactly 4,294,967,295 and distances above it.
- Duplicate query lines (the same `s t` more than once).

### How the data is generated

The generator is deterministic for a given seed and scale (seed 1 is the development seed; the scoring seed is private). Every scale is built from the same distribution; `sample` is not a prefix of `full`. To make your own: `python3.13 generators/d_traverse.py --seed <int> --scale {full,sample,tiny} --out <dir>` (`full` takes under a minute on a laptop).

**Main body: a road network.** All but a few hundred nodes sit on a jittered square lattice. Arterial roads run along every 8th row and column, highway links join every 6th node along every 48th row and column, and local streets and some diagonals fill in the rest. The arterials, the highways and a random spanning forest of local streets are two-way, so **the main body is strongly connected**: every main-body node can reach every other. About 85% of the remaining local streets are two-way, the rest one-way. A weight is a travel time: straight-line length divided by the road's speed (highways about 5x and arterials about 2.5x faster than local streets, 3% of local streets 3 to 12x slower), with independent noise of up to 12% per direction. Main-body weights are between 1 and a few thousand (median about 120), apart from the injected weight-0 edges and parallel copies.

**Special structures**, outside the main body, at fixed rates:

| Structure | Count | Edges |
|---|---|---|
| Isolated nodes | `max(2, n // 100000)` | none |
| Nodes with no in-edges | `max(2, n // 100000)` | two out-edges each, into the main body, weights in `[2^32 - 2^20, 2^32 - 1]` |
| Nodes with no out-edges | `max(2, n // 100000)` | two in-edges each, from the main body, weights 1 to 500 |
| Islands | `max(1, n // 200000)` | 14 nodes and 33 edges each, no edge to or from anything else: a two-way ring of 12, a weight-0 path, two parallel pairs, a self-loop, and a two-edge spur of weights near 2^32 whose last node has no out-edges |

Inside the main body, chosen at random: `max(2, m // 1000)` extra parallel edges (each a copy of an existing edge with a different weight, lighter or heavier), `max(1, m // 5000)` exact duplicate lines, `max(1, m // 10000)` self-loops, and `max(1, m // 2000)` edges whose weight is set to 0.

Node ids are a random relabelling, so an id says nothing about where a node is in the network, and edge lines are shuffled.

**Queries.** Each line is one of the kinds below, in random order. Counts are `max(minimum, q * per_mille // 1000)`; every query that is not injected is normal.

| Kind | Per 1000 | Min | s | t | Answer |
|---|---|---|---|---|---|
| normal | rest | | uniform over the main body | by Dijkstra rank, below | distance |
| same node | 2 | 1 | main body | `= s` | 0 |
| same isolated | 1 | 1 | node with no edges | `= s` | 0 |
| zero path | 1 | 1 | island | island, via weight-0 edges | 0 |
| parallel min | 2 | 2 | island | island, decided by the lighter of two parallel edges (one group listed lighter first, one heavier first) | distance |
| max weight | 1 | 1 | island | spur | 4294967295 |
| over uint32 | 1 | 1 | island | spur end | above 4294967295 |
| from no-in node | 1 | 1 | node with no in-edges | by Dijkstra rank | at least 4293918720 |
| to no-out node | 1 | 1 | main body, a tail of one of t's in-edges | node with no out-edges | distance |
| unreachable, island | 2 | 1 | island | main body | -1 |
| unreachable, no-out source | 1 | 1 | node with no out-edges | main body | -1 |
| unreachable, isolated source | 1 | 1 | node with no edges | main body | -1 |
| unreachable, no-in target | 1 | 1 | main body | node with no in-edges | -1 |
| duplicate | 10 | 1 | a copy of another query line, never of an unreachable no-in-target one | | same as the original |

**Query distance.** A normal query's target is chosen by Dijkstra rank: an octave `o` is drawn uniformly from 1 to 16, then a rank `r` uniformly from `[2^o, 2^(o+1) - 1]`, and `t` is the `r`-th node that a Dijkstra search from `s` settles (`s` itself is settled first; ties are broken arbitrarily). So `t != s`, `t` is always reachable, `log2(r)` is close to uniform between 1 and 17, the median `r` is 512, the mean is about 12,300 and the maximum is 131,071. At `tiny` scale `r` is capped at the main-body size minus one. The distribution is the same at every scale, so a `sample` query is as long as a `full` one; `sample` simply has a tenth as many of them on a tenth of the map.

## 3. Task

For each query `(s, t)`, compute `d(s, t)`: the minimum, over all directed paths from `s` to `t`, of the sum of the path's edge weights.

- A path may have zero edges, so `d(s, s) = 0` for every node, including nodes with no edges and nodes with a self-loop.
- If no directed path from `s` to `t` exists, the answer is `-1`.
- Every edge line is an edge. With parallel edges, a shortest path uses the lightest one; duplicate lines change nothing. A self-loop never shortens a path.
- Weight 0 is an ordinary weight. All weights are non-negative.
- Direction matters: `d(s, t)` and `d(t, s)` are independent.
- A duplicated query line is answered again, with the same answer, on its own line.
- Answers are exact integers and can exceed 2^32 - 1. A shortest path has at most `n - 1` edges, so every answer is at most `(n - 1) * (2^32 - 1) < 2^63` and fits in a signed 64-bit integer.

**Preprocessing is allowed and counted.** The harness times the whole process, from exec to exit, so parsing, any index or ordering you build, the queries and the output are all inside your time. You may answer queries in any order and in parallel; only the output order is fixed. Nothing derived from the input may be computed ahead of the run (site rule 04): the scoring graph is generated at a seed you have not seen.

## 4. Output

One file, `/out/distances.txt`, ASCII, LF line endings, one trailing newline, nothing else.

- Exactly one line per query line, in input order: line `i` answers line `i` of `queries.txt`.
- Each line is `d(s, t)` in decimal with no leading zeros and no sign (`0` for zero), or the two characters `-1` when `t` is unreachable from `s`.
- The answer is fully determined by the input, so there are no ties or rounding to define. The SHA-256 of this file is the correctness gate.

### Worked example

`graph.txt` (8 nodes, 12 edge lines):

```
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
```

`queries.txt`:

```
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
```

`distances.txt`, exactly these 42 bytes (SHA-256 `2e9871bba1d5013144ceff48d806a28da0e3f203b29118a89d98d8f654c3f6a0`):

```
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
```

| Query | Answer | Why |
|---|---|---|
| `0 2` | 7 | `0->1->2` is 4 + 3, shorter than the direct edge of 9. The duplicate line `0 1 4` changes nothing. |
| `2 1` | 2 | Parallel edges `2 1 6` and `2 1 2`: the lighter one counts, though it is listed second. |
| `1 4` | 0 | `1->3->4` uses two weight-0 edges, so `s != t` at distance 0. |
| `4 4` | 0 | `s == t`. The self-loop `4 4 7` is never part of a shortest path. |
| `7 7` | 0 | Node 7 is on no edge line but is still a node, because `n = 8`. |
| `0 6` | 8589934594 | 4 + 0 + 0 + 4294967295 + 4294967295, above 2^32 - 1. |
| `4 5` | 4294967295 | One edge of the maximum weight. |
| `6 0` | -1 | Node 6 has no out-edges. |
| `0 7` | -1 | Node 7 has no in-edges. |
| `1 0` | 8 | `1->2->0` is 3 + 5; edge `2 0 5` has no reverse, so `0 2` and `2 0` differ. |
| `2 1` | 2 | A duplicate query is answered again. |

## 5. Sizes

Measured from the generator, seed 1.

| Scale | Nodes `n` | Edges `m` | Queries | `graph.txt` | `queries.txt` | `distances.txt` |
|---|---|---|---|---|---|---|
| `full` | 1,740,512 | 5,000,000 | 2,000 | 91,950,247 B | 29,401 B | 9,039 B |
| `sample` | 174,051 | 500,000 | 200 | 8,194,650 B | 2,535 B | 908 B |
| `tiny` | 348 | 1,000 | 40 | 11,056 B | 296 B | 181 B |

`sample` is 10% of `full` in nodes, edges and queries, from the same distribution.

SHA-256, seed 1:

| File | `full` | `sample` |
|---|---|---|
| `graph.txt` | `fe6471f2712d10b23c2ec58a4cbc7db1ea3e742daf6b6db62829e74d6c4a26c7` | `16351d408f1f94a6bcf66de5c5762abede111c07a4a38306253504a6c9c9b7cd` |
| `queries.txt` | `160f1ef48378dcb567a8d9ddd2676ee777b044a5ede3ad4c7b42e812aa4b3a6f` | `fb4065ffe94d2dd51c924f389bd0c6afa58d42534391aa5b0fe5edfaa33988a5` |
| `distances.txt` (expected) | `a23dbdf0a3e49cc8901310a9ee1dc6a9a569a85297b6da75c2a7002cb2ed277e` | `a4c76ed030bc7e365db33191f6cf74708fba7f25eb48d64c8c4279f2e57046a0` |

Structure per scale:

| | `full` | `sample` | `tiny` |
|---|---|---|---|
| Main-body nodes (one strongly connected component) | 1,740,349 | 174,031 | 328 |
| Isolated nodes | 17 | 2 | 2 |
| Nodes with no in-edges | 17 | 2 | 2 |
| Nodes with no out-edges (incl. one per island) | 25 | 3 | 3 |
| Islands | 8 | 1 | 1 |
| Injected parallel groups (main body + 2 per island) | 5,016 | 502 | 4 |
| Injected exact duplicate lines | 1,000 | 100 | 1 |
| Self-loops (main body + 1 per island) | 508 | 51 | 2 |
| Weight-0 edges injected (main body + 2 per island) | 2,516 | 252 | 3 |

A lighter parallel copy can also have weight 0, so the file can hold a few more weight-0 lines than the last row says.

Query mix per scale:

| Kind | `full` | `sample` | `tiny` |
|---|---|---|---|
| normal | 1,950 | 185 | 26 |
| `s == t` (main body, isolated) | 4 + 2 | 1 + 1 | 1 + 1 |
| zero path | 2 | 1 | 1 |
| parallel min | 4 | 2 | 2 |
| max weight, over uint32 | 2 + 2 | 1 + 1 | 1 + 1 |
| from no-in node, to no-out node | 2 + 2 | 1 + 1 | 1 + 1 |
| unreachable (island, no-out source, isolated source, no-in target) | 4 + 2 + 2 + 2 | 1 + 1 + 1 + 1 | 1 + 1 + 1 + 1 |
| duplicate | 20 | 2 | 1 |
| **answers `-1`** | **10** | **4** | **4** |

## 6. Banned libraries

Per site rule 02, a library that solves the whole task is banned. For this track that is any library or module that computes shortest paths, distances or route-planning indexes for you. By name, and not limited to:

- **Python:** NetworkX, python-igraph, graph-tool, rustworkx (retworkx), NetworKit, scipy.sparse.csgraph, cuGraph, Pathfinding (`pathfinding`), OSMnx.
- **C and C++:** Boost Graph Library (BGL), LEMON, igraph, SNAP, NetworKit, RoutingKit, OSRM, Valhalla, the graph module of Google OR-Tools.
- **Rust:** the algorithm modules of petgraph (`petgraph::algo`, including `dijkstra`, `astar`, `bellman_ford`, `floyd_warshall`, `k_shortest_path`), fast_paths, the `pathfinding` crate, rustworkx-core.
- **JVM:** GraphHopper, JGraphT, JUNG, Neo4j Graph Data Science.
- **Go, Julia and others:** gonum `graph/path`, Graphs.jl (and LightGraphs.jl), pgRouting, any database's built-in shortest-path function.

Also banned: any library that builds contraction hierarchies, hub labels, transit-node routing or another shortest-path index for you.

Allowed, and counted on the dependency axis like any other dependency: plain data structures (heaps and priority queues, including radix heaps, hash maps, bitsets, arenas, allocators), parsing helpers, and parallelism runtimes. Graph *containers* are allowed as long as no shortest-path or indexing algorithm from them is called; petgraph's `Graph` type is fine, `petgraph::algo` is not. If you are unsure whether a library is allowed, ask before kickoff.

## 7. Reference behaviour

`reference/d_traverse.py` is single-threaded Python 3.13, standard library only, and slow on purpose.

- It reads `graph.txt` line by line, splits each line and converts the three fields with `int()`.
- It stores the graph as a Python list of per-node lists of `(head, weight)` tuples, appended in file order. Parallel edges, duplicate lines and self-loops are kept as they are.
- For every query it starts a fresh Dijkstra search from `s` with a `heapq` binary heap and a `dict` of tentative distances, skips stale heap entries, and stops when `t` is popped. It builds no index and reuses nothing between queries.
- It searches forward from `s` only, so it settles every node closer to `s` than `t` is, and on an unreachable target it settles everything `s` can reach. Each of the "no-in target" queries settles the whole main body.
- It writes all answers at the end.

Measured at `full`, seed 1, on an Apple M4 Pro with `/usr/bin/time -l`: 46.8 s and 48.2 s wall in two runs (with other jobs on the machine; an earlier run on a quiet machine, same distribution, took 43.0 s), peak RSS 915 MB, identical output both times. About 2 s of that is loading the graph; the rest is queries, of which the two no-in-target queries take about 3 s each. At `sample` it takes 3.3 s and 103 MB. The scoring-box number comes from the calibration run on the box.

## 8. Changes from the site

The site text is in `late-oct-event/src/data/tracks.json`. Differences, with reasons:

1. **Dataset size is 92 MB, not 180 MB.** `graph.txt` is 91,950,247 bytes at `full` (about 18.4 bytes per edge line); 180 was a placeholder. `n`, `m`, the header and the line format are exactly as published.
2. **Reference time and timeout.** The published 72.4 s and 724 s were placeholders. Measured here: about 43 to 48 s on an Apple M4 Pro; the scoring-box time and the 10x timeout follow from the Oct 26-27 calibration.
3. **The preview lines are illustrative.** `0 918442 37`, `918442 0 41`, `1 7 1200` and `1739998 62 9` have the right format and are typical of the data, but they are not lines of the seed-1 file. The first edge lines of the seed-1 `full` file are `1188446 437285 120`, `64107 865467 170`, `472413 359585 122` and `429961 196139 82`, if the site should show real ones.
4. **Weight 0 is legal.** The site says weights are uint32; this spec makes explicit that the range is `[0, 4294967295]`, that 0 occurs, and that sums are not limited to 32 bits.
5. **Things the site does not mention, all defined here:** parallel edges, exact duplicate lines, self-loops, nodes with no edges (they still count in `n`), and unreachable pairs.
6. **Queries and output are defined here.** The site gives only the query count. This spec adds `queries.txt` (2,000 lines `s t`, no header), the query distribution in section 2, and `distances.txt` with `-1` for unreachable.
7. **Scales.** `sample` has 10% of the nodes, edges and queries (200), from the same distribution, so it is a faithful small model of `full` for local checks; `tiny` has 1,000 edges and 40 queries.
