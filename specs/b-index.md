# Track B: Index

## 1. Summary

You get a 254 MB corpus of legal-flavoured text, one document per line, and 50,000 queries: 62% boolean (`AND` / `OR` / `NOT`, strictly left to right) and 38% exact phrase queries. Write one line per query: the number of matching documents, the sum of their ids, and the ten smallest ids. Your score is one wall-clock time for the whole run, so reading the corpus, building whatever index you want and answering the queries are all on the same clock. Tokenization is byte-based and ASCII-only so every language can reproduce it exactly. The queries are in the input from the start: the task is to answer these 50,000 queries, and how much of the corpus you index to do that is your call.

## 2. Input files

Both files are in `/data`, read-only.

### 2.1 `docs.txt`

- Valid UTF-8, no BOM, LF line endings, one trailing LF.
- Line `i` (counting from 0) is the decimal id `i`, one TAB byte (0x09), the document text, then LF. Ids are dense and ascending, `0` to `N-1`, with no leading zeros.
- The text is zero or more bytes, none of which is TAB, LF or CR. The generator writes printable ASCII (0x20 to 0x7E) and valid 2-, 3- and 4-byte UTF-8 sequences.
- The text may be empty, in which case the line is the id, a TAB and the LF.

### 2.2 Tokenization

This rule is the whole definition of a token. Apply it to the document text, the bytes after the first TAB and before the LF:

1. ASCII-lowercase: bytes 0x41 to 0x5A (`A` to `Z`) become 0x61 to 0x7A. No other byte changes.
2. A token is a maximal run of bytes in 0x61 to 0x7A (`a` to `z`) or 0x30 to 0x39 (`0` to `9`).
3. Every other byte is a separator. That includes every byte of 0x80 or above, even inside a letter: `Zürich` is the two tokens `z` and `rich`, `naïve` is `na` and `ve`, `273K` written with the KELVIN SIGN (U+212A) is `273`, and `İSTANBUL` is `stanbul`.
4. Tokens are numbered 0, 1, 2, ... in the order they occur. The number of separator bytes between two tokens does not matter.

There is no Unicode case folding or normalisation, no stemming and no stopword removal. Tokens are byte strings, so `007` and `7` are different tokens. `habeas-corpus`, `court's` and `U.S.C.` are 2, 2 and 3 tokens. No token in a generated corpus is longer than 32 bytes (the longest is 17).

In Python this is `re.findall(rb"[a-z0-9]+", text.lower())` on `bytes`, because `bytes.lower()` only touches `A` to `Z`. A tokenizer that decodes UTF-8 and uses Unicode-aware lowercasing or word boundaries gets a different answer on this corpus (`"K".lower()` is `k`, `"İ".lower()` is `i` plus a combining dot), and so does NFKC normalisation (`ﬁ` becomes `fi`).

### 2.3 `queries.txt`

- ASCII, LF line endings, one trailing LF, one query per line.
- Grammar, with single spaces exactly where shown:

```
query   = bool / phrase
bool    = "BOOL" SP term *( SP op SP term )            ; 1 to 6 terms
op      = "AND" / "OR" / "NOT"
phrase  = "PHRASE" SP DQUOTE term 1*( SP term ) DQUOTE  ; 2 to 8 terms
term    = 1*32( %x61-7A / %x30-39 )                     ; a-z and 0-9
```

- There are no parentheses, no leading operator and no empty term. Terms are always lowercase and operators always uppercase, so `and`, `or` and `not` in a term position are ordinary words (`BOOL and AND not` is valid).
- A `BOOL` query may have a single term and no operator.
- The same line can appear more than once. Each occurrence gets its own output line.

### 2.4 How the corpus is generated

Every scale uses the same distribution, and only the counts change. `sample` is not a prefix of `full`, and the holdout seed changes the synthetic vocabulary, the rank order of the legal words and every draw.

