"""Speed Demon 2026, Track C (Join): tests for the generator, the reference and the spec.

    python3.13 -m unittest discover -s tests -p 'test_c_*.py'

Three kinds of check:
  * hand-written inputs with hand-computed output bytes (one of them is the
    worked example in specs/c-join.md, byte for byte);
  * determinism of the generator and of the reference;
  * every generated file (tiny and sample) is parsed by an independent,
    csv-module-free implementation of the spec grammar, which asserts that
    the file uses only the features the spec allows, that every listed edge
    case is present, and that its own aggregation matches the reference.
"""

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATOR = os.path.join(ROOT, "generators", "c_join.py")
REFERENCE = os.path.join(ROOT, "reference", "c_join.py")
PYTHON = sys.executable


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load("c_join_generator", GENERATOR)
ref = _load("c_join_reference", REFERENCE)

BOM = b"\xef\xbb\xbf"
CRLF, LF = b"\r\n", b"\n"
HEADER_FIELDS = [b"id", b"name", b"note", b"region_id", b"amount"]


# --------------------------------------------------------------------------
# An independent implementation of the spec: bytes in, records out, no csv.
# --------------------------------------------------------------------------

Rec = namedtuple("Rec", "fields quoted term bom")
FIELD = re.compile(rb'"((?:[^"]|"")*)"|([^,"\r\n]*)')
AMOUNT = re.compile(rb"(-?)([0-9]+)\.([0-9]{2})")


def strict_parse(data):
    """Split records.csv bytes into records by the spec's parsing rules."""
    recs, pos, end = [], 0, len(data)
    while pos < end:
        bom = data.startswith(BOM, pos)          # one U+FEFF at a record start
        if bom:
            pos += 3
        fields, quoted = [], []
        for col in range(5):
            m = FIELD.match(data, pos)
            if m.group(1) is not None:
                fields.append(m.group(1).replace(b'""', b'"'))
                quoted.append(True)
            else:
                fields.append(m.group(2))
                quoted.append(False)
            pos = m.end()
            if col < 4:
                if data[pos:pos + 1] != b",":
                    raise ValueError(f"expected ',' at byte {pos}")
                pos += 1
        if data.startswith(CRLF, pos):
            term = CRLF
        elif data.startswith(LF, pos):
            term = LF
        elif pos == end:
            term = b""                           # last record, no terminator
        else:
            raise ValueError(f"expected a record terminator at byte {pos}")
        pos += len(term)
        recs.append(Rec(fields, quoted, term, bom))
    if not recs or recs[0].fields != HEADER_FIELDS or any(recs[0].quoted):
        raise ValueError("bad header")
    return recs


def parse_regions(data):
    lines = data.split(b"\n")
    if lines[0] != b"region_id,country" or lines[-1] != b"":
        raise ValueError("bad regions.csv")
    regions = {}
    for line in lines[1:-1]:
        region_id, country = line.split(b",")
        regions[int(region_id)] = country
    return regions


def cents_of(amount):
    sign, whole, frac = AMOUNT.fullmatch(amount).groups()
    cents = int(whole) * 100 + int(frac)
    return -cents if sign else cents


def money(cents):
    sign = b"-" if cents < 0 else b""
    return sign + b"%d.%02d" % divmod(abs(cents), 100)


def expected_output(recs, regions):
    """The spec's aggregation, written independently of the reference."""
    acc = {}
    for rec in recs[1:]:
        rid, name, note, region, amount = rec.fields
        key = regions.get(int(region), b"_UNMATCHED")
        a = acc.setdefault(key, {"count": 0, "ids": 0, "sum": 0, "min": None,
                                 "max": None, "names": set(), "nb": 0, "tb": 0})
        cents = cents_of(amount)
        a["count"] += 1
        a["ids"] += int(rid)
        a["sum"] += cents
        a["min"] = cents if a["min"] is None else min(a["min"], cents)
        a["max"] = cents if a["max"] is None else max(a["max"], cents)
        a["names"].add(name)
        a["nb"] += len(name)
        a["tb"] += len(note)
    out = []
    for key in sorted(acc):                      # bytes sort is bytewise
        a = acc[key]
        out.append(b" ".join([key, b"%d" % a["count"], b"%d" % a["ids"], money(a["sum"]),
                              money(a["min"]), money(a["max"]), b"%d" % len(a["names"]),
                              b"%d" % a["nb"], b"%d" % a["tb"]]) + b"\n")
    return b"".join(out)


