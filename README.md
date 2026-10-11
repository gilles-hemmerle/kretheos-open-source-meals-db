# kratheos-food-data

One open food database, rebuilt from open sources by one command.

The build downloads the food-composition databases listed in
[`sources.toml`](sources.toml), stores every file exactly as published,
converts each source to one common schema, and writes:

- `dist/food.sqlite`, the main database: generic foods (an average "pizza,
  cheese"), packaged products found by their barcode, restaurant items, with
  a full-text index on the names in every language and an index by country;
- Parquet, CSV and JSON Lines files holding every record of every source;
- `dist/manifest.json`, which says what was built from what.

It was written for [Kratheos](#the-kratheos-layer), an AI sport coach with a
nutrition part, but nothing in the database is specific to it.

Figures of the build of 2026-10-11:

| Source | Records kept | Kind |
| --- | ---: | --- |
| Open Food Facts | 3 454 545 | products with a barcode |
| USDA FoodData Central | 449 175 | 13 547 generic foods, 435 628 branded products |
| Canadian Nutrient File | 5 993 | generic foods |
| Ciqual (France) | 3 341 | generic foods |
| CoFID (United Kingdom) | 2 853 | generic foods |
| Swiss Food Composition Database | 1 246 | 1 216 generic foods, 30 branded |

That is 26 950 generic foods and 3 890 203 product records for 3 404 293
distinct barcodes; `food.sqlite` holds 3 564 569 foods.

## Rebuild

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), about 9 GB of
network transfer the first time and 20 GB of free disk space.

```sh
make build
```

does everything: it obtains the source files (8.4 GB, resumed after an
interruption, verified by checksum, not downloaded again when unchanged),
runs the adapters, aggregates, applies the Kratheos layer and writes `dist/`.
With the files already there a build takes about two and a half minutes on a
laptop.

| Command | What it does |
| --- | --- |
| `make build` | Rebuild `dist/` from upstream, falling back to the stored copies |
| `make build-from-mirror` | Rebuild from the stored copies only; no publisher is contacted |
| `make build MIRROR_URL=<address>` | Same as `make build`, fetching missing large files from a release |
| `make download` | Obtain or verify the source files without building |
| `make release-files` | Lay out in `release/` what a release must carry |
| `make test`, `make lint` | Tests (no network) and code style |
| `make clean` | Remove `work/` and `dist/`; the stored originals stay |

`uv run food-data --help` lists the commands behind these targets and their
options (`--only ciqual,swiss` to build some sources, `--min-free-gb` to
protect the disk, `--override`, see below).

## The sources and their licences

| id | Database | Publisher | Version built | Licence |
| --- | --- | --- | --- | --- |
| `off` | [Open Food Facts](https://world.openfoodfacts.org/) | Open Food Facts | Parquet export, rolling | ODbL 1.0 (contents: DbCL 1.0) |
| `usda` | [FoodData Central](https://fdc.nal.usda.gov/) | U.S. Department of Agriculture | Foundation and Branded 2026-04, FNDDS 2021-2023, SR Legacy 2018 | CC0 1.0 (public domain) |
| `ciqual` | [Ciqual](https://ciqual.anses.fr/) | Anses, France | 2025 | Licence Ouverte 2.0 (Etalab) |
| `swiss` | [Swiss Food Composition Database](https://naehrwertdaten.ch/) | FSVO, Switzerland | 7.1 (2026-07) | opendata.swiss "Open use. Must provide the source." |
| `cnf` | [Canadian Nutrient File](https://open.canada.ca/data/en/dataset/1b6139bd-ed7e-4043-bc28-ff00e10f3109) | Health Canada | 2026 | Open Government Licence - Canada |
| `cofid` | [CoFID](https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid) | Public Health England | 2021 | Open Government Licence v3.0 |

`sources.toml` holds, for each source, the page where its terms were read,
the sentence that allows redistribution and the date it was checked. Two
databases are listed there but disabled, with the reason: the German BLS 4.0
(free to use, but no licence text and nothing on redistribution) and FoodRepo
(the service closed). A source is only enabled once its terms clearly allow
redistribution.

**How the output is licensed.** Open Food Facts is share-alike, so the
database as a whole is made available under the
[Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
Every record keeps the licence of its own source in its `licence` column.
[`LICENSE-DATA`](LICENSE-DATA) has the details and [`ATTRIBUTION`](ATTRIBUTION)
the credits to reproduce; both are also inside `food.sqlite` (tables `meta`
and `sources`). The code is under the MIT licence ([`LICENSE`](LICENSE)).

No personal data: no name or handle of an Open Food Facts contributor is ever
read, and no image is taken.

## What is in `dist/`

| File | Content |
| --- | --- |
| `food.sqlite` | The main database, Kratheos layer applied (see below) |
| `generic_foods.parquet`, `.csv.gz`, `.jsonl.gz` | Every generic food of every source |
| `products.parquet` | Every product record of every source |
| `products_by_barcode.parquet` | One preferred source record per barcode |
| `identifiers.txt.gz` | Sorted list of the identifiers of `food.sqlite` |
| `manifest.json` | Date, version and checksum of every source file, origin (upstream or stored copy), licences, counts, checksum of every output |

The Parquet, CSV and JSON Lines files hold the sources as they are, in the
[common schema](#the-common-schema), and nothing from Kratheos.
`--all-formats` also writes the two product tables as CSV and JSON Lines.

Aggregation rules:

- Generic foods are never merged across sources and nothing is averaged:
  "apricot, raw" exists once per table that has it, each with its source.
- Every product record of every source is kept. For each barcode one record
  is preferred, chosen by: plausible values first, then complete macros
  (energy, protein, carbohydrate, fat), then the most recent update, then the
  fixed order of `sources.toml`. The preferred record lists the other
  sources and records that know the barcode (`sources`, `other_records`) and
  whether they agree (`sources_agree`: energy within 10 kcal or 10 %, each
  macro within 2 g or 20 %).

## `food.sqlite`

| Table | Content |
| --- | --- |
| `foods` | One row per food: the generic foods, one row per barcode, and the products without a valid barcode (restaurant items mostly). Columns of the common schema, plus `record_id`, `confirmations`, `source_count`, `sources`, `other_records`, `sources_agree`. Lists are JSON text, booleans are 0 or 1. |
| `names` | Every name of every food, one row per language: `food_pk`, `lang`, `name`, `origin` (`source` or `kratheos`), `kratheos_origin` (`machine`, `human` or `correction`). |
| `foods_fts` | FTS5 index over the names of a food in every language it has, and its brand. Diacritics are ignored; prefixes of 2 and 3 letters are indexed. |
| `food_countries` | One row per food and country, keyed by country first; `countries` counts foods per country. |
| `languages` | The language codes present in `names`. |
| `display_names` | View applying the rule for the name to show. |
| `source_values` | The foods a Kratheos correction changed, as their source gives them. |
| `sources` | Version, licence and attribution of each source. |
| `meta` | Layout version, licence notice and the two helper queries below. |

Indexes: `foods(id)` and `foods(barcode)` are unique.

Scan a barcode. Normalise it first (see [Barcodes](#barcodes)):

```sql
SELECT * FROM foods WHERE barcode = '3017620422003';
```

Search as the person types. Turn each word into a quoted prefix term
(`pain compl` becomes `"pain"* "compl"*`; `kratheos_food_data.lookup.fts_query`
does it) and rank the foods of the person's country first:

```sql
SELECT f.id, f.name, f.brand, f.countries
FROM (
    SELECT rowid, rank FROM foods_fts WHERE foods_fts MATCH :query ORDER BY rank LIMIT 200
) AS m
JOIN foods AS f ON f.pk = m.rowid
ORDER BY
    EXISTS (
        SELECT 1 FROM food_countries AS c WHERE c.country = :country AND c.food_pk = f.pk
    ) DESC,
    m.rank,
    f.popularity DESC
LIMIT 20;
```

The index ranks the matches and keeps the best 200, which are then ordered
with the foods of the person's country first. On 3.5 million foods this takes
a few tens of milliseconds from three letters on; a prefix of one or two
letters matches hundreds of thousands of foods and takes about a second, so
start searching at the third letter. The query is also stored in
`meta.search_query`.

A search finds a food by any of its names: "Mandel", "amande" and "mandorla"
all find the almond of the Swiss table. To keep only one country, join
`food_countries` and filter on `country`; its primary key starts with the
country, so both uses are one index lookup per food.

### The name to show

A food has a name in each language its source gives, plus those added in
Kratheos. The name to show to a person is, in this order:

1. the name in the person's language;
2. if there is none, the English name;
3. if there is no English name either, whatever name the record has
   (`foods.name`, its main name).

Three ready ways to apply the rule:

```sql
-- the view, for a language present in the database
SELECT name FROM display_names WHERE id = 'ciqual:13000' AND lang = 'de';

-- the same as a query, for any language (also stored in meta.display_name_query)
SELECT COALESCE(
    (SELECT n.name FROM names AS n WHERE n.food_pk = f.pk AND n.lang = :language),
    (SELECT n.name FROM names AS n WHERE n.food_pk = f.pk AND n.lang = 'en'),
    f.name
) AS name
FROM foods AS f
WHERE f.id = :id;
```

```python
from kratheos_food_data.names import display_name

display_name({"fr": "Abricot", "en": "Apricot"}, "de")  # "Apricot"
```

## Identifiers: a promise

Favourites and diary entries of an application point at foods of this
database, so the identifier of a food (`foods.id`) never changes when the
database is rebuilt:

- **A food with a valid barcode is `gtin:<canonical barcode>`**, for instance
  `gtin:3017620422003`. It identifies the product itself, whichever source is
  preferred this time. The source record behind it is in `record_id`
  (`off:3017620422003`), the records of the other sources in `other_records`.
- **Every other food is `<source>:<identifier in the source>`**, built only
  from what the source keeps stable across its releases:

| Source | Identifier | What was checked |
| --- | --- | --- |
| `off` | the product code, `off:<code>` | It is the key of Open Food Facts; items without a real barcode have an internal code (`off:2000000002603`). |
| `usda` Foundation Foods | NDB number, `usda:foundation:<ndb>` | FoodData Central gives a food a new `fdc_id` at each update. All 340 NDB numbers of 2025-04 and all 236 of 2023-04 are still in 2026-04, including the foods whose `fdc_id` changed. |
| `usda` SR Legacy | NDB number, `usda:sr_legacy:<ndb>` | Final release (2018). |
| `usda` FNDDS | food code, `usda:fndds:<code>` | Between 2022-10 and 2024-10 every `fdc_id` changed; 5 389 of 5 624 food codes are still there, the others were retired. |
| `usda` Branded Foods | the barcode, `usda:branded:<gtin>` | A product gets a new `fdc_id` at each update (two million rows for 465 000 barcodes). |
| `ciqual` | `alim_code` | 3 003 of the 3 185 codes of Ciqual 2020 are in Ciqual 2025, the others were retired. |
| `swiss` | the database ID | 1 128 of the 1 145 IDs of version 6.5 (2023) are in 7.1 (2026). |
| `cnf` | `Food_Code` | 598 of the 607 foods added in 2015 are under the same code in 2026. |
| `cofid` | the food code | Per the user guide each food has a unique code, and a food whose data are replaced gets a new one. |
| `kratheos` | the identifier given in Kratheos, stored in `kratheos.sqlite` | Stable by construction. |

No identifier comes from a counter, a row number or the order of processing.
`foods.pk`, `names.food_pk` and the row ids of `foods_fts` are internal: they
link the tables inside one file and change from build to build. Keep `id`,
never `pk`.

What can still happen is that a publisher retires a food. Each build writes
`identifiers.txt.gz`, and

```sh
uv run food-data ids-diff old/identifiers.txt.gz dist/identifiers.txt.gz
```

lists the identifiers added and removed between two builds, so a release can
say which ones disappeared upstream. Two builds of the same inputs give the
same identifiers and the same `food.sqlite`, byte for byte; the tests check it.

## The common schema

One record per food of a source. Values are per 100 g, or per 100 ml when
`basis` says so.

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | text | Identifier of the source record, `<source>:<id in the source>` (in `food.sqlite`: the public identifier, see above) |
| `source` | text | `off`, `usda`, `ciqual`, `swiss`, `cnf`, `cofid`, `kratheos` |
| `source_dataset` | text | Sub-dataset inside the source (`foundation`, `sr_legacy`, `fndds`, `branded`...) |
| `source_record_id` | text | Identifier inside the source |
| `kind` | text | `generic` (food-composition table) or `product` (branded item) |
| `barcode` | text | Canonical GTIN; NULL when the source has none or it is invalid |
| `name`, `name_lang` | text | Main name of the record and its language |
| `names` | map | Every name the source gives, by language code; no machine translation |
| `brand` | text | Brand, for products |
| `category` | text | Category in the source's own classification |
| `basis` | text | `100g` or `100ml` |
| `energy_kcal` | number | Energy, kcal |
| `protein_g` | number | Protein, g |
| `carbohydrate_g` | number | Available carbohydrate, fibre excluded, g |
| `carbohydrate_total_g` | number | Carbohydrate of the source when it includes fibre; NULL otherwise |
| `sugars_g` | number | Sugars, g |
| `fat_g`, `saturated_fat_g` | number | Fat and saturated fat, g |
| `fiber_g` | number | Dietary fibre, g |
| `salt_g` | number | Salt, g |
| `alcohol_g` | number | Alcohol, g |
| `serving_size`, `serving_unit`, `serving_text` | number, text | One serving, in g or ml, and as the source words it |
| `is_drink` | boolean | Whether the source classifies the food as a beverage; NULL when unknown |
| `countries` | list | Countries the values apply to, ISO 3166-1 alpha-2, upper case |
| `popularity` | integer | Open Food Facts: number of distinct scans |
| `has_macros` | boolean | Energy, protein, carbohydrate and fat are all present |
| `plausible` | boolean | No quality flag was raised |
| `flags` | list | Derivation and quality flags, below |
| `licence` | text | Licence of the record |
| `source_url` | text | Link to the record on the publisher's site |
| `updated_at` | date | Last update of the record in the source |

`products_by_barcode` adds `product_id` (`gtin:<barcode>`), `source_count`,
`sources`, `other_records` and `sources_agree`.

A record is kept when it has a name and an energy value. The others are
counted in the manifest.

### Units and conventions

- **Basis.** 100 g, or 100 ml for the sources that say so. Open Food Facts
  does not record it: the basis is inferred from the unit of the package
  quantity, then of the serving; with neither, 100 g is assumed and flagged
  `basis_assumed` (four Open Food Facts records in ten).
- **Energy** is in kcal. A source that only gives kJ is converted
  (kcal = kJ / 4.184) and flagged `energy_from_kj`.
- **Carbohydrate is available carbohydrate, dietary fibre excluded**, as on
  European labels. Sources do not agree on the word, so each was checked:

  | Source | What it publishes | What the build does |
  | --- | --- | --- |
  | Ciqual | available carbohydrate (constituent tagged CHOAVL) | kept |
  | Swiss table | "Carbohydrates, available" | kept |
  | CoFID | available carbohydrate, as monosaccharide equivalents (about 5 to 10 % above the weight for starch) | kept |
  | USDA generic foods | "Carbohydrate, by difference", fibre included | fibre subtracted |
  | USDA Branded Foods | total carbohydrate of the label, fibre included (available carbohydrate for the New Zealand market) | fibre subtracted |
  | Canadian Nutrient File | "Carbohydrate, total (by difference)", fibre included | fibre subtracted |
  | Open Food Facts | whatever the label says: total in the United States and Canada, available in Europe. The export does not say which. | fibre subtracted when every country of sale is the United States or Canada |

  When fibre is subtracted, the figure of the source is kept in
  `carbohydrate_total_g` and the record is flagged
  `carbohydrate_fiber_subtracted`. When the source includes fibre but gives no
  fibre value, the figure cannot be converted: it is kept as it is and flagged
  `carbohydrate_includes_fiber`.
- **Salt.** A source that only gives sodium is converted (salt = sodium x 2.5)
  and flagged `salt_from_sodium`.
- **Alcohol** given in % vol is converted (g = % vol x 0.789) and flagged
  `alcohol_from_abv`.
- **Countries** are ISO 3166-1 alpha-2 codes in upper case. For a product:
  where it is sold (Open Food Facts country tags, USDA market country); a tag
  that maps to no country, such as `world`, is dropped and counted. For a
  generic food: the country of the authority that publishes the table
  (Ciqual FR, the Swiss table CH, USDA US, the Canadian file CA, CoFID GB),
  because its values describe foods as sold and eaten there.
- **Text values.** `traces` is kept as 0 and `< x` as x; the record is flagged
  `inexact_values`.

### Flags

Informational flags say how a value was obtained and never make a record
implausible: `energy_from_kj`, `salt_from_sodium`, `alcohol_from_abv`,
`basis_assumed`, `carbohydrate_fiber_subtracted`,
`carbohydrate_includes_fiber`, `inexact_values`, `obsolete` (the source marks
the product as no longer sold), `barcode_invalid` (the source has a barcode
that is not a valid GTIN: the record is kept without it), `barcode_restricted`
(an in-store number, not unique across retailers), `kratheos_corrected`.

Quality flags make a record implausible. A record that fails is kept and
flagged, never corrected:

| Flag | Rule |
| --- | --- |
| `value_out_of_range` | A nutrient is below 0 or above 100 |
| `energy_out_of_range` | Energy is below 0 or above 905 kcal (pure fat is 900) |
| `sugars_exceed_carbohydrate` | Sugars exceed carbohydrate by more than 1 g |
| `saturated_exceed_fat` | Saturated fat exceeds fat by more than 1 g |
| `macros_sum_over_100` | Protein + carbohydrate + fat exceed 105 g |
| `energy_macro_mismatch` | Energy is not what the macros explain |

The energy check follows the carbohydrate convention: expected energy =
4 x protein + 4 x carbohydrate + 2 x fibre + 9 x fat + 7 x alcohol (polyols
at 2.4). The energy passes within 20 % and 10 kcal of it, plus 2 kcal per
gram of fibre and 2.4 per gram of polyols. 20 % is the tolerance regulators
accept between a label and an analysis; the 10 kcal absorb rounding on foods
with almost no energy; the fibre margin exists because a United States label
counts fibre at 4 kcal/g or at 0 where a European one counts 2. Known cases
the check cannot explain: vinegar and lemon juice (energy from organic
acids), and CoFID sugars above 100 g (monosaccharide equivalents).

## Barcodes

Sources write the same barcode in different ways. One canonical form is used
everywhere, and an application must apply the same rule before a lookup
(`kratheos_food_data.barcode.normalize_gtin`):

1. remove spaces and dashes; anything else that is not a digit makes the
   barcode invalid;
2. remove leading zeros, then left-pad with zeros to 8 digits when the number
   fits in 8 (EAN-8), else to 13 digits (EAN-13; a 12-digit UPC-A becomes a
   GTIN-13), else keep 14 digits (GTIN-14);
3. verify the GS1 check digit; a barcode that fails is invalid.

`049000028911`, `0049000028911` and `00049000028911` are the same product.
A record with an invalid barcode is kept, without barcode, and flagged.

## The stored originals

Every source file is kept exactly as downloaded, so the database can be
rebuilt if a publisher disappears. Nothing in this repository ever rewrites
one.

- **`sources/<id>/`**, committed: the small sources (Ciqual, the Swiss table,
  the Canadian file, CoFID), each file next to a `.meta.json` sidecar with
  its address, download date and checksums.
- **`mirror/`**, not in git: the large ones, under the flat name
  `<id>.<file name>` (`off.food.parquet`, `usda.branded_food_csv.zip`...),
  with `MIRROR.json`, which indexes them. They are meant to be attached to a
  release.

A release refuses a file above 2 GiB and the Open Food Facts export is about
8 GB, so `make release-files` writes into `release/` exactly what to attach:
the small files as they are, the large ones in parts
(`off.food.parquet.part-01-of-05`...), and `MIRROR.json`, which lists the
checksum of each whole file and of each part (the manifest of a build repeats
them). Nothing needs to be joined by hand:

```sh
make build-from-mirror MIRROR_URL=https://example.org/releases/download/v1
```

downloads the parts that are missing from `mirror/`, joins them, verifies the
checksum of the whole file and builds without contacting any publisher.

Where a build takes its files from:

- by default from upstream, and from the stored copy when upstream fails;
- with `--from-mirror`, from the stored copies only, each verified against
  its recorded checksum: a reproducible rebuild;
- with `--override <source>/<file>=<path>`, from another file for one run,
  to try a changed source without touching the stored one.

The manifest records which of the three was used for every file. Files
published under a versioned address are pinned by checksum in `sources.toml`:
they are never downloaded twice. If a publisher replaces one, the build keeps
the stored copy and says so, in its log and in the manifest; the new file is
adopted by updating the entry of the source there, after reading what changed.

## The Kratheos layer

Whatever Kratheos and its users add lives in one small file,
[`kratheos.sqlite`](kratheos.sqlite), committed at the root. It never holds
source data, and the sources never hold anything from it. The build reads the
originals, aggregates the sources, then applies the layer:

1. **`products`**: foods added in Kratheos, with or without a barcode. They
   become records of the source `kratheos`, identified by `kratheos:<id>`,
   with the countries given at creation. One with a barcode competes for
   that barcode like the record of any source, Kratheos coming last in the
   fixed order.
2. **`corrections`**: corrected values for a food. They replace the values in
   `foods`, the quality checks are run again, and the food is flagged
   `kratheos_corrected`. The source's own values stay available: in
   `source_values`, and untouched in the Parquet files.
3. **`confirmations`**: how many people confirmed the values of a food.
4. **`translations`**: a name for a food in a language it lacks. A
   translation never replaces a name given by the source; `names.origin`
   tells the two apart.

| Table | Columns |
| --- | --- |
| `meta` | `key`, `value`: `schema_version`, `exported_at` |
| `translations` | `record_id`, `lang`, `name`, `origin` (`machine` or `human`), `updated_at` |
| `products` | `id`, `kind`, `barcode`, `name`, `name_lang`, `names` (JSON), the value columns, `countries` (JSON), `updated_at` |
| `corrections` | `record_id`, `names` (JSON), the value columns (NULL = not corrected), `updated_at` |
| `confirmations` | `record_id`, `confirmations`, `updated_at` |

The value columns are those of the common schema: `brand`, `category`,
`basis`, `energy_kcal`, `protein_g`, `carbohydrate_g` (available
carbohydrate), `sugars_g`, `fat_g`, `saturated_fat_g`, `fiber_g`, `salt_g`,
`alcohol_g`, `serving_size`, `serving_unit`, `serving_text`, `is_drink`.
`record_id` is the identifier of a food in `food.sqlite` (`gtin:...` for a
food with a barcode).

**Never who.** The file holds no account, no handle and no identifier of a
person, only counts. Its layout is closed: the build refuses a file that has
a table or a column not listed above.

`uv run food-data kratheos-init` creates an empty file when there is none.

## Development

```sh
make test     # no network: publishers are played by a local web server
make lint
```

```
sources.toml             the sources, their files and their licences
kratheos.sqlite          the Kratheos layer
sources/                 stored originals of the small sources
src/kratheos_food_data/
    cli.py               command line
    build.py             the pipeline
    config.py            reads sources.toml
    download.py          resumable, checksummed downloads
    mirror.py            stored originals, fallback, split and join
    adapters/            one module per source, to the staging schema
    schema.py            staging and output columns, flags
    barcode.py           GTIN normalisation, in Python and SQL
    normalize.py         derivations and quality checks, same for every source
    aggregate.py         aggregation of the sources, then the Kratheos layer
    kratheos.py          layout and reading of kratheos.sqlite
    sqlite_export.py     writes food.sqlite and the identifier list
    names.py             the rule for the name to show
    lookup.py            barcode, identifier and name lookups
    fastfood.py          measures prepared dishes and restaurant chains
    manifest.py          writes manifest.json
tests/                   miniature sources and tests
```

To add a source: read its terms on the publisher's own page, add it to
`sources.toml` with the sentence that allows redistribution, write an adapter
that maps its files to the staging columns of `schema.py` (and says which
carbohydrate convention it follows), add a miniature copy to `tests/`. The
normalisation, the quality checks and the aggregation apply to it unchanged.

Requests are polite: one file at a time, a User-Agent naming the project
(add a contact with the `FOOD_DATA_CONTACT` environment variable), no API
scraping.