- **Vocabulary.** 600,000 words ranked by frequency. Ranks 1 to 122 are English function words in a fixed order (`the`, `of`, `and`, `to`, `in`, ...). 509 legal words (`indemnity`, `surety`, `writ`, `habeas`, `corpus`, `bail`, `bond`, `recognizance`, `forfeiture`, `matter`, ...) sit at ranks 123 to 7,082 in a seed-dependent order. The remaining ranks hold seed-dependent synthetic words of 3 to 12 lowercase letters.
- **Words.** Each word unit is drawn independently with weight proportional to `1 / (r + 1/2)` for rank `r` (Zipf-Mandelbrot, exponent 1).
- **Documents.** A document has 1 to 999 word units: 1 to 9 (5% of documents), 10 to 79 (35%), 80 to 199 (42%), 200 to 399 (16%), 400 to 999 (2%), uniform inside each bucket. About 2% of units are replaced by one of 93 legal phrases (`writ of habeas corpus`, `in the matter of`, `bail bond`, `and/or`, ...) and 1.5% by a number or citation (`1983`, `42 U.S.C. § 1983`, `No. 07-1234`, `$5,000.00`, `3rd`, `Fed. R. Civ. P. 12(b)(6)`, `Velmora v. Trisk`).
- **Text.** Units are joined by a space (86%) or by `, `, `. `, `; `, `: `, ` (`, `) `, `-`, `/`, `'s `, ` - `, ` & `, `... `, quotes, an em dash (U+2014) with or without spaces, a no-break space (U+00A0) or `’s ` (U+2019). The two possessive separators add the token `s`. The first letter of each sentence is capitalised.

Edge cases are injected into a fixed share of documents at every scale, with a minimum count so `tiny` has them too. A document gets at most one of these:

| Injected case | Share of documents | full | sample | tiny |
|---|---|---|---|---|
| Empty text | 0.05% | 125 | 12 | 2 |
| Text with bytes but no token (`§§ ¶`, `* * *`, `—`, ...) | 0.05% | 125 | 12 | 2 |
| 1 to 3 non-ASCII words from a list of 32 (`café`, `Zürich`, `Straße`, `İstanbul`, `273K`, `ﬁduciary`, `bail bond` with a no-break space, `𝔄ct`, `Ñandú`, ...) | 2% | 5,000 | 500 | 20 |
| Whole text in ASCII upper case | 1% | 2,500 | 250 | 10 |
| Whole text in ASCII title case | 1% | 2,500 | 250 | 10 |
| A run of one word repeated 2, 3 or 4 times (`w w w`) | 0.5% | 1,250 | 125 | 6 |
| A zero-padded case number (`Case No. 2026-cv-00123`) | 0.5% | 1,250 | 125 | 6 |
| A punctuation-joined word from a list of 30 (`habeas-corpus`, `and/or`, `court's`, `U.S.C.`, `plaintiff(s)`, `d/b/a`, ...) | 1% | 2,500 | 250 | 10 |

Outside those documents, every document repeats common words, almost every one has capital letters, and hyphens, slashes, apostrophes, em dashes and no-break spaces join words all over the corpus.

### 2.5 How the queries are drawn

The document frequency `df(t)` of a token is the number of documents containing it, and `N` is the document count. Tokens fall into five bands:

| Band | df | Terms at full (seed 1) |
|---|---|---|
| VC | df > N/10 | 112 |
| C | N/100 < df ≤ N/10 | 1,032 |
| M | N/1,000 < df ≤ N/100 | 9,347 |
| U | N/10,000 < df ≤ N/1,000 | 90,640 |
| R | 1 ≤ df ≤ N/10,000 | 508,702 |

When a band is empty (at `tiny`, R always is), the nearest non-empty band is used instead, trying the rarer side first.

**BOOL queries (31,000 at full).** 27,200 of them are ordinary:

- **Terms.** 2 to 6 terms: 2 (38%), 3 (32%), 4 (18%), 5 (8%), 6 (4%).
- **Operators.** Each operator is independently `AND` (50%), `OR` (30%) or `NOT` (20%).
- **Bands.** Each query picks one random document with at least one token as its source. Each term picks a band (VC 3%, C 12%, M 30%, U 30%, R 25%). With probability 75% the term is drawn uniformly from the source document's distinct tokens in that band. Otherwise, or if the source has none in that band, it is drawn uniformly from all corpus tokens in the band.

The other 3,800 are edge cases, at a fixed share of all queries:

| Kind | Share of all queries | full | What it is |
|---|---|---|---|
| single | 2% | 1,000 | one term, no operator |
| absent | 1% | 500 | contains a term that is in no document, half of them normalisation traps such as `zurich`, `cafe`, `strasse`, `273k`, `habeascorpus`, `andor` |
| not_empty | 0.5% | 250 | `x NOT x`, `x AND y NOT x` or `x OR y NOT x NOT y`, so `NOT` empties a non-empty set |
| or_chain | 0.1% | 50 | 3 to 6 terms, each from band VC (70%) or C (30%), joined by `OR` |
| op_words | 0.5% | 250 | the lowercase words `and`, `or`, `not` used as terms |
| left_assoc | 1% | 500 | three terms with mixed operators: `OR AND`, `AND OR`, `OR NOT` or `NOT OR`. For `OR AND` and `OR NOT`, AND-before-OR precedence would usually give a different answer |
| digits | 0.5% | 250 | a zero-padded token such as `00123`, alone, `OR` its unpadded form, or `AND` another token |
| utf8 | 0.5% | 250 | a token cut out of a non-ASCII word (`rich`, `stanbul`, `273`) `AND` an anchor (see below) from the same document, or alone if it has none |
| upper | 0.5% | 250 | two tokens of an all-caps document |
| repeat | 0.5% | 250 | the word of a repeated run, alone or `AND` a token of the same document |
| punct | 0.5% | 250 | the parts of a punctuation-joined word, joined by `AND` |