def run_reference(records, regions):
    with tempfile.TemporaryDirectory() as tmp:
        data, out = os.path.join(tmp, "data"), os.path.join(tmp, "out")
        os.makedirs(data)
        with open(os.path.join(data, "records.csv"), "wb") as f:
            f.write(records)
        with open(os.path.join(data, "regions.csv"), "wb") as f:
            f.write(regions)
        ref.main(data, out)
        assert os.listdir(out) == ["aggregates.txt"]
        with open(os.path.join(out, "aggregates.txt"), "rb") as f:
            return f.read()


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# --------------------------------------------------------------------------
# Hand-written inputs, hand-computed outputs.
# --------------------------------------------------------------------------

# The worked example in specs/c-join.md. Its first five lines are the site's
# preview, byte for byte.
WORKED_RECORDS = (
    BOM + b"id,name,note,region_id,amount\r\n"
    b'1,"Kaphar, Titus","said ""no"" twice",7,1420.00\r\n'
    b'2,"Betts","a note that\nspans two lines",7,88.50\n'
    b'3,,"",12,0.00\r\n'
    b'4,"trailing space ","comma, quote "" and newline\ntogether",3,-5.25\n'
    + BOM + b'5,"Betts ","one\ntwo\nthree",12,-0.75\r\n'
    b'6,"",,12,10.00\r\n'
    + BOM + b'7,"Ann ""Annie"" Lee","x",5,2.00\n'
    b'8,"Nobody","",4000000,-1.00\r\n'
    b'9,"Neg","n",20,-0.05\r\n'
)
WORKED_REGIONS = b"region_id,country\n3,BBB\n7,AAA\n12,AAA\n20,CCC\n21,DDD\n"
WORKED_OUTPUT = (
    b"AAA 5 17 1517.75 -0.75 1420.00 4 24 55\n"
    b"BBB 1 4 -5.25 -5.25 -5.25 1 15 35\n"
    b"CCC 1 9 -0.05 -0.05 -0.05 1 3 1\n"
    b"_UNMATCHED 2 15 1.00 -1.00 2.00 2 21 1\n"
)

# Rules the spec defines but the generator never exercises: no BOM at the
# start, U+FEFF inside a quoted field is data (3 bytes), and a last record
# that ends at end of file. The variants below must not change the output.
RULES_RECORDS = (
    b"id,name,note,region_id,amount\n"
    b'1,"a' + BOM + b'b","x\n' + BOM + b'y",7,1.00\n'
    b'2,"' + BOM + b'","",7,-1.00\r\n'
    b'4,"d","",8,5.00\n'
    b'5,"d ","",8,-5.00\r\n'
    + BOM + b'3,"c",,7,0.50'
)
RULES_REGIONS = b"region_id,country\n7,AAA\n8,BBB\n"
RULES_OUTPUT = (
    b"AAA 3 6 0.50 -1.00 1.00 3 9 6\n"
    b"BBB 2 9 0.00 -5.00 5.00 2 3 0\n"
)


