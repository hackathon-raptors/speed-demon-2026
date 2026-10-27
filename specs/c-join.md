# Track C: Join

The correctness track. The fast path here is easy and the edge cases are the actual task.

## 1. Summary

Parse `records.csv`, a hostile CSV file of about 200 MB, with quoted commas, escaped quotes, embedded newlines, mixed CRLF and LF line endings, and a UTF-8 byte order mark at the start of some chunks and not others. Join every record against `regions.csv`, a plain lookup table that maps a `region_id` to a three-letter country code. Then write one line of aggregates per country, sorted. Every edge case the file can contain is listed in section 2.5, appears in every dataset at every scale, and is already visible in the sample. The parsing rules in section 2.1 are not negotiable: a parser that is three times faster and wrong scores zero.

## 2. Input files

Both files are in `/data` (read-only): `records.csv` and `regions.csv`. Nothing else is there.

### 2.1 `records.csv`: parsing rules

These rules define what every byte sequence means. Section 2.2 then narrows them to what the generator actually writes, and you may rely on both.

```
file        = [bom] header *( [bom] record )
header      = "id,name,note,region_id,amount" terminator
record      = id "," text "," text "," region-id "," amount terminator
terminator  = CRLF / LF                      ; %x0D.0A or %x0A; may be omitted on the final record
id          = 1*DIGIT                        ; ASCII 0-9 only
region-id   = 1*DIGIT
amount      = [ "-" ] 1*DIGIT "." 2DIGIT     ; -?[0-9]+\.[0-9]{2}
text        = quoted / unquoted
quoted      = DQUOTE *( qbyte / DQUOTE DQUOTE ) DQUOTE
qbyte       = any byte except DQUOTE         ; commas, LF, CR, U+FEFF are all literal here
unquoted    = *( any byte except DQUOTE, ",", CR, LF )
bom         = %xEF.BB.BF                     ; U+FEFF
```

- **Columns.** Every record has exactly five fields, in the header's order: `id,name,note,region_id,amount`. The header appears once, as the first record.
- **Quoting.** A field that starts with `"` is quoted. It ends at the next `"` that is not part of a `""` pair, and that closing quote must be followed by `,` or by a record terminator. The value of a quoted field is the bytes between its delimiting quotes, with every `""` replaced by one `"`. Inside quotes, every other byte is literal data, including `,`, LF and U+FEFF. A field that does not start with `"` is unquoted, and its value is its bytes as they are.
- **Empty values.** An empty unquoted field (nothing between the commas) and an empty quoted field (`""`) both have the empty string as their value. They are the same value.
- **Records.** A record terminator is a CRLF or a bare LF that is outside quotes. An LF inside a quoted field is part of the value and does not end the record. The final record may end at end of file without a terminator. (The generator always writes one, see 2.2.) A terminator at the very end of the file does not start another record. An empty line is not a record and never occurs.
- **Byte order marks.** A single U+FEFF (bytes `EF BB BF`) at the very start of the file, or immediately after a record terminator, is a byte order mark. It is not data and is skipped. A U+FEFF anywhere else is data. Inside a quoted field it is three bytes of the value. A second U+FEFF directly after a skipped one would be data at the start of the `id` field, which makes the record invalid. The generator never writes that.
- **Values.** `id` and `region_id` are decimal integers. `amount` is a money value in exact integer cents: sign × (integer part × 100 + the two fraction digits). For example, `-5.25` is -525 cents and `0.00` is 0. `name` and `note` are byte strings compared byte for byte, with no trimming, case folding or Unicode normalisation. The text is UTF-8.

### 2.2 `records.csv`: what the generator writes

These hold for every seed and every scale, including the holdout.

