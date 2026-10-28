# Track A: Ingest

## 1. Summary

Read 3,100,000 HTTP request log lines and, for every hour and endpoint that saw traffic, report the request count, the p50, p95 and p99 latency, and the number and rate of 5xx responses. The input is one space-delimited text file covering 72 hours. It is not sorted by time: eight log shards each write in time order, and their chunks are interleaved as they arrive. The output is one text file with one line per (hour, endpoint) group, in a fixed total order, so a correct answer has exactly one SHA-256.

## 2. Input files

One file, `/data/requests.log`.

**Encoding.** ASCII (so also valid UTF-8), no BOM. Every line ends with a single LF, including the last. No CR, no empty lines, no header, no comments. Every line matches the grammar below; a solution may assume well-formed input.

**Grammar** (ABNF, `SP` is one space `0x20`, `LF` is `0x0A`):

```
line      = ts SP method SP endpoint SP status SP latency LF
ts        = 4DIGIT "-" 2DIGIT "-" 2DIGIT "T" 2DIGIT ":" 2DIGIT ":" 2DIGIT "." 6DIGIT "Z"
method    = "GET" / "POST" / "PUT" / "PATCH" / "DELETE" / "HEAD" / "OPTIONS"
endpoint  = "/" *124echar
echar     = ALPHA / DIGIT / "/" / "-" / "_" / "." / "{" / "}" / ":"
status    = ("1" / "2" / "3" / "4" / "5") 2DIGIT
latency   = "0" / (%x31-39 *9DIGIT)
```

| Field | Meaning and range |
|---|---|
| `ts` | Request time, RFC 3339 in UTC, always exactly 27 bytes: microsecond precision (six fractional digits) and a trailing `Z`. Always a real calendar date and time. Every timestamp lies in the same 72 hours for every seed and scale: `2026-10-30T18:00:00.000000Z` to `2026-11-02T17:59:59.999999Z` inclusive. The span crosses two midnights and the October to November month boundary. |
| `method` | One of the seven methods above, 3 to 7 bytes. |
| `endpoint` | A path template such as `/v2/orders/{id}`: 1 to 125 bytes from the set `A-Z a-z 0-9 / - _ . { } :`, starting with `/`. Never contains a space. Placeholders like `{id}` are literal text. |
| `status` | HTTP status, always three digits, 100 to 599. The generator emits 200 201 202 204 301 302 304 400 401 403 404 405 409 410 422 429 499 500 501 502 503 504 599; a solution must handle any value in 100 to 599. |
| `latency` | Microseconds (not milliseconds), unsigned 32-bit: 0 to 4294967295, decimal with no leading zeros. |

**Endpoints.** Each seed has a catalogue of exactly 1,842 distinct endpoint templates. Thirteen are the same for every seed: `/`, `/health`, `/healthz`, `/v2/checkout`, the byte-order family `/v2/orders`, `/v2/orders-export`, `/v2/orders.csv`, `/v2/orders/{id}`, `/v2/orders/{id}/items`, `/v2/ordersByDate`, `/v2/orders_legacy`, `/v2/orderstatus`, and the longest template, `/internal/v1/tenants/{tenant_id}/projects/{project_id}/pipelines/{pipeline_id}/runs/{run_id}/artifacts/{artifact_id}:download` (125 bytes). The other 1,829 are composed from a vocabulary and depend on the seed. Popularity follows a Zipf law with exponent 1 over a seed-dependent ranking, so a handful of endpoints carry most of the traffic and a long tail sees a few requests a day. `full` and `sample` contain all 1,842 endpoints; `tiny` contains a subset.

**Order.** Lines are not sorted. The generator models eight shards, each writing non-decreasing timestamps (ties allowed). Shards 1 to 7 ship a chunk every 1 to 120 seconds of log time, 0 to 30 seconds after it closes; shard 0 uploads each hour as one chunk, 5 to 10 minutes after the hour ends. The file is the chunks in arrival order. The shard is not a field, and **no bound on how far a line can be out of order is promised**: a solution must not rely on one.

**Edge cases.** Each is injected on purpose, at a fixed rate in `sample` and `full` and at least once in `tiny`, for every seed. A *group* is an (hour, endpoint) pair (see the Task section). Rates are per million lines, so `full` gets ten times the `sample` count.