class HandWrittenCases(unittest.TestCase):
    def test_worked_example(self):
        self.assertEqual(run_reference(WORKED_RECORDS, WORKED_REGIONS), WORKED_OUTPUT)

    def test_worked_example_independent_parser(self):
        recs = strict_parse(WORKED_RECORDS)
        self.assertEqual(expected_output(recs, parse_regions(WORKED_REGIONS)), WORKED_OUTPUT)

    def test_worked_example_starts_with_the_site_preview(self):
        preview = [  # tracks.json, with the site's markers turned back into bytes
            "⟨BOM⟩id,name,note,region_id,amount␍␊",
            '1,"Kaphar, Titus","said ""no"" twice",7,1420.00␍␊',
            '2,"Betts","a note that␊spans two lines",7,88.50␊',
            '3,,"",12,0.00␍␊',
            '4,"trailing space ","comma, quote "" and newline␊together",3,-5.25␊',
        ]
        raw = "".join(preview).replace("⟨BOM⟩", "﻿").replace("␍", "\r").replace("␊", "\n")
        self.assertTrue(WORKED_RECORDS.startswith(raw.encode("utf-8")))

    def test_rules_the_generator_never_exercises(self):
        variants = {
            "as written": RULES_RECORDS,
            "leading BOM": BOM + RULES_RECORDS,
            "final LF": RULES_RECORDS + LF,
            "final CRLF": RULES_RECORDS + CRLF,
        }
        regions = parse_regions(RULES_REGIONS)
        for label, records in variants.items():
            with self.subTest(label):
                self.assertEqual(run_reference(records, RULES_REGIONS), RULES_OUTPUT)
                self.assertEqual(expected_output(strict_parse(records), regions), RULES_OUTPUT)

    def test_ordering_is_bytewise_and_unmatched_sorts_last(self):
        records = (BOM + b"id,name,note,region_id,amount\r\n"
                   b'1,"x","",1,1.00\n2,"x","",2,1.00\n3,"x","",3,1.00\n4,"x","",9,1.00\r\n')
        regions = b"region_id,country\n1,ZZZ\n2,AAB\n3,AAA\n"
        self.assertEqual(run_reference(records, regions), (
            b"AAA 1 3 1.00 1.00 1.00 1 1 0\n"
            b"AAB 1 2 1.00 1.00 1.00 1 1 0\n"
            b"ZZZ 1 1 1.00 1.00 1.00 1 1 0\n"
            b"_UNMATCHED 1 4 1.00 1.00 1.00 1 1 0\n"))

    def test_money_formatting(self):
        cases = {0: "0.00", 5: "0.05", -5: "-0.05", 99: "0.99", -100: "-1.00",
                 -525: "-5.25", 142000: "1420.00", 123456789: "1234567.89"}
        for cents, text in cases.items():
            self.assertEqual(ref.format_cents(cents), text)
            self.assertEqual(ref.parse_cents(text), cents)
        self.assertEqual(ref.parse_cents("-0.00"), 0)
        self.assertEqual(ref.parse_cents("007.50"), 750)
        for bad in ("1.5", "1.500", ".50", "+1.00", " 1.00", "1,00", "", "-", "1e2"):
            with self.assertRaises(ValueError):
                ref.parse_cents(bad)
        for bad in ("+5", " 5", "1_0", "٣", ""):
            with self.assertRaises(ValueError):
                ref.parse_int(bad)


# --------------------------------------------------------------------------
# Determinism.
# --------------------------------------------------------------------------

class Determinism(unittest.TestCase):
    def _generate(self, seed, scale, out, hashseed):
        env = dict(os.environ, PYTHONHASHSEED=str(hashseed))
        subprocess.run([PYTHON, GENERATOR, "--seed", str(seed), "--scale", scale, "--out", out],
                       check=True, env=env)
        return {name: sha256(os.path.join(out, name)) for name in sorted(os.listdir(out))}

    def test_generator_is_deterministic_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._generate(1, "tiny", os.path.join(tmp, "a"), hashseed=1)
            b = self._generate(1, "tiny", os.path.join(tmp, "b"), hashseed=2)
            self.assertEqual(sorted(a), ["records.csv", "regions.csv"])
            self.assertEqual(a, b)

    def test_seeds_and_scales_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = self._generate(1, "tiny", os.path.join(tmp, "1"), hashseed=0)
            two = self._generate(2, "tiny", os.path.join(tmp, "2"), hashseed=0)
            for name in one:
                self.assertNotEqual(one[name], two[name], name)
        # Different scales draw from different streams, so sample is not a
        # prefix of full (and does not share its lookup) for the same seed.
        heads = {scale: [gen.make_rng(1, scale).random() for _ in range(4)]
                 for scale in ("full", "sample", "tiny")}
        self.assertEqual(len({tuple(h) for h in heads.values()}), 3)
        self.assertEqual(heads["full"], [gen.make_rng(1, "full").random() for _ in range(4)])

    def test_reference_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "data")
            gen.generate(1, "tiny", data)
            outs = []
            for run in ("a", "b"):
                out = os.path.join(tmp, run)
                subprocess.run([PYTHON, REFERENCE, "--data", data, "--out", out], check=True,
                               env=dict(os.environ, PYTHONHASHSEED=str(len(outs) + 7)))
                self.assertEqual(os.listdir(out), ["aggregates.txt"])
                outs.append(sha256(os.path.join(out, "aggregates.txt")))
            self.assertEqual(outs[0], outs[1])


