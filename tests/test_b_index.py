"""Tests for Track B (Index): generator, reference and the spec's worked example.

    python3.13 -m unittest discover -s tests -p 'test_b_*.py'
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATOR = os.path.join(ROOT, "generators", "b_index.py")
REFERENCE = os.path.join(ROOT, "reference", "b_index.py")
SPEC = os.path.join(ROOT, "specs", "b-index.md")
PYTHON = sys.executable


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = load("b_index_generator", GENERATOR)
ref = load("b_index_reference", REFERENCE)


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run_generator(seed: int, scale: str, out: str, hash_seed: str = "0") -> None:
    env = dict(os.environ, PYTHONHASHSEED=hash_seed)
    subprocess.run([PYTHON, GENERATOR, "--seed", str(seed), "--scale", scale, "--out", out],
                   check=True, env=env, capture_output=True)


def run_reference(data: str, out: str, hash_seed: str = "0") -> bytes:
    env = dict(os.environ, PYTHONHASHSEED=hash_seed)
    subprocess.run([PYTHON, REFERENCE, "--data", data, "--out", out], check=True, env=env,
                   capture_output=True)
    with open(os.path.join(out, "results.txt"), "rb") as f:
        return f.read()


def solve(docs: bytes, queries: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        with open(os.path.join(data, "docs.txt"), "wb") as f:
            f.write(docs)
        with open(os.path.join(data, "queries.txt"), "wb") as f:
            f.write(queries)
        ref.main(data, os.path.join(tmp, "out"))
        with open(os.path.join(tmp, "out", "results.txt"), "rb") as f:
            return f.read()


# ---------------------------------------------------------------------------
# The worked example from specs/b-index.md, byte for byte.
# ---------------------------------------------------------------------------
EXAMPLE_DOCS = (
    "0\tThe surety posted bail; the BAIL BOND was forfeited.\n"
    "1\tWrit of Habeas-Corpus: see 42 U.S.C. § 1983 (bail bond).\n"
    "2\t\n"
    "3\tZürich bail—bond, bail bail bond. Case 007.\n"
    "4\tIn the matter of the estate. Bond not posted, No. 7.\n"
).encode("utf-8")

EXAMPLE_QUERIES = b"""BOOL bail AND bond
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
"""

EXAMPLE_RESULTS = b"""3 4 0 1 3
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
"""


class WorkedExample(unittest.TestCase):
    def test_reference_matches_worked_example(self):
        self.assertEqual(solve(EXAMPLE_DOCS, EXAMPLE_QUERIES), EXAMPLE_RESULTS)

    def test_spec_shows_the_same_example(self):
        with open(SPEC, encoding="utf-8") as f:
            spec = f.read()
        docs_shown = EXAMPLE_DOCS.decode("utf-8").replace("\t", "<TAB>")
        self.assertIn(docs_shown, spec)
        self.assertIn(EXAMPLE_QUERIES.decode("ascii"), spec)
        self.assertIn(EXAMPLE_RESULTS.decode("ascii"), spec)


# ---------------------------------------------------------------------------
# Hand-written edge cases with hand-computed answers.
# ---------------------------------------------------------------------------
def docs_file(texts: list[str]) -> bytes:
    return "".join(f"{i}\t{t}\n" for i, t in enumerate(texts)).encode("utf-8")


class EdgeCases(unittest.TestCase):
    def check(self, texts: list[str], cases: list[tuple[str, str]]):
        queries = "".join(q + "\n" for q, _ in cases).encode("ascii")
        expected = "".join(a + "\n" for _, a in cases).encode("ascii")
        self.assertEqual(solve(docs_file(texts), queries).decode(), expected.decode())

    def test_tokenizer_byte_rule(self):
        texts = [
            "273K",                 # 0: Kelvin sign, not ASCII K
            "ﬁduciary duty",        # 1: fi ligature
            "bail bond",            # 2: no-break space joins two tokens
            "İSTANBUL",             # 3: dotted capital I is two separator bytes
            "naïve café",      # 4: non-ASCII letters split words
            "Straße \U0001d504ct",  # 5: sharp s, 4-byte character
            "MiXeD CaSe 3RD",            # 6: ASCII upper case is lowered
        ]
        self.check(texts, [
            ("BOOL 273", "1 0 0"),
            ("BOOL 273k", "0 0"),
            ("BOOL duciary", "1 1 1"),
            ("BOOL fiduciary", "0 0"),
            ('PHRASE "bail bond"', "1 2 2"),
            ("BOOL stanbul", "1 3 3"),
            ("BOOL i OR istanbul", "0 0"),
            ('PHRASE "na ve caf"', "1 4 4"),
            ("BOOL naive OR cafe", "0 0"),
            ('PHRASE "stra e ct"', "1 5 5"),
            ("BOOL strasse", "0 0"),
            ('PHRASE "mixed case 3rd"', "1 6 6"),
        ])

    def test_empty_and_separator_only_documents(self):
        texts = ["", "— §§ ...", "bail", "", "bail bond"]
        self.check(texts, [
            ("BOOL bail", "2 6 2 4"),
            ("BOOL bail NOT bond", "1 2 2"),
            ('PHRASE "bail bond"', "1 4 4"),
        ])

    def test_boolean_semantics(self):
        texts = [
            "a b c",        # 0
            "a",            # 1
            "b c",          # 2
            "c",            # 3
            "and or not",   # 4: operator words are ordinary lowercase terms
            "b b b b",      # 5: a term repeated in one document counts once
        ]
        self.check(texts, [
            ("BOOL a OR b AND c", "2 2 0 2"),          # (a OR b) AND c, not a OR (b AND c)
            ("BOOL a AND c OR b", "3 7 0 2 5"),        # (a AND c) OR b
            ("BOOL c NOT a OR a", "4 6 0 1 2 3"),      # (c NOT a) OR a
            ("BOOL b NOT b", "0 0"),
            ("BOOL a NOT a OR c NOT c", "0 0"),        # NOT empties a non-empty set
            ("BOOL missing", "0 0"),
            ("BOOL missing OR a", "2 1 0 1"),
            ("BOOL a AND missing", "0 0"),
            ("BOOL a NOT missing", "2 1 0 1"),
            ("BOOL and AND not", "1 4 4"),
            ("BOOL or", "1 4 4"),
            ("BOOL b", "3 7 0 2 5"),
            ("BOOL a OR b OR c OR and", "6 15 0 1 2 3 4 5"),
        ])

    def test_phrase_semantics(self):
        texts = [
            "bail bail bond",              # 0
            "bail bond bail",              # 1
            "bond. Bail",                  # 2: sentence punctuation does not break adjacency
            "bail",                        # 3: phrases never span documents ...
            "bond",                        # 4: ... so "bail bond" does not match 3+4
            "the bond, the bail",          # 5: "bail the bond" has every term, never in order
            "x x",                         # 6
            "x x x",                       # 7
        ]
        self.check(texts, [
            ('PHRASE "bail bond"', "2 1 0 1"),
            ('PHRASE "bail bail"', "1 0 0"),
            ('PHRASE "bail bail bond"', "1 0 0"),
            ('PHRASE "bond bail"', "2 3 1 2"),
            ('PHRASE "the bond"', "1 5 5"),
            ('PHRASE "bail the bond"', "0 0"),
            ('PHRASE "x x"', "2 13 6 7"),
            ('PHRASE "x x x"', "1 7 7"),
            ('PHRASE "x x x x"', "0 0"),
            ('PHRASE "bail missing"', "0 0"),
        ])

    def test_result_line_lists_ten_smallest_ids(self):
        texts = ["bail"] * 14 + ["bond"] + ["bail"]
        ids = [i for i in range(16) if i != 14]
        self.check(texts, [
            ("BOOL bail", " ".join(map(str, [len(ids), sum(ids)] + ids[:10]))),
            ("BOOL bond", "1 14 14"),
        ])

    def test_digit_tokens_are_strings(self):
        texts = ["No. 2026-cv-00123", "123 and 0123", "1983", "U.S.C. § 1983(b)(6)"]
        self.check(texts, [
            ("BOOL 00123", "1 0 0"),
            ("BOOL 123", "1 1 1"),
            ("BOOL 0123 OR 00123", "2 1 0 1"),
            ('PHRASE "2026 cv 00123"', "1 0 0"),
            ("BOOL 1983", "2 5 2 3"),
            ('PHRASE "u s c 1983 b 6"', "1 3 3"),
        ])


# ---------------------------------------------------------------------------
# Generator and reference determinism, formats, and edge-case coverage.
# ---------------------------------------------------------------------------
QUERY_RE = re.compile(
    rb'(BOOL [a-z0-9]{1,32}( (AND|OR|NOT) [a-z0-9]{1,32}){0,5}'
    rb'|PHRASE "[a-z0-9]{1,32}( [a-z0-9]{1,32}){1,7}")\n')


class GeneratedData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dirs = {}
        for scale in ("tiny", "sample"):
            path = os.path.join(cls.tmp.name, f"{scale}-seed1")
            run_generator(1, scale, path, hash_seed="1")
            cls.dirs[scale] = path

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_generator_is_deterministic(self):
        again = os.path.join(self.tmp.name, "tiny-again")
        run_generator(1, "tiny", again, hash_seed="2")
        for name in ("docs.txt", "queries.txt"):
            self.assertEqual(sha256(os.path.join(self.dirs["tiny"], name)), sha256(os.path.join(again, name)))

    def test_seeds_differ(self):
        other = os.path.join(self.tmp.name, "tiny-seed2")
        run_generator(2, "tiny", other)
        for name in ("docs.txt", "queries.txt"):
            self.assertNotEqual(sha256(os.path.join(self.dirs["tiny"], name)), sha256(os.path.join(other, name)))

    def test_scales_do_not_share_records(self):
        with open(os.path.join(self.dirs["tiny"], "docs.txt"), "rb") as f:
            tiny = f.read().split(b"\n")[:50]
        with open(os.path.join(self.dirs["sample"], "docs.txt"), "rb") as f:
            sample = f.read().split(b"\n")[:50]
        self.assertEqual(sum(a == b for a, b in zip(tiny, sample)), 0)

    def test_reference_is_deterministic(self):
        out1 = run_reference(self.dirs["tiny"], os.path.join(self.tmp.name, "out1"), hash_seed="1")
        out2 = run_reference(self.dirs["tiny"], os.path.join(self.tmp.name, "out2"), hash_seed="2")
        self.assertEqual(out1, out2)
        self.assertTrue(out1.endswith(b"\n") and b"\r" not in out1 and b" \n" not in out1)
        self.assertEqual(out1.count(b"\n"), gen.SCALES["tiny"][1])

    def test_tokenizers_agree(self):
        with open(os.path.join(self.dirs["tiny"], "docs.txt"), "rb") as f:
            data = f.read()
        data += "Kﬁİ A-Z a_z 0 9 \xff".encode("utf-8")
        self.assertEqual(gen.tokenize(data), ref.tokenize(data))

    def test_file_formats(self):
        for scale, path in self.dirs.items():
            with self.subTest(scale=scale):
                n_docs, n_queries = gen.SCALES[scale]
                with open(os.path.join(path, "docs.txt"), "rb") as f:
                    docs = f.read()
                docs.decode("utf-8")  # valid UTF-8
                self.assertNotIn(b"\r", docs)
                lines = docs.split(b"\n")
                self.assertEqual(lines[-1], b"")
                self.assertEqual(len(lines) - 1, n_docs)
                for i, line in enumerate(lines[:-1]):
                    id_field, tab, text = line.partition(b"\t")
                    self.assertEqual((id_field, tab), (str(i).encode(), b"\t"))
                    self.assertNotIn(b"\t", text)
                self.assertLessEqual(max(map(len, ref.tokenize(docs))), 32)
                with open(os.path.join(path, "queries.txt"), "rb") as f:
                    queries = f.readlines()
                self.assertEqual(len(queries), n_queries)
                for q in queries:
                    self.assertRegex(q, QUERY_RE)
                n_bool = sum(q.startswith(b"BOOL ") for q in queries)
                self.assertEqual(n_bool, n_queries * 62 // 100)

    def test_every_edge_case_is_present(self):
        for scale, path in self.dirs.items():
            with self.subTest(scale=scale):
                missing = sorted(set(EDGE_CASES) - edge_cases_found(path))
                self.assertEqual(missing, [])


EDGE_CASES = [
    # corpus side
    "empty_doc", "separator_only_doc", "non_ascii_bytes", "uppercase", "all_caps_doc",
    "digits", "zero_padded_digits", "punct_joined", "term_repeated_in_doc", "repeated_run",
    # query side, each one changing an answer
    "bool_single", "bool_absent", "bool_trap_absent", "phrase_absent", "bool_not_empty",
    "bool_or_chain_common", "bool_op_words", "bool_left_assoc_matters", "phrase_repeated_token",
    "phrase_positions_matter", "answer_needs_lowercasing", "answer_needs_byte_rule",
    "answer_needs_punct_split", "answer_needs_digit_token", "answer_needs_zero_padding",
    "answer_counts_repeat_once", "phrase_crosses_sentence",
]


def precedence_eval(index, terms, ops):
    """Standard precedence (AND and NOT bind tighter than OR), for comparison only."""
    groups, current = [], index.get(terms[0], ref.EMPTY)
    for op, term in zip(ops, terms[1:]):
        postings = index.get(term, ref.EMPTY)
        if op == b"OR":
            groups.append(current)
            current = postings
        elif op == b"AND":
            current = current & postings
        else:
            current = current - postings
    groups.append(current)
    result = set()
    for g in groups:
        result |= g
    return result


def edge_cases_found(path: str) -> set[str]:
    found = set()
    texts, index = ref.load_corpus(os.path.join(path, "docs.txt"))
    n = len(texts)
    lowered = [t.lower() for t in texts]
    all_caps = set()
    for i, text in enumerate(texts):
        tokens = ref.tokenize(text)
        if text == b"":
            found.add("empty_doc")
        elif not tokens:
            found.add("separator_only_doc")
        if re.search(rb"[\x80-\xff]", text):
            found.add("non_ascii_bytes")
        if re.search(rb"[A-Z]", text):
            found.add("uppercase")
            if re.search(rb"[A-Z]{3}", text) and not re.search(rb"[a-z]", text):
                all_caps.add(i)
                found.add("all_caps_doc")
        if re.search(rb"[a-z0-9][-/'.][a-z0-9]", lowered[i]):
            found.add("punct_joined")
        if len(tokens) != len(set(tokens)):
            found.add("term_repeated_in_doc")
        if re.search(rb"(?<![a-z0-9])([a-z]{3,}) \1(?![a-z0-9])", lowered[i]):
            found.add("repeated_run")
    for token in index:
        if re.search(rb"[0-9]", token):
            found.add("digits")
        if re.fullmatch(rb"0[0-9]+", token):
            found.add("zero_padded_digits")

    traps = {t.encode() for t in gen.TRAP_TERMS}
    with open(os.path.join(path, "queries.txt"), "rb") as f:
        queries = [q.rstrip(b"\n") for q in f]
    for q in queries:
        kind, _, rest = q.partition(b" ")
        if kind == b"BOOL":
            words = rest.split(b" ")
            terms, ops = words[0::2], words[1::2]
            result = ref.run_bool(index, words)
            if len(terms) == 1:
                found.add("bool_single")
            if any(t not in index for t in terms):
                found.add("bool_absent")
            if any(t in traps and t not in index for t in terms):
                found.add("bool_trap_absent")
            if any(t in (b"and", b"or", b"not") for t in terms):
                found.add("bool_op_words")
            if len(terms) >= 3 and all(o == b"OR" for o in ops) and \
                    all(len(index.get(t, ())) * 100 > n for t in terms):
                found.add("bool_or_chain_common")
            running = index.get(terms[0], ref.EMPTY)
            for op, term in zip(ops, terms[1:]):
                postings = index.get(term, ref.EMPTY)
                if op == b"AND":
                    after = running & postings
                elif op == b"OR":
                    after = running | postings
                else:
                    after = running - postings
                if op == b"NOT" and running and not after:
                    found.add("bool_not_empty")
                running = after
            if precedence_eval(index, terms, ops) != set(result):
                found.add("bool_left_assoc_matters")
            if len(terms) == 1 and any(lowered[d].count(terms[0]) >= 2 and ref.tokenize(texts[d]).count(terms[0]) >= 2
                                       for d in sorted(result)[:50]):
                found.add("answer_counts_repeat_once")
        else:
            terms = rest[1:-1].split(b" ")
            result = ref.run_phrase(index, texts, terms)
            if len(set(terms)) < len(terms):
                found.add("phrase_repeated_token")
            if any(t not in index for t in terms):
                found.add("phrase_absent")
            bag = index.get(terms[0], ref.EMPTY)
            for t in terms[1:]:
                bag = bag & index.get(t, ref.EMPTY)
            if len(result) < len(bag):
                found.add("phrase_positions_matter")
            for d in sorted(result)[:50]:
                for a, b in zip(terms, terms[1:]):
                    if re.search(rb"(?<![a-z0-9])" + a + rb"\. " + b + rb"(?![a-z0-9])", lowered[d]):
                        found.add("phrase_crosses_sentence")
        if not result:
            continue
        sample_docs = sorted(result)[:200]
        if any(d in all_caps for d in sample_docs):
            found.add("answer_needs_lowercasing")
        if any(re.search(rb"[0-9]", t) for t in terms):
            found.add("answer_needs_digit_token")
        if any(re.fullmatch(rb"0[0-9]+", t) for t in terms):
            found.add("answer_needs_zero_padding")
        for d in sample_docs:
            text = lowered[d]
            for t in terms:
                if re.search(rb"[\x80-\xff]" + t + rb"(?![a-z0-9])|(?<![a-z0-9])" + t + rb"[\x80-\xff]", text):
                    found.add("answer_needs_byte_rule")
            for a, b in zip(terms, terms[1:]):
                if re.search(rb"(?<![a-z0-9])" + a + rb"[-/'.]" + b + rb"(?![a-z0-9])", text):
                    found.add("answer_needs_punct_split")
    return found


if __name__ == "__main__":
    unittest.main()