**PHRASE queries (19,000 at full).** A phrase has 2 to 8 terms: 2 (35%), 3 (30%), 4 (20%), 5 (9%), 6 (4%), 7 (1%), 8 (1%). An anchor is a token with df ≤ N/1,000 (bands U and R). 15,000 phrases are ordinary. Each is a window of consecutive tokens copied from a random document. First an anchor occurrence is picked uniformly among that document's anchors, then the window position uniformly among the windows that contain it. Every ordinary phrase therefore matches at least one document. The rest are edge cases:

| Kind | Share of all queries | full | What it is |
|---|---|---|---|
| scrambled | 4% | 2,000 | an ordinary window, reversed or with two neighbouring tokens swapped, so every term is present but usually not in that order |
| absent | 0.5% | 250 | an ordinary window with one term replaced by an absent term |
| repeat | 1% | 500 | from a repeated run of `w`: `w w`, the whole run, the run plus one more `w`, or the run with one or two neighbouring tokens |
| utf8 | 0.5% | 250 | a window around the tokens of a non-ASCII word (`z rich`, `s o paulo`) |
| upper | 0.5% | 250 | an anchored window from an all-caps document |
| digits | 0.5% | 250 | a window around a zero-padded token |
| punct | 0.5% | 250 | a window across a punctuation-joined word (`habeas corpus` from `habeas-corpus`) |
| cross | 0.5% | 250 | a window across a sentence boundary `. ` |

Every phrase contains an anchor or an absent term, except some of the `repeat` kind. The order of all 50,000 queries is shuffled. `sample` has exactly one tenth of every `full` count above. `tiny` (1,000 queries) has `max(3, share × 1,000)` of each kind.

Measured on the seed-1 `full` queries:

- BOOL terms by band, over all 91,168 term occurrences: VC 3.5%, C 11.5%, M 29.5%, U 30.0%, R 25.0%, absent 0.5%.
- PHRASE queries: 18,658 of 19,000 (98.2%) contain an anchor, 250 contain an absent term, and the other 92 are `repeat` phrases with neither.
- BOOL answers: 23.4% match nothing. Median count is 8, 90th percentile 2,661, 99th percentile 79,880, largest 234,103 of the 250,000 documents.
- PHRASE answers: 12.1% match nothing. Median count is 1, 99th percentile 5, largest 38.

## 3. Task

Answer every query in `queries.txt` against the documents in `docs.txt`. You read both files in the same run, in any order, whenever you like. You do not have to build a full inverted index; indexing only the terms the queries use is a legitimate strategy. Only `results.txt` is checked.

Let `S(t)` be the set of ids of the documents whose token sequence contains token `t` at least once. A term that occurs nowhere has `S(t) = ∅`. Documents are counted, not occurrences: a document that says `bail` five times is one match.

- **BOOL** `t1 op1 t2 op2 t3 ...` starts with `R = S(t1)` and applies each operator to the running result, strictly left to right, with no precedence:
  - `AND t` gives `R ∩ S(t)`;
  - `OR t` gives `R ∪ S(t)`;
  - `NOT t` gives `R \ S(t)`, the documents of `R` that do not contain `t`. `NOT` is always binary.

  So `a OR b AND c` means `(a OR b) AND c`, and `a NOT b OR c` means `(a NOT b) OR c`. A single-term query is `S(t1)`.
- **PHRASE** `"t1 t2 ... tk"` matches document `d` when its tokens contain `t1, t2, ..., tk` at k consecutive positions `p, p+1, ..., p+k-1`. A repeated term must repeat in the document (`"x x x"` needs three consecutive `x`). Whatever separates two tokens in the text, whether a space, `. `, a hyphen, an em dash or a no-break space, is invisible, so a phrase can match across a sentence boundary. A phrase never spans two documents.
- Empty documents and documents with no token match nothing.

## 4. Output

Write `/out/results.txt`: ASCII, LF line endings, one trailing LF, no trailing spaces, exactly one line per query in the same order as `queries.txt`.