# --------------------------------------------------------------------------
# Generated data: conformance, edge-case presence, reference cross-check.
# --------------------------------------------------------------------------

EDGE_CASES = (
    # encoding
    "bom_at_file_start", "bom_mid_file", "bom_after_crlf", "bom_after_lf",
    "crlf_record_after_bom", "lf_record_after_bom", "crlf_terminator", "lf_terminator",
    "name_comma", "note_comma", "name_escaped_quote", "note_escaped_quote",
    "note_two_lines", "note_several_lines", "note_comma_quote_lf",
    "name_empty_unquoted", "name_empty_quoted", "note_empty_unquoted", "note_empty_quoted",
    "name_trailing_space", "last_record_crlf",
    # values and the join
    "negative_amount", "zero_amount", "negative_amount_under_one",
    "unknown_region_in_range", "unknown_region_above_range",
    "trailing_space_twins_in_one_country", "both_empty_names_in_one_country",
    "country_without_records", "country_all_negative_under_one",
)

ID = re.compile(rb"[1-9][0-9]*")
EMITTED_AMOUNT = re.compile(rb"-?(?:0|[1-9][0-9]*)\.[0-9]{2}")
REGION_LINE = re.compile(rb"[1-9][0-9]*,[A-Z]{3}")


def check_conformance(test, data, recs, regions_data):
    """Only the features the spec lists, and the generator's own guarantees."""
    test.assertTrue(data.startswith(BOM + b"id,name,note,region_id,amount\r\n"))
    test.assertTrue(data.endswith(CRLF))
    test.assertEqual(recs[-1].term, CRLF)
    boms = sum(r.bom for r in recs)
    # Every byte >= 0x80 belongs to a BOM at a record start; every CR to a CRLF terminator.
    test.assertEqual(len(re.findall(rb"[\x80-\xff]", data)), 3 * boms)
    test.assertEqual(data.count(b"\r"), sum(r.term == CRLF for r in recs))
    test.assertIsNone(re.search(rb"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]", data))
    test.assertFalse(recs[1].bom, "a mid-file BOM never directly follows the header")
    last_id = 0
    for rec in recs[1:]:
        rid, name, note, region, amount = rec.fields
        test.assertNotEqual(rec.fields, HEADER_FIELDS, "the header appears once")
        test.assertEqual(rec.quoted[0] or rec.quoted[3] or rec.quoted[4], False)
        test.assertTrue(ID.fullmatch(rid) and ID.fullmatch(region), rec)
        test.assertTrue(EMITTED_AMOUNT.fullmatch(amount) and amount != b"-0.00", rec)
        test.assertGreater(int(rid), last_id, "ids increase")
        last_id = int(rid)
        for value, quoted in ((name, rec.quoted[1]), (note, rec.quoted[2])):
            test.assertEqual(quoted or value == b"", True, "non-empty text is always quoted")
            test.assertFalse(value.startswith(b" "), rec)
        test.assertNotIn(b"\n", name)
        test.assertFalse(note.startswith(b"\n") or note.endswith(b"\n"), rec)
        test.assertNotIn(b"\n\n", note)
        test.assertNotIn(b"\n ", note)
    test.assertTrue(regions_data.startswith(b"region_id,country\n") and regions_data.endswith(b"\n"))
    lines = regions_data.split(b"\n")[1:-1]
    test.assertTrue(all(REGION_LINE.fullmatch(line) for line in lines))
    ids = [int(line.split(b",")[0]) for line in lines]
    test.assertEqual(ids, sorted(set(ids)), "region ids are unique and ascending")