- **Start and end.** The file starts with a BOM, then the header terminated by CRLF: `EF BB BF` `id,name,note,region_id,amount` `0D 0A`. It ends with the last record's terminator, which is always CRLF. The header appears exactly once.
- **BOMs.** Mid-file BOMs occur only at the start of a record. They never directly follow the header, never come two in a row, and never sit at end of file. Think of the file as chunks concatenated from different exports: some chunks start with a BOM and the rest don't.
- **Terminators.** Each record independently ends with CRLF or LF, about half and half. No CR appears anywhere except as the first byte of a CRLF terminator. In particular there is no CR inside a quoted field and no CR-only line ending.
- **Bytes.** Apart from the BOMs, every byte is printable ASCII (0x20 to 0x7E), LF or CR. There are no other control bytes and no non-ASCII text.
- **`id`.** Always unquoted. Matches `[1-9][0-9]*`. Ids are unique and strictly increasing through the file, usually by 1 and sometimes by 2 to 11.
- **`region_id`.** Always unquoted. Matches `[1-9][0-9]*` and is at most 9,999,999. About 99.6% of records reference a region that is in `regions.csv`. The rest reference either an id inside the lookup's range that the lookup does not contain, or an id above the range (10,000,000 is never reached).
- **`amount`.** Always unquoted. Matches `-?(0|[1-9][0-9]*)\.[0-9]{2}` (no leading zeros) and is never `-0.00`. The range is -999.99 to 99,999.99. About 4% of amounts are negative, some of them between -0.99 and -0.01, and about 2% are `0.00`.
- **`name`.** Either empty (about 0.5% unquoted empty and 0.5% `""`) or a quoted non-empty value. Non-empty names are always quoted, even when they don't need to be, as `"Betts"` is in the site's preview. A name is made of letters and spaces. It may contain a comma (`Kaphar, Titus`) or double quotes (`Ann "Annie" Lee`, written `"Ann ""Annie"" Lee"`), and may end with one or more trailing spaces. Seed 1 has runs of up to six at full scale, and there is no fixed upper bound. It never starts with a space and never contains an LF.
- **`note`.** Either empty (about 1.5% unquoted empty and 1.5% `""`) or a quoted non-empty value. A note is made of lowercase words separated by single spaces or by LFs. It may contain a comma, a quoted word (`said "no" twice`) or a lone `"`, and may end with one trailing space (never more). It has at most four lines (at most three LFs). It never starts or ends with an LF, never has two LFs in a row, never has a space right after an LF, and never starts with a space. A note can begin or end with a quote character, so a quoted field can begin with `"""` or end with `"""`.
- **Distribution (for planning, not a guarantee).** Region popularity is skewed: half the records pick a referenced region uniformly, and half pick by Zipf with weight 1/rank over a random ranking of the regions. Names are drawn uniformly from a pool of 100,000 distinct values, about 5% of which differ from another pool name only by trailing spaces. Notes are drawn uniformly from a pool of 60,000. tiny uses pools of 400 and 300.

### 2.3 `regions.csv`

The lookup table is deliberately plain. It has no hostile features.

- ASCII, LF line endings, no BOM, no quotes, no spaces. The header line is `region_id,country`, followed by one line per region, `region_id,country`. The last line ends with LF.
- `region_id` matches `[1-9][0-9]*`. Region ids are unique and listed in ascending numeric order. `country` matches `[A-Z]{3}`.
- At full and sample scale there are 200,000 regions with ids between 1 and 999,999, spread over 240 countries (about 830 regions per country). tiny shrinks this to 2,000 regions between 1 and 9,999 and 24 countries, so a test can read it by eye. full and sample share every other parameter.
- Country codes are synthetic and change with the seed. One country has regions in the table that no record references. Another country's records all carry amounts between -0.99 and -0.01.

### 2.4 Scales

| scale | records | lookup | use |
|---|---|---|---|
| `full` | 3,320,000 | 200,000 regions, 240 countries | scoring |
| `sample` | 332,000 (10% of full) | same shape as full | published with its expected hash |
| `tiny` | 1,000 | 2,000 regions, 24 countries | tests |

`sample` is drawn from the same distribution as `full`, from its own random stream: it is not a prefix of `full` and does not share its lookup.

### 2.5 Edge cases

Every case below is written on purpose. The generator inserts one block of records containing all of them per 25,000 records, and never fewer than four blocks: that is 4 blocks in tiny, 13 in sample and 132 in full. Ordinary records produce most of them again at the rates in 2.2. `tests/test_c_join.py` asserts that each one is present in tiny and in sample, using the names in brackets.

Encoding:

1. A BOM at the start of the file `[bom_at_file_start]`.
2. A BOM at a mid-file record boundary `[bom_mid_file]`: one after a CRLF-terminated record `[bom_after_crlf]` and one after an LF-terminated record `[bom_after_lf]`.
3. A record directly after a mid-file BOM that ends with CRLF `[crlf_record_after_bom]`, and one that ends with LF `[lf_record_after_bom]`.
4. CRLF and LF terminators mixed in the same file `[crlf_terminator, lf_terminator]`.
5. A quoted field containing a comma, in a name `[name_comma]` and in a note `[note_comma]`.
6. A quoted field containing `""` escapes, in a name `[name_escaped_quote]` and in a note `[note_escaped_quote]`.
7. A note containing one bare LF, so the record spans two lines `[note_two_lines]`.
8. A note spanning several lines (two or three LFs) `[note_several_lines]`.
9. A note containing a comma, a `""` escape and an LF together `[note_comma_quote_lf]`.
10. An empty unquoted field, in a name `[name_empty_unquoted]` and in a note `[note_empty_unquoted]`.
11. An empty quoted field `""`, in a name `[name_empty_quoted]` and in a note `[note_empty_quoted]`.
12. A name with trailing spaces inside the quotes `[name_trailing_space]`.
13. The last record of the file, terminated by CRLF `[last_record_crlf]`.