```
<count> <sum> <id_1> <id_2> ... <id_m>
```

- `count` is the number of matching documents, `|R|`.
- `sum` is the exact sum of the ids of all matching documents. It is at most `N(N-1)/2`, under 2^35 at `full`, so a 64-bit integer holds it at every scale.
- `id_1 < id_2 < ... < id_m` are the `m = min(10, count)` smallest matching ids, ascending.
- Fields are separated by one space. Numbers are decimal with no leading zeros. A query with no match prints `0 0`.

### Worked example

`docs.txt`, where `<TAB>` stands for the single byte 0x09. `ü` is the bytes C3 BC, `—` is E2 80 94 and `§` is C2 A7. Line 2 is an empty document.

```
0<TAB>The surety posted bail; the BAIL BOND was forfeited.
1<TAB>Writ of Habeas-Corpus: see 42 U.S.C. § 1983 (bail bond).
2<TAB>
3<TAB>Zürich bail—bond, bail bail bond. Case 007.
4<TAB>In the matter of the estate. Bond not posted, No. 7.
```

Their tokens:

| id | tokens |
|---|---|
| 0 | the surety posted bail the bail bond was forfeited |
| 1 | writ of habeas corpus see 42 u s c 1983 bail bond |
| 2 | (none) |
| 3 | z rich bail bond bail bail bond case 007 |
| 4 | in the matter of the estate bond not posted no 7 |

`queries.txt`:

```
BOOL bail AND bond
BOOL bail
BOOL estate OR surety AND bail
BOOL bond NOT bail
BOOL bail NOT bail
BOOL zurich
BOOL rich AND z
BOOL not AND bond
BOOL 7
BOOL the OR writ OR case
PHRASE "bail bond"
PHRASE "bail bail bond"
PHRASE "bond bail"
PHRASE "habeas corpus"
PHRASE "u s c 1983"
PHRASE "estate bond"
PHRASE "forfeited writ"
PHRASE "in the matter of"
```

`results.txt`, exactly:

```
3 4 0 1 3
3 4 0 1 3
1 0 0
1 4 4
0 0
0 0
1 3 3
1 4 4
1 4 4
4 8 0 1 3 4
3 4 0 1 3
1 3 3
1 3 3
1 1 1
1 1 1
1 4 4
0 0
1 4 4
```

Line by line:
- `bail` counts document 3 once although it says `bail` three times.
- `estate OR surety AND bail` is `({4} ∪ {0}) ∩ {0, 1, 3} = {0}`. With AND-before-OR precedence it would be `{0, 4}`.
- `zurich` matches nothing, because `Zürich` is `z rich`.
- `7` does not match `007`.
- `"bond bail"` matches only document 3, where `bond, bail` are adjacent across a comma, although documents 0 and 1 contain both words.
- `"estate bond"` matches across `. `.
- `"forfeited writ"` would need to span documents 0 and 1, so it matches nothing.

## 5. Sizes

Measured with seed 1 on an Apple M4 Pro (Python 3.13.12).

| | full | sample | tiny |
|---|---|---|---|
| Documents | 250,000 | 25,000 | 1,000 |
| `docs.txt` bytes | 253,827,587 | 25,263,400 | 1,000,131 |
| Tokens | 36,276,657 | 3,613,148 | 143,405 |
| Distinct tokens | 609,833 | 378,185 | 46,109 |
| (term, document) pairs | 29,798,541 | 2,971,398 | 117,843 |
| Queries (BOOL / PHRASE) | 50,000 (31,000 / 19,000) | 5,000 (3,100 / 1,900) | 1,000 (620 / 380) |
| `queries.txt` bytes | 1,763,534 | 177,987 | 35,793 |
| `results.txt` bytes | 1,459,301 | 109,450 | 11,919 |
| Generation time | 42.3 s | 4.9 s | 0.9 s |
| Reference wall time | 27.2 s | 1.35 s | 0.06 s |
| Reference peak RSS | 2.29 GB | 344 MB | 40 MB |

SHA-256 of the seed-1 files, to check a local run of the generator and of your solution:

| File | full | sample |
|---|---|---|
| `docs.txt` | `6cd461670d6c39f9c5211162577532d3648edeab12340c71cc7d2ab5f18e66c8` | `e157fabb5874c6058a3f6c7726f51df08b2369664cc76c52dde62335ef57eaa4` |
| `queries.txt` | `77ef7ffc59afde051006b02a2ce8e4c090fbbfbf101acd6f862d0f8e298d39f9` | `930ba8716d7a9b9b962b24f5c9d11b5e0b1efb1b9c24f775f889e50c639f16aa` |
| `results.txt` | `f86973c210228141bcb6f1f1b10188ad2a9b65f71e7cd7cf4590b38de4e80927` | `eeeec1e9319da98a6f3c664c78f93e7078fd603c25c2bb3027174e81e3638a8f` |