def edge_cases_present(recs, regions):
    found = set()
    max_region = max(regions)
    for i, rec in enumerate(recs):
        found.add("crlf_terminator" if rec.term == CRLF else "lf_terminator")
        if rec.bom and i == 0:
            found.add("bom_at_file_start")
        elif rec.bom:
            found.add("bom_mid_file")
            found.add("bom_after_crlf" if recs[i - 1].term == CRLF else "bom_after_lf")
            found.add("crlf_record_after_bom" if rec.term == CRLF else "lf_record_after_bom")
    if recs[-1].term == CRLF:
        found.add("last_record_crlf")
    names, empties, amounts = {}, {}, {}
    for rec in recs[1:]:
        rid, name, note, region, amount = rec.fields
        for col, value, quoted in (("name", name, rec.quoted[1]), ("note", note, rec.quoted[2])):
            if value == b"":
                found.add(f"{col}_empty_{'quoted' if quoted else 'unquoted'}")
            if b"," in value:
                found.add(f"{col}_comma")
            if b'"' in value:
                found.add(f"{col}_escaped_quote")
        if name.endswith(b" "):
            found.add("name_trailing_space")
        lfs = note.count(b"\n")
        if lfs == 1:
            found.add("note_two_lines")
        if lfs >= 2:
            found.add("note_several_lines")
        if lfs and b"," in note and b'"' in note:
            found.add("note_comma_quote_lf")
        cents = cents_of(amount)
        if cents == 0:
            found.add("zero_amount")
        if cents < 0:
            found.add("negative_amount")
        if -100 < cents < 0:
            found.add("negative_amount_under_one")
        region = int(region)
        if region not in regions:
            found.add("unknown_region_in_range" if region < max_region else "unknown_region_above_range")
        key = regions.get(region, b"_UNMATCHED")
        names.setdefault(key, set()).add(name)
        if name == b"":
            empties.setdefault(key, set()).add(rec.quoted[1])
        amounts.setdefault(key, []).append(cents)
    for key, values in names.items():
        if any(v.endswith(b" ") and v.rstrip(b" ") in values for v in values):
            found.add("trailing_space_twins_in_one_country")
    if any(len(forms) == 2 for forms in empties.values()):
        found.add("both_empty_names_in_one_country")
    if set(regions.values()) - set(names):
        found.add("country_without_records")
    if any(-100 < max(v) < 0 for k, v in amounts.items() if k != b"_UNMATCHED"):
        found.add("country_all_negative_under_one")
    return found


class GeneratedData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sets = {}
        for scale in ("tiny", "sample"):
            data = os.path.join(cls.tmp.name, scale)
            gen.generate(1, scale, data)
            with open(os.path.join(data, "records.csv"), "rb") as f:
                records = f.read()
            with open(os.path.join(data, "regions.csv"), "rb") as f:
                regions_data = f.read()
            out = os.path.join(cls.tmp.name, scale + "-out")
            ref.main(data, out)
            with open(os.path.join(out, "aggregates.txt"), "rb") as f:
                output = f.read()
            cls.sets[scale] = (records, strict_parse(records), regions_data,
                               parse_regions(regions_data), output)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_record_counts(self):
        for scale, (_, recs, _, regions, _) in self.sets.items():
            with self.subTest(scale):
                self.assertEqual(len(recs) - 1, gen.SCALES[scale]["records"])
                self.assertEqual(len(regions), gen.SCALES[scale]["regions"])
                self.assertEqual(len(set(regions.values())), gen.SCALES[scale]["countries"])

    def test_conformance(self):
        for scale, (records, recs, regions_data, _, _) in self.sets.items():
            with self.subTest(scale):
                check_conformance(self, records, recs, regions_data)

    def test_every_edge_case_is_present(self):
        for scale, (_, recs, _, regions, _) in self.sets.items():
            with self.subTest(scale):
                missing = set(EDGE_CASES) - edge_cases_present(recs, regions)
                self.assertEqual(missing, set())

    def test_reference_matches_independent_parse(self):
        for scale, (_, recs, _, regions, output) in self.sets.items():
            with self.subTest(scale):
                self.assertEqual(output, expected_output(recs, regions))

    def test_output_format(self):
        num, cash = rb"(?:0|[1-9][0-9]*)", rb"-?(?:0|[1-9][0-9]*)\.[0-9]{2}"
        line = re.compile(rb"(?:[A-Z]{3}|_UNMATCHED)" + b"".join(
            b" " + part for part in (num, num, cash, cash, cash, num, num, num)) + b"\n")
        for scale, (_, _, _, _, output) in self.sets.items():
            with self.subTest(scale):
                lines = output.splitlines(keepends=True)
                self.assertTrue(all(line.fullmatch(x) for x in lines))
                self.assertNotIn(b"-0.00", output)
                keys = [x.split(b" ")[0] for x in lines]
                self.assertEqual(keys, sorted(set(keys)))
                self.assertEqual(keys[-1], b"_UNMATCHED")


if __name__ == "__main__":
    unittest.main()