Values and the join:

14. Negative amounts `[negative_amount]`, including some between -0.99 and -0.01 `[negative_amount_under_one]`.
15. Zero amounts, written `0.00` `[zero_amount]`.
16. An unknown `region_id` inside the lookup's id range, a hole `[unknown_region_in_range]`, and one above it `[unknown_region_above_range]`.
17. Two names in one country that differ only by a trailing space, `Betts` and `Betts ` `[trailing_space_twins_in_one_country]`.
18. An empty unquoted name and an empty quoted name in one country `[both_empty_names_in_one_country]`.
19. A country in `regions.csv` with no records at all `[country_without_records]`.
20. A country whose records all have amounts between -0.99 and -0.01, so its sum, min and max are all negative `[country_all_negative_under_one]`.

## 3. Task

1. Parse `records.csv` per section 2.1.
2. Join each record to `regions.csv` on `region_id`, compared as integers. The record's group is that region's `country`. If `region_id` is not in `regions.csv`, the record's group is `_UNMATCHED`.
3. For every group that has at least one record, compute:

| column | definition |
|---|---|
| `count` | number of records in the group |
| `id_sum` | sum of the group's `id` values. The BOM always sits in front of an `id`, so this column is what makes BOM handling visible in the output. |
| `amount_sum` | sum of the group's amounts, in cents |
| `amount_min` | smallest amount in the group, in cents |
| `amount_max` | largest amount in the group, in cents |
| `names_distinct` | number of distinct `name` values in the group, compared as exact byte strings after unescaping. `Betts` and `Betts ` are two values. `Ann "Annie" Lee` is compared with its quotes unescaped. The empty unquoted name and the empty quoted name are both the empty string and count as one value. |
| `name_bytes` | sum of the UTF-8 byte lengths of the group's `name` values after unescaping. The delimiting quotes count 0, each `""` counts 1, and trailing spaces count. Distinct counting alone would not catch a parser that fails to unescape, because that mistake maps names one to one; this column catches it. |
| `note_bytes` | sum of the UTF-8 byte lengths of the group's `note` values after unescaping. An embedded LF counts 1, each `""` counts 1, and an empty note counts 0. |

4. A country that is in `regions.csv` but has no records produces no line. `_UNMATCHED` produces a line if and only if at least one record has an unknown `region_id`. (In generated data that is always the case.) Group keys are unique, so there are no ties.

**Magnitudes.** `id_sum` and `amount_sum` exceed 2^32 for most groups in `full`. They always fit in a signed 64-bit integer: ids are below 4 × 10^7, so `id_sum` is below 1.4 × 10^14, and `|amount_sum|` is below 3.4 × 10^13 cents. The other columns are below 2^31.

## 4. Output

Write exactly one file, `/out/aggregates.txt`.

- One line per group: `country count id_sum amount_sum amount_min amount_max names_distinct name_bytes note_bytes`. Fields are separated by a single space. No key can contain a space, because codes are `[A-Z]{3}` and the unmatched key is `_UNMATCHED`. There is no header line.
- **Order.** Lines are sorted by key, bytewise ascending. `_UNMATCHED` sorts after every country code because `_` (0x5F) is greater than `Z` (0x5A), so it is always the last line.
- **Integers** (`count`, `id_sum`, `names_distinct`, `name_bytes`, `note_bytes`) are in decimal with no leading zeros and no plus sign.
- **Money** (`amount_sum`, `amount_min`, `amount_max`) is integer cents printed as: `-` if negative, then the integer part with no leading zeros (`0` when the absolute value is below 1.00), then `.`, then exactly two digits. For example -525 is `-5.25`, -5 is `-0.05`, 0 is `0.00` (never `-0.00`) and 142000 is `1420.00`. No floating point is involved anywhere.
- UTF-8 (in practice ASCII), LF after every line including the last, no BOM, no trailing spaces.

### Worked example