The query side grows faster than the corpus. Going from `sample` to `full` multiplies the documents and the queries by 10 each, and posting lists grow about 10 times too, so the work per query grows with them. On the reference the query phase is about 14% of the run at `sample` (0.18 s of 1.25 s) and about 56% at `full` (14.9 s of 26.7 s). The live leaderboard runs on `sample`; final scoring runs on a `full`-size holdout.

## 6. Banned libraries

Per rule 02, the library that solves the whole task is banned. For this track that means any ready-made search engine, information-retrieval library or full-text index, embedded or as a service, including:

- Apache Lucene in any form: Lucene, Lucene.NET, PyLucene, Apache Solr, Elasticsearch, OpenSearch.
- Tantivy (and tantivy-py), Xapian, Bleve, Meilisearch, Typesense, Quickwit, Manticore Search, Sphinx, Vespa, ZincSearch, Sonic, Groonga, Bluge.
- Whoosh, PISA, Terrier, Anserini and Pyserini, Indri, Galago, Lemur, MG4J, ATIRE, JASS.
- Full-text modules of databases: SQLite FTS3, FTS4 and FTS5, PostgreSQL `tsvector`/`tsquery` and `pg_trgm`, MySQL and MariaDB `FULLTEXT`, the DuckDB `fts` extension, RediSearch.
- JavaScript search libraries: Lunr, elasticlunr, FlexSearch, MiniSearch, Fuse.js, Orama.
- A SQL engine (SQLite, DuckDB, PostgreSQL and the like) used to store postings and evaluate the boolean or phrase logic in SQL counts as a search engine.

Everything else is allowed and priced by the D axis: hash maps, sorting, regular expressions, multi-pattern matchers, bitmap and posting-compression libraries (Roaring, StreamVByte, FastPFor), memory-mapped files, thread pools.

## 7. Reference behaviour

`reference/b_index.py`, single-threaded Python, standard library only:

- Reads `docs.txt` line by line, tokenizes each document with the regular expression above, and adds the document id to a Python `set` per distinct term. That is one set for every term in the corpus, 609,833 at `full`. It also keeps every document's raw text in memory.
- Answers `BOOL` queries with Python set operations applied in the order written. Every operation builds a new set.
- Answers `PHRASE` queries by intersecting the terms' sets in phrase order to get candidates, then re-tokenizing each candidate document from its raw text and scanning it for the exact sequence. It stores no positions.
- Formats each answer by sorting the whole result set, then printing its length, its sum and its first ten ids.

At `full` on the M4 Pro it takes 27.2 s wall (27.1 s on a second run): about 11.9 s to read and index the corpus and 14.9 s to answer the queries. Peak RSS is 2.29 GB, under the 4 GB ceiling. Most of that is the posting sets, which hold 29.8 million (term, document) pairs, plus 254 MB of raw text.

## 8. Changes from the site

The site's Track B entry (`src/data/tracks.json`) differs from this spec in these places:

1. **One time, not two.** The site says build and query time are measured separately and combined 40/60 toward queries. The harness times one container run, and there is no trustworthy way to see where a program's build ends and its queries begin, so T is the total wall time. The queries are in the input from the start.
2. **The hint "The build/query split is published in the spec so you can plan the trade"** no longer applies: there is no split to publish. The trade between indexing work and query work now lives inside one run, and this spec publishes the query distribution (section 2.5) so you can plan it.
3. **The sample query `BOOL indemnity AND (nothing) NOT surety`** is `BOOL indemnity AND nothing NOT surety` here. The grammar has no parentheses anywhere, which matches the site's own "no parentheses" note.
4. **"Build an inverted index"** is how most teams will do it, not a requirement. Any method that writes the right `results.txt` is allowed, including indexing only the query terms.
5. **Stopwords** are retained everywhere, in `BOOL` queries as well as phrases. The site only says so for phrases.
6. **The numbers.** The site's `spec` numbers are the final scoring-box figures: `dataset_mb` 256 (a 253.8 MB `docs.txt` plus a 1.8 MB `queries.txt`), the box-calibrated `reference_s`, and `timeout_s` at ten times it. The query count (50,000) and the 62% / 38% mix are unchanged. (On an Apple M4 Pro the reference runs in about 27.2 s; the scoring box differs.)
7. **Added here** because the site does not define them: the corpus format, the tokenizer, the output format, the query distribution and the edge cases.