| # | Case | sample / full | tiny |
|---|---|---|---|
| E1 | A group with exactly one request | 100 per million (31 / 310) | 3 |
| E2 | A group whose latencies are exactly 0 and 4294967295 (p50 = 0, p95 = p99 = 4294967295) | 10 per million (3 / 31) | 1 |
| E3 | A group whose statuses are exactly 200, 499, 500 and 599 (2 errors of 4) | 10 per million (3 / 31) | 1 |
| E4 | A timestamp exactly on an hour boundary, `HH:00:00.000000Z`, paired with `HH:59:59.999999Z` of the hour before, written after it | at each of the 71 boundaries, lines/100,000 pairs (3 / 31) | 3 pairs: 2026-10-30T19:00, 2026-11-01T00:00 (day and month rollover), 2026-11-02T17:00 |
| E5 | The first and last instants of the span, `2026-10-30T18:00:00.000000Z` and `2026-11-02T17:59:59.999999Z` | 1 each | 1 each |
| E6 | Latency 0 outside E2 | 100 per million (31 / 310) | 2 |
| E7 | Latency 4294967295 outside E2 | 100 per million (31 / 310) | 2 |
| E8 | Status 499 next to 500 and 599 | E3, plus ordinary traffic (499 and 599 are rare there) | E3 |
| E9 | Equal timestamps on consecutive lines, different requests | 100 per million pairs | 2 pairs |
| E10 | Equal timestamps on lines far apart (two shards) | 100 per million pairs | 2 pairs |
| E11 | Byte-identical duplicate lines, which count as two requests | 50 per million pairs | 1 pair |
| E12 | A line whose hour is earlier than the hour of a line before it | every E4 pair | every E4 pair |
| E13 | All seven methods | 1 each, plus ordinary traffic | 1 each |
| E14 | All thirteen fixed endpoints, including the 1-byte `/` and the 125-byte template, in one shared hour | 1 each, plus ordinary traffic | 1 each |
| E15 | Every catalogue endpoint at least once (1,842 distinct) | 1 each | not applicable |

## 3. Task

1. **Group key.** Each line belongs to the group (hour, endpoint). The hour is the timestamp truncated to the UTC hour: `2026-10-30T18:59:59.999999Z` is in hour 18:00, and `2026-10-30T19:00:00.000000Z` is in hour 19:00. The endpoint is compared as an exact byte string, with no normalisation of case, slashes or placeholders.
2. **Ignored fields.** `method` does not affect the result. `status` matters only for the error count.
3. **count** is the number of lines in the group. Duplicate lines are separate requests and each one counts.
4. **p50, p95, p99** use the nearest-rank method. Sort the group's n latencies ascending, keeping duplicates; the p-th percentile is the value at 1-indexed rank `ceil(p * n / 100)`, which in integer arithmetic is `(p * n + 99) div 100`. Every percentile is therefore one of the input latencies. With n = 1 all three equal the single value. With n = 101, the ranks are 51, 96 and 100.
5. **errors** is the number of lines in the group with 500 ≤ status ≤ 599. A 499 is not an error.
6. **error_ppm** is `floor(errors * 1000000 / count)`, computed in integers: 1 error in 3 requests is `333333`.
7. **Empty and unknown.** Only groups with at least one line are printed; an hour with no traffic for an endpoint produces no line, and nothing is printed for the catalogue as such. There are no unknown keys: every endpoint string that occurs is its own key. An empty `requests.log` (the generator never writes one) gives an empty `summary.txt` of zero bytes.

## 4. Output

One file, `/out/summary.txt`. UTF-8 (in practice ASCII), LF line endings, a newline after every line including the last, no BOM, no header, no trailing spaces. One line per group, eight fields separated by single spaces:

```
hour endpoint count p50 p95 p99 errors error_ppm
```

- `hour` is the group's hour as `YYYY-MM-DDTHH:00:00Z`, for example `2026-10-30T18:00:00Z`.
- `endpoint` is copied byte for byte from the input.
- The six numbers are decimal integers with no leading zeros and no sign. `count` ≥ 1, `0 ≤ errors ≤ count`, `0 ≤ error_ppm ≤ 1000000`.

**Order.** Lines are sorted by `hour` ascending, then by `endpoint` ascending as unsigned bytes, where a proper prefix sorts first. Each (hour, endpoint) appears exactly once, so the order is total. The fixed-width `hour` field makes chronological order the same as byte order. Byte order is not case-insensitive, locale or per-segment order: within one hour, `/v2/orders` < `/v2/orders-export` < `/v2/orders.csv` < `/v2/orders/{id}` < `/v2/orders/{id}/items` < `/v2/ordersByDate` < `/v2/orders_legacy` < `/v2/orderstatus`, and `/v2/rank100` < `/v2/rank20`.

**Worked example.** This `requests.log`:

```text
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
```

gives exactly these bytes in `summary.txt`:

```text
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
```

Reading it: the two `/health` lines at `18:00:00.115332Z` are duplicates and both count, so n = 3 with sorted latencies 62, 62, 4294967295; p50 is rank 2 (62) and p95 and p99 are rank 3. `/v2/checkout` in hour 18 has latencies 0, 1500, 2500, 30000, 204117; p50 is rank 3 (2500); its errors are the 503, the 500 and the 599, not the 499 or the 201, so 3 of 5 is 600000 ppm. The `18:59:59.999999Z` request belongs to hour 18 even though the file lists the `19:00:00.000000Z` one first.

## 5. Sizes

Measured with seed 1 on an Apple M4 Pro, Python 3.13.12. The generator writes the same bytes, at all three scales, on Linux aarch64 in `python:3.13-slim` (Python 3.13.17), and the reference run through `reference/run.py` there gives the same `summary.txt` hashes for `tiny` and `sample`.