In the notation the site uses, ⟨BOM⟩ is the bytes `EF BB BF`, ␍␊ is CRLF and ␊ is a bare LF. The first five lines of this `records.csv` are the site's preview, byte for byte. The same bytes are `WORKED_RECORDS` in `tests/test_c_join.py`.

`records.csv`:

```
⟨BOM⟩id,name,note,region_id,amount␍␊
1,"Kaphar, Titus","said ""no"" twice",7,1420.00␍␊
2,"Betts","a note that␊spans two lines",7,88.50␊
3,,"",12,0.00␍␊
4,"trailing space ","comma, quote "" and newline␊together",3,-5.25␊
⟨BOM⟩5,"Betts ","one␊two␊three",12,-0.75␍␊
6,"",,12,10.00␍␊
⟨BOM⟩7,"Ann ""Annie"" Lee","x",5,2.00␊
8,"Nobody","",4000000,-1.00␍␊
9,"Neg","n",20,-0.05␍␊
```

`regions.csv`:

```
region_id,country␊
3,BBB␊
7,AAA␊
12,AAA␊
20,CCC␊
21,DDD␊
```

`aggregates.txt`, with every line ending in LF:

```
AAA 5 17 1517.75 -0.75 1420.00 4 24 55
BBB 1 4 -5.25 -5.25 -5.25 1 15 35
CCC 1 9 -0.05 -0.05 -0.05 1 3 1
_UNMATCHED 2 15 1.00 -1.00 2.00 2 21 1
```

How the lines come about:

- **AAA** (regions 7 and 12) holds records 1, 2, 3, 5 and 6.
  - The ids sum to 17.
  - The amounts sum to 142000 + 8850 + 0 - 75 + 1000 = 151775 cents.
  - The names are `Kaphar, Titus`, `Betts`, the empty name (unquoted in record 3, quoted in record 6, the same value), and `Betts ` (record 5, with a trailing space). That is 4 distinct values, with 13 + 5 + 0 + 6 + 0 = 24 bytes.
  - The notes are 15 bytes (`said "no" twice`), 27 (one LF), 0, 13 (two LFs) and 0 (unquoted empty), so 55 bytes.
  - The BOM before record 5 follows an LF terminator, and record 5 ends with CRLF.
- **BBB** holds only record 4. Its name has a trailing space (15 bytes), and its note `comma, quote " and newline` LF `together` is 35 bytes.
- **CCC** holds only record 9. Its amount is -0.05, so the sum, the min and the max are all `-0.05`.
- **DDD** (region 21) has no records and produces no line.
- **_UNMATCHED** holds record 7 and record 8:
  - Record 7 has region 5, which is not in the table although it lies between 3 and 21. Its name `Ann "Annie" Lee` is 15 bytes once unescaped.
  - Record 8 has region 4000000, above the table's range.
  - The BOM before record 7 follows a CRLF terminator.

## 5. Sizes

Measured from the generator with seed 1 on an Apple M4 Pro. Record counts are exact for every seed. Byte counts vary by a fraction of a percent between seeds.

| | full | sample |
|---|---|---|
| records | 3,320,000 | 332,000 |
| `records.csv` | 201,935,059 B | 19,846,456 B |
| `regions.csv` | 2,177,799 B (200,000 regions, 240 countries) | 2,177,777 B (200,000 regions, 240 countries) |
| dataset total | 204,112,858 B (204.1 MB) | 22,024,233 B (22.0 MB) |
| mid-file BOMs | 777 | 83 |
| edge-case blocks | 132 | 13 |
| records with an unknown `region_id` | 13,475 | 1,298 |
| records whose note contains an LF | 276,857 | 27,212 |
| `aggregates.txt` | 240 lines, 17,054 B | 240 lines, 15,415 B |
| generation time | 10.5 s | 1.6 s |

Seed-1 sha256 values, as measured when this spec was written. The hashes published with the datasets are authoritative.

| file | sha256 |
|---|---|
| full `records.csv` | `36166f4ac44101a22ebf25b4f949436a681a2698cb0f2ae1f7f6853010259f48` |
| full `regions.csv` | `414c57c4f3ec968f9bce28c59eebfdf57a9425322b468db7ee964c18c4852fe1` |
| full `aggregates.txt` | `d8088b1cd42ba1338b1345b107b55db7b96ef1f5066f3ff64fd26a8912a0f536` |
| sample `records.csv` | `07bc636857b70556921be08d406f887eb07dbdacb8384587ed7fd3b52addfd86` |
| sample `regions.csv` | `1bd3904fd5eaed7a4dc1ae761bf369521ce5f384e4c400342919d6edf9e5ece1` |
| sample `aggregates.txt` | `9035fed36823232dd41ae2d1b9d6d21aa7c73f49d8e95647e953a89d5ebe9a66` |