| Scale | Lines | `requests.log` bytes | Generation | `requests.log` sha256 |
|---|---|---|---|---|
| full | 3,100,000 | 238,836,234 | 4.5 s | `7ec80ba45bceef51ac7f79d3a14521fc6187b2dcb87b5e5542d08063fe5d962a` |
| sample | 310,000 | 23,870,290 | 0.5 s | `7b22c678acceba2b82120394afe27e827ca4083a677a8e51147defa854c86847` |
| tiny | 1,000 | 76,509 | under 0.1 s | `a99dd14856a3e6e408097dabc1b3ad5ac5a3801fb12f388bacb2802c8591339f` |

| Scale | `summary.txt` lines | `summary.txt` bytes | `summary.txt` sha256 |
|---|---|---|---|
| full | 126,908 | 10,113,024 | `495ca09682b9673ef836a506496177680bb4a3af54255174befddb1f83481cd3` |
| sample | 66,153 | 5,185,225 | `ca6311bbeb536fa55d85265bf6fd3d24cc9586f13de1ac73bd2a23ac3893aac3` |
| tiny | 873 | 68,899 | `c24fa507c963eaeac9224fd8435019b4f4e8c002ece7e32f316681a3957eb92f` |

The reference on `full`: 13.4 s wall time and 237 MB peak RSS on the M4 Pro, measured twice with `/usr/bin/time -l` (13.39 s and 13.44 s, same output hash). The scoring box is calibrated on Oct 26 and 27.

## 6. Banned libraries

Per rule 02, the library that solves the whole task is banned. For this track that means, in any language and through any binding:

- **Dataframe and analytics engines:** pandas, Polars, DuckDB, Apache Arrow compute (pyarrow, Arrow C++ compute and Acero, the arrow-rs compute kernels), Apache DataFusion, ClickHouse (clickhouse-local, chDB), Apache Spark and PySpark, Dask, Modin, Vaex, cuDF, Velox, R data.table and dplyr, Julia DataFrames.jl.
- **SQL engines of any kind, embedded or server:** SQLite, DuckDB, H2, PostgreSQL, MySQL and the like.
- **Command-line tools that compute grouped statistics:** GNU datamash, Miller (`mlr`), xsv, qsv, csvkit.

Everything else is allowed and priced on the D axis: hash maps, sorting and selection routines, parsing and SIMD helpers, date and time libraries, allocators, numpy. Ask the organisers before kickoff if a library is not on this list and you are unsure.

## 7. Reference behaviour

`reference/a_ingest.py` (CPython 3.13, standard library only, single-threaded):

- Reads `requests.log` line by line through Python's text-mode file iterator, which decodes every line into a new string.
- Splits each line on spaces into five strings.
- Parses every timestamp with `datetime.strptime`, an RFC 3339 format with `%z` for the `Z`, then truncates it to the hour with `replace()`. This is most of the run time.
- Keys a dictionary by (hour datetime, endpoint) and appends each latency, as a Python int, to a growing list for that group; a second dictionary counts 5xx responses.
- Keeps every latency until the end of the input, then sorts the group keys, sorts each group's list in full and reads the three percentiles from it.

## 8. Changes from the site

Compared with the Track A entry on https://speeddemon.dev (`late-oct-event/src/data/tracks.json`):

1. **The site's `spec` numbers are the final scoring-box figures.** `dataset_mb` is 239 (238,836,234 bytes for seed 1); `reference_s` is the box-calibrated reference and `timeout_s` is ten times it. The line count, 3.1M, is the binding number; lines of this format average 77 bytes. About 80% of the reference's time is the per-line `strptime`, so with 3.1M lines fixed the time follows the line count rather than the data's shape. (On an Apple M4 Pro the reference runs in about 13.4 s; the scoring box differs.)
2. **The three preview rows** on the site are lines 849 to 851 of the seed-1 sample, unmodified:
   `2026-10-30T18:07:18.569411Z GET /api/v2/products 200 8177`,
   `2026-10-30T18:07:21.136274Z GET /beta/regions 503 81573`,
   `2026-10-30T18:07:27.963350Z PUT /v2/ab_tests 200 6381`.
   The site's example endpoints `/v2/orders/{id}`, `/v2/checkout` and `/health` are in every catalogue.
5. **`status` is narrowed from uint16 to three digits, 100 to 599.**
6. **The task is pinned down where the site is loose:** percentiles are nearest rank, and the error rate is printed as an integer count plus floor parts per million instead of a fraction.
7. **"Monotonic per shard but interleaved across shards"** is kept, with two clarifications: the shard is not a field in the line, and no bound on disorder is promised.
8. **Added detail the site does not give:** the 72-hour span (2026-10-30T18:00Z to 2026-11-02T18:00Z, fixed for every seed) and the eight shards.
9. **"1,842 distinct"** holds for `full` and `sample`. `tiny`, the test scale, has a subset of the catalogue.