Reference on `full`, on the same machine: 6.0 s wall and about 400 MB peak RSS. The scoring-box figure, and the timeout of 10× that figure, are published after calibration.

## 6. Banned libraries

Per rule 02, no analytics dataframe library in Track C. Concretely, you may not use any dataframe library or SQL or query engine, in any language, whether or not it ships with your language's standard library. That includes:

- pandas, Polars, DuckDB
- Apache Arrow in any binding, including its CSV reader and its compute kernels: pyarrow, arrow-rs and arrow-csv, Arrow C++, Acero
- Apache DataFusion, Apache Spark and PySpark, Dask, Modin, Vaex, cuDF and the rest of RAPIDS
- ClickHouse and chDB
- SQLite, including Python's `sqlite3` module and SQLite's CSV virtual table
- R's data.table and dplyr
- Julia's DataFrames.jl
- Go's gota and dataframe-go

If a library's job is to perform a relational join or group-by for you, it is banned even if it is not named here.

CSV parsing libraries are allowed: for example Rust's `csv` and `csv-core`, C's `libcsv` or C++'s `rapidcsv`. A non-standard-library CSV parser is a dependency like any other and is charged on the D axis. A CSV module in your language's standard library (Python's `csv`, Go's `encoding/csv`) costs nothing there. Whatever you use, its output must follow section 2.1 exactly. Most CSV libraries have options that change these rules, such as trimming, comment lines, header handling and BOM handling, and getting them wrong fails the hash.

## 7. Reference behaviour

`reference/c_join.py` is the obvious Python version: correct, single-threaded and readable.

- It loads `regions.csv` into a dict mapping `region_id` to `country`.
- It opens `records.csv` as UTF-8 text with `newline=""` and hands it to Python's `csv` module (excel dialect), which returns one list of five `str` per record.
- It removes one leading U+FEFF from the first field of every record, then checks the header.
- It validates `id` and `region_id` with a regular expression and converts them with `int()`, and converts `amount` to integer cents with a regular expression.
- It looks up each record's `region_id` in the dict, a hash join, one record at a time.
- It keeps one Python object per country holding running count, sums, min and max, a `set` of names, and byte counts. It gets the byte counts by encoding every name and note back to UTF-8.
- When the file is done, it sorts the roughly 240 keys and writes the lines.

Where the time goes:

- every byte is decoded into Python `str` objects and then re-encoded to count bytes;
- every record costs a new list, five new strings and a dozen interpreted function calls;
- everything runs on one core.

## 8. Changes from the site

Compared with the Track C entry on https://speeddemon.dev (`src/data/tracks.json`):

1. **`spec.dataset_mb` 204 → 204.1.** That is the measured total of both files (records.csv 201.9 MB, regions.csv 2.2 MB). It is effectively unchanged.
2. **`spec.records` "hostile CSV" → 3,320,000 records.**
3. **`spec.reference_s` 36.9 → 6.0 s measured on an Apple M4 Pro.** This is below the conventions' 20 to 60 s band. The reference gets through this file at about 34 MB/s, because Python's `csv` module does the parsing in C. A csv-module reference would reach 20 s only at roughly three times the published dataset size, and the size was kept at 204 MB. The scoring-box time, and `timeout_s` at 10× that time, come from the calibration on Oct 26-27.
4. **The lookup table is named and defined.** The site says "join it against a lookup table" without naming one. It is `regions.csv` (section 2.3), and it is deliberately plain.
5. **"Emit sorted aggregates" is defined** (sections 3 and 4). `id_sum` and `name_bytes` are there so that BOM handling and unescaping both change the hash.
6. **"A UTF-8 BOM on some chunks and not others" is made precise.** The file always starts with a BOM. Mid-file BOMs appear only at record boundaries (section 2.2), and the BOM rule is stated exactly in 2.1.
7. **The preview's values are illustrative.** Its five lines are the start of the worked example in section 4, not lines of the generated file. Generated ids reach seven digits, region ids reach seven digits, and names and notes are synthetic. The site's promise that every hostile feature is visible in those five lines holds. The generated file uses no feature beyond them: no repeated header, no CR-only line endings, no CR inside quotes, no quoted numeric fields, no unquoted non-empty text and no non-ASCII text. The one thing the preview does not show is the end of the file, which is defined in 2.1 and fixed in 2.2: the last record is always terminated, by CRLF.
