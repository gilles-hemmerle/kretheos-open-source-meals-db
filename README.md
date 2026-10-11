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

The same repository holds a small [service](#the-service) that answers
searches, barcode lookups and contributions over HTTP from that database, so
that anyone can host it.

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
distinct barcodes; `food.sqlite` holds 3 564 569 foods. 17 755 of the generic
foods have [household portions](#household-portions), 44 116 in all: 27 771
from USDA for 12 117 foods, 16 345 from the Canadian Nutrient File for 5 638.
Ciqual, the Swiss table and CoFID publish none.

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
| `make serve` | Run the [service](#the-service) on `dist/food.sqlite` |
| `make test`, `make lint` | Tests (no network) and code style |
| `make clean` | Remove `work/` and `dist/`; the stored originals stay |

`uv run food-data --help` lists the commands behind these targets and their
options (`--only ciqual,swiss` to build some sources, `--min-free-gb` to
protect the disk, `--override`, see below). `food-data build --dist <dir>
--work <dir>` writes the database, and its scratch files, somewhere else
than `dist/` and `work/`: a way to try a build beside a database being
served from `dist/`.

## The sources and their licences

| id | Database | Publisher | Version built | Licence |
| --- | --- | --- | --- | --- |
| `off` | [Open Food Facts](https://world.openfoodfacts.org/) | Open Food Facts | Parquet export, rolling | ODbL 1.0 (contents: DbCL 1.0) |
| `usda` | [FoodData Central](https://fdc.nal.usda.gov/) | U.S. Department of Agriculture | Foundation and Branded 2026-04, FNDDS 2021-2023, SR Legacy 2018 | CC0 1.0 (public domain) |
| `ciqual` | [Ciqual](https://ciqual.anses.fr/) | Anses, France | 2025 | Licence Ouverte 2.0 (Etalab) |
| `swiss` | [Swiss Food Composition Database](https://naehrwertdaten.ch/) | FSVO, Switzerland | 7.1 (2026-07) | opendata.swiss "Open use. Must provide the source." |
| `cnf` | [Canadian Nutrient File](https://open.canada.ca/data/en/dataset/1b6139bd-ed7e-4043-bc28-ff00e10f3109) | Health Canada | 2026 | Open Government Licence - Canada |
| `cofid` | [CoFID](https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid) | Public Health England | 2021 | Open Government Licence v3.0 |
| `chain` | [Kratheos](#restaurant-chains): products of restaurant chains | Kratheos | the files of `sources/chains/` | none: see below |

`sources.toml` holds, for each source, the page where its terms were read,
the sentence that allows redistribution and the date it was checked. Two
databases are listed there but disabled, with the reason: the German BLS 4.0
(free to use, but no licence text and nothing on redistribution) and FoodRepo
(the service closed). A downloaded source is only enabled once its terms
clearly allow redistribution. `chain` is the one source that is not an open
database: the values restaurant chains publish for their products, compiled
by Kratheos and committed in `sources/chains/`
([Restaurant chains](#restaurant-chains)).

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
| `food.sqlite` | The main database, Kratheos layer applied, with every version of the foods that have several (see below) |
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
| `foods` | One row per food: the generic foods, one row per barcode, and the products without a valid barcode (restaurant items mostly). Columns of the common schema, plus `record_id` (the version whose values the row holds), `votes`, `versions_count`, `source_count`, `sources`, `other_records`, `sources_agree`. Lists are JSON text, booleans are 0 or 1. |
| `names` | Every name of every food, one row per language: `food_pk`, `lang`, `name`, `origin` (`source` or `kratheos`), `kratheos_origin` (`machine` or `human`, for a name added in Kratheos). |
| `foods_fts` | FTS5 index over the names of a food in every language it has, and its brand. Diacritics are ignored; prefixes of 2 and 3 letters are indexed. |
| `food_countries` | One row per food and country, keyed by country first; `countries` counts foods per country. |
| `languages` | The language codes present in `names`. |
| `display_names` | View applying the rule for the name to show. |
| `versions` | Every version of the values of the foods that have more than one: `food_pk`, `position` (order of the build), `version_id`, `origin` (`source` or `user`), `votes`, `served` (the one in `foods`), then its source, values, portion, countries, flags, link and date. See [Versions of a food](#versions-of-a-food). |
| `portions` | The household portions of the foods that have some, one row per portion: `food_pk`, `position` (those of the source in its order, then those added in Kratheos), `portion_id`, `origin` (`source` or `kratheos`), `labels` (JSON object: the label by language code), `amount` and `unit` (`g` or `ml`). See [Household portions](#household-portions). |
| `sources` | Version, licence, attribution and `priority` (the fixed order) of each source. |
| `meta` | Layout version (3; layout 2 is the same without `portions`), licence notice, the two helper queries below and `user_version_votes`. |

Indexes: `foods(id)` and `foods(barcode)` are unique; `versions(version_id)`;
`portions(portion_id)`.

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

### Versions of a food

A food can have several versions of its values: one per record of a source
that knows it (two sources share 352 584 barcodes, and disagree on 53 411 of
them), the product added in Kratheos under its barcode, and the versions
users propose. Nothing is averaged and nothing is overwritten: every version
is kept in `versions`, with the number of people who voted for it.

- **Who the food is** (`id`, `kind`, `barcode`, `name`, the names, `brand`,
  `category`, `is_drink`, `popularity`) comes from one record, the one the
  [aggregation rule](#what-is-in-dist) prefers among the sources; a product
  added in Kratheos only gives them when no source knows the barcode. It does
  not change with the votes.
- **Whose values are served.** A version is "usable" when its values are
  plausible and it has the four macros.
  1. *Threshold.* A version entered in Kratheos (`origin` `user`: a product
     added, a version proposed) with fewer than 3 votes is left out when a
     record of a source is usable. The number is a setting
     (`--user-version-votes`, `FOOD_DATA_USER_VERSION_VOTES`), recorded in
     `meta`. When no record of a source is usable (148 803 foods only have
     implausible source values), every such version counts: one person's
     plausible entry replaces them.
  2. *Country.* A service that knows the country of a person drops the
     versions that list other countries and keeps those that list the
     country or none, provided one of those listing it is usable, or nothing
     is usable at all: a country never trades usable values for values that
     are not.
  3. *Order*, the first wins: the most votes; plausible values; complete
     macros; listing the country asked for; a source before a user; then
     among sources the most recent, the fixed source order and the
     identifier, among users the oldest and the identifier.

  So a user's version needs strictly more votes than a source to pass it,
  and a later proposal strictly more than an earlier one.

The row of a food in `foods` holds the values served when no country is
asked for (`record_id` names the version), so someone who only reads `foods`
gets one coherent answer. With an empty layer that is the preferred record of
each barcode, exactly as a build without votes always was. `versions.position`
is the order of the build: steps 3 without the votes and the country.

```sql
SELECT v.version_id, v.origin, v.votes, v.served, v.countries, v.energy_kcal
FROM versions AS v JOIN foods AS f ON f.pk = v.food_pk
WHERE f.id = 'gtin:3017620422003' ORDER BY v.position;
```

`kratheos_food_data.versions.choose_served` is the rule in Python, country
included; the tests check that it and the SQL of the build agree.

## Identifiers: a promise

Favourites and diary entries of an application point at foods of this
database, so the identifier of a food (`foods.id`) never changes when the
database is rebuilt:

- **A food with a valid barcode is `gtin:<canonical barcode>`**, for instance
  `gtin:3017620422003`. It identifies the product itself, whichever version
  is served this time. The version behind its values is in `record_id`
  (`off:3017620422003`), the records of the other sources in `other_records`.
- **A version** is identified by the record of its source
  (`usda:branded:3017620422003`), or by `kratheos:v:<id>` when a user
  proposed it. These are stable too: votes are kept by version.
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
| `chain` | chain, country and the key the site gives the product, `chain:burger-king:ch:<key>` | The key is the one of the chain's own site. A site that gives none leaves the name: the product keeps its identifier as long as the chain keeps the name. |
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
| `source` | text | `off`, `usda`, `ciqual`, `swiss`, `cnf`, `cofid`, `chain`, `kratheos` |
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

### Household portions

`serving_size`, `serving_unit` and `serving_text` hold the one serving a
record gives of itself: the serving of the label of a packaged product. The
food-composition tables that publish household portions give several per
food ("1 large = 50 g", "1 cup", "1 slice"), which is what a person needs
to log an egg without a scale. They are read by the adapters into a second
staging file (`source_record_id`, `position`, `labels`, `amount`, `unit`)
and written to the table `portions` of `food.sqlite`:

| Source | What it publishes | What is read |
| --- | --- | --- |
| USDA Foundation Foods | a number, a measure and sometimes a precision | "1 cup, shredded", in English |
| USDA SR Legacy | a number and the measure as text | "1 cup, chopped", "0.5 fillet", in English |
| USDA FNDDS | the whole wording | "1 extra large egg", in English |
| Canadian Nutrient File | measures worded in English and in French | "1 large egg" / "1 oeufs large (gros)", "250 ml" |
| Ciqual, Swiss table, CoFID | nothing | nothing |

Rules, the same for every source:

- the label is the one of the source, in the languages it gives, never
  translated; the amount is the weight in grams the source gives for the
  label as a whole ("0.5 fillet" weighs what the file says). No source
  gives a portion as a volume of its own, so every amount read is in `g`;
- the order is the one of the source (its sequence number, or the order of
  the lines of the Canadian file);
- nothing is guessed: a portion is left out, and counted in the manifest,
  when it has no weight, no wording (FNDDS writes "Quantity not specified"
  for the amount it assumes when a survey gives none), when its measure is
  a weight taken in the laboratory (Foundation Foods: a patty weighed raw,
  then cooked), when the same line comes twice, or when its label is only a
  weight ("100 g", "4 oz", "1 lb"): that says nothing about the food. A
  volume stays ("250 ml", "1 fl oz"): its weight is the one of this food;
- a portion belongs to the food its record is. Nothing is borrowed from the
  "same" food of another table, and no weight is computed for a food
  without one: the records of Ciqual and of the Swiss table get theirs from
  the people who add them ([the Kratheos layer](#the-kratheos-layer)).

`manifest.json` says how many foods and portions each source brought
(`statistics.foods.portions`) and, source by source, what was read, kept and
left out (`sources[].adapter.portions`).

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
(an in-store number, not unique across retailers).

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

The [service](#the-service) adds two flags of its own to a food record:
`versions_disagree`, when the versions of a food give different values, and
`in_store_barcode`, when its barcode is a number a shop puts on what it
weighs or packs itself (GTIN-13 beginning with 02, 04 or 20 to 29, and the
8-digit numbers beginning the same way).

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
- **`sources/chains/`**, committed: the files of the source `chain`, which
  no publisher offers for download ([Restaurant chains](#restaurant-chains)).
  They have no sidecar: the manifest of a build records their checksums.
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
   with the countries given at creation. One with a barcode a source also
   knows becomes one more [version](#versions-of-a-food) of that food.
2. **`versions`**: other values proposed for a food of any source, with
   their own portion and countries. A version replaces nothing: it stands
   beside the records of the sources, under `kratheos:v:<id>`.
3. **`votes`**: how many people voted for a version, whether it is a record
   of a source, a product added in Kratheos or a version proposed by a user.
4. **`translations`**: a name for a food in a language it lacks. A
   translation never replaces a name given by the source; `names.origin`
   tells the two apart.
5. **`portions`**: a household portion people added to a food ("1 oeuf
   moyen", 50 g), with its label in the language it was written in. It is
   offered to everyone after the portions the source gives, oldest first,
   under `kratheos:p:<id>`; `portions.origin` tells the two apart.

| Table | Columns |
| --- | --- |
| `meta` | `key`, `value`: `schema_version` (3), `exported_at` |
| `translations` | `record_id`, `lang`, `name`, `origin` (`machine` or `human`), `updated_at` |
| `products` | `id`, `kind`, `barcode`, `name`, `name_lang`, `names` (JSON), the value columns, `countries` (JSON), `updated_at` |
| `versions` | `version_id`, `food_id`, the columns of a version, `countries` (JSON), `updated_at` |
| `votes` | `version_id`, `food_id`, `votes`, `updated_at` |
| `portions` | `portion_id`, `food_id`, `lang`, `label`, `amount`, `unit` (`g` or `ml`), `updated_at` |

The value columns of a product are those of the common schema: `brand`,
`category`, `basis`, `energy_kcal`, `protein_g`, `carbohydrate_g` (available
carbohydrate), `sugars_g`, `fat_g`, `saturated_fat_g`, `fiber_g`, `salt_g`,
`alcohol_g`, `serving_size`, `serving_unit`, `serving_text`, `is_drink`. The
columns of a version are the same without `brand`, `category` and `is_drink`:
a version says what a food contains, not what it is. `record_id` and
`food_id` are the identifier of a food in `food.sqlite` (`gtin:...` for a
food with a barcode); `version_id` is a version of it.

**Never who.** The file holds no account, no handle and no identifier of a
person: who proposed a version, added a portion or voted is not in it, only
counts.
Its layout is closed: the build refuses a file that has a table or a column
not listed above.

`uv run food-data kratheos-init` creates an empty file when there is none.
The file is written by the service ([the weekly export](#the-weekly-export));
nobody edits it by hand.

## Restaurant chains

Restaurant chains publish the nutrition values of their products. Kratheos
compiles them, one chain and one country at a time, and the source `chain`
reads them from the CSV files committed in `sources/chains/`, one per chain
and country (`mcdonalds_fr.csv`). The build never downloads them. The source
is presented under the name **Kratheos**: these are the values the chains
publish for their products, compiled by Kratheos. The repository ships 35
files, 16 chains in France, Switzerland, Germany, Italy and Spain, which give
3 837 products. Without a file the source is there and empty.

Columns read (a file may hold more): `chain`, `country`, `language`,
`product_name`, `category`, `product_key`, `portion_size`, `portion_unit`
(`g` or `ml`), `is_drink` (`yes` or `no`), `energy_kcal_per_100`,
`protein_g_per_100`, `carbohydrate_g_per_100`, `sugars_g_per_100`,
`fat_g_per_100`, `saturated_fat_g_per_100`, `fibre_g_per_100`,
`salt_g_per_100`; and, when the file has them, `date_collected` and the eight
`..._per_portion` columns.

The committed files hold more than the build reads: the values per portion,
what the chain gave (`values_given`: per portion, per 100, or both; a cell the
chain does not publish is computed from the other basis and the weight of the
portion), where each row was read (`source_url`) and what a check found odd
in it (`sanity_flags`). They are the trace of the collection. `source_url`
stays there: a record of the database links to no page of a chain.

Each line becomes a product without barcode: the chain is its brand, the
country of the file its country, the name is in the language of the file and
`date_collected` is its date. Its identifier is `chain:<chain>:<country>:<key>`,
with the key the chain gives the product, or its name when the chain gives
none; lines of a file that share an identifier are one product, the first.
Values are per 100 g, or per 100 ml when the portion is in ml. The portion of
a product is the one its chain gives, when the chain gives its weight.
Carbohydrate is available carbohydrate, except for the United States and
Canada, where menus give the total and fibre is subtracted. Two files of the
same chain and country in two languages give one product with two names.

A line whose values are given per portion only, for a portion the chain
publishes no weight for, cannot be expressed per 100 g: it is left out and
counted per file (`rows_per_portion_only` in the manifest), and no weight is
ever guessed. A line without energy is dropped and counted; a line whose
values make no sense is kept and flagged, like any record.

Records of this source carry the licence `publisher-terms`: no open licence.
Someone who publishes a database built with these files publishes those
values with it.

## The service

`food-data serve` answers the API over HTTP: search as a person types,
lookup by barcode, read by identifier, and the contributions of users
(products added, versions of the values proposed, votes, names, household
portions, reports).
[`docs/api.md`](docs/api.md) is its reference: the contract agreed with the
Kratheos backend, and how the service implements it.

```sh
make build                              # or install a ready-made database, below
make serve TOKEN=a-long-shared-secret   # http://127.0.0.1:8750
curl 'http://127.0.0.1:8750/v1/foods/search?q=pain+compl&lang=fr&country=CH'
curl 'http://127.0.0.1:8750/v1/barcodes/3017620422003?lang=fr'
```

One process, the Python standard library, SQLite and nothing else to
install. It is meant to run on a private address, behind the application
that authenticates people:

- **reads need no authentication**; writes and moderation need the shared
  token (`Authorization: Bearer ...`). Without a token the service only
  answers reads;
- it listens on `127.0.0.1` unless told otherwise, and knows no person: only
  the opaque key the application sends, which it stores hashed;
- every request is bounded (64 KiB of body) and validated; values go through
  the same quality checks as the build before they are accepted.

**Search** uses the stored full-text index, within a time budget (half a
second by default, a few tens of milliseconds for most texts). Two letters
match hundreds of thousands of barcoded products, and ranking them is what
takes a second, so with fewer than three letters or digits only the generic
foods and the products without barcode are searched (160 000 foods): two
letters are the beginning of a word, a single letter is a whole word. From
the third letter on everything is searched.

The order of the results is made of rules a person can predict, with no list
of foods. The generic foods of the food-composition tables (`kind=generic`)
are all read and ordered on the words of their names: a name in the language
of the request, a name that is or begins with the text typed ("Pomme, chair
et peau, crue" for "pomme", before "Pomme de terre" and long before "Compote
de pomme"), the table of the country, then raw and fresh forms before cooked
ones, and dried, canned or pre-packed ones last. Products are ordered by
country, name and number of scans; the 13-digit numbers shops put on what
they weigh and pack themselves (most of the `in_store_barcode`, one food in
ten) come after every other product, and are found by barcode as before;
the 8-digit ones, own brands of a few retailers, rank like any product. Without `kind`, the
generic foods whose name is the text typed come first, then the products.
[`docs/api.md`](docs/api.md#search) has the details.

### Its files

Everything is in one data directory (`FOOD_DATA_DIR`; `data/` in the
repository by default, never committed):

| File | What it holds | Leaves the server? |
| --- | --- | --- |
| `food.sqlite` | The main database being served, read-only. Replaced in one step by a rebuild or an installation. | It is the public database. |
| `manifest.json`, `identifiers.txt.gz` | The manifest and the identifier list of that build; the build date served comes from the manifest. | Public. |
| `live.sqlite` (and `-wal`, `-shm` while running) | Everything entered through the service: products, versions, names, portions; and what is private: the votes by hashed contributor key, the reports, what moderation hid, the history of writes (which hashed key wrote what), the keys moderation blocked and what is hidden with them. Mode 600. | Never as it is. Only through the export below. |
| `contributor.key` | The secret the contributor keys are hashed with (HMAC-SHA-256), when `FOOD_DATA_KEY_SECRET` does not give it. Mode 600. | Never. |
| `rebuild.json` | The report of the last rebuild. | No. |

`live.sqlite` is the only copy of what was entered since the last export:
back it up (`sqlite3 live.sqlite ".backup copy.sqlite"`, safe while the
service runs). A live database that does not exist yet is created, and takes
over what the `kratheos.sqlite` of the repository holds, so that a service
started from a published layer goes on from it; `food-data import-layer`
does the same later, adding what the live database lacks and changing
nothing it has.

What is written is visible at once, in search, by barcode and by identifier:
every read puts the main database and the live database together. Once a
rebuild has folded the live data into `food.sqlite`, both hold the same
versions, the same names and the same portions, each kept once, and the
votes of a food are those of the live database alone: nothing is counted
twice.

**Portions.** A food record lists the portions of the food: the serving of
the label of a packaged product, the [household portions](#household-portions)
its source publishes, then those people added, each with its label in the
language of the request (else English, else any). Anyone can add one to any
food (a label in a language, an amount in g or ml): it is refused when the
food already has it (the same label once accents, case and spaces are put
aside, and the same amount within a gram), when it weighs less than 1 or
more than 2000, and beyond ten added portions per food.

**Moderation** needs the token too. A food is reported once per person, and
the list of the reported foods tells how many different people reported each
and why, the most reported first; reports hide nothing by themselves, a
moderator hides or restores a food, one version or one portion. The
products, the versions, the names and the portions that are published carry
no author: the history kept in `live.sqlite` is the only link to the hashed
key that wrote them. With
it a moderator sees what one key wrote, hides all of it and refuses its
later writes in one call (its votes stop counting), and undoes both, which
gives back exactly what was served before.
[`docs/api.md`](docs/api.md#moderation-one-contributor-key) has the calls.
A `live.sqlite` written by an earlier version is brought to the current
layout when the service opens it; nothing stored is changed.

### Settings

| Variable | Option | Default | |
| --- | --- | --- | --- |
| `FOOD_DATA_DIR` | | `data/` | The data directory |
| `FOOD_DATA_DATABASE` | `--database` | `food.sqlite` in it | The main database |
| `FOOD_DATA_LIVE` | `--live` | `live.sqlite` in it | The live database |
| `FOOD_DATA_TOKEN` | | none | Shared secret of writes and moderation, 16 characters at least |
| `FOOD_DATA_KEY_SECRET` | | `contributor.key` | Secret the contributor keys are hashed with |
| `FOOD_DATA_HOST`, `FOOD_DATA_PORT` | `--host`, `--port` | `127.0.0.1`, `8750` | Where to listen |
| `FOOD_DATA_USER_VERSION_VOTES` | `--user-version-votes` | 3 | Votes a user's version needs to be served instead of a source; also read by the build |
| `FOOD_DATA_SEARCH_BUDGET_MS` | | 500 | Time a search may spend in the index |
| `FOOD_DATA_SEARCHES` | | 3 | Searches ranked at the same time; the others wait their turn |

### The weekly export

```sh
uv run food-data export-layer            # writes kratheos.sqlite from the live database
uv run food-data export-layer --commit   # and commits it, alone, with a dated message
uv run food-data export-layer --commit --push   # and pushes, when a remote exists
```

The file written has the [layout of the layer](#the-kratheos-layer) and
holds what users see and nothing else: no contributor key, hashed or not, no
report, nothing moderation hid (a hidden food is left out with everything
attached to it, a hidden version with its votes, a hidden portion alone),
nothing a blocked key wrote and none of its votes. Votes are counts. The same
live data gives the same file, byte for byte, so a week without a change
gives no commit; `--commit` commits that file only, whatever else changed in
the repository. An export refuses to replace a layer that holds data by an
empty one, or by one with a fifth fewer products, versions, names or
portions (`--force` to do it anyway): that is what a live database that lost its data,
or never took the layer over, would do.

### The periodic rebuild

```sh
uv run food-data rebuild                 # export, rebuild, compare, swap
uv run food-data rebuild --from-mirror   # the same from the stored originals only
```

1. exports the live database to `kratheos.sqlite`;
2. builds into `work/rebuild/`, never into the files being served;
3. compares the result with the database being served: foods, foods per
   source, and the identifiers (`ids-diff`);
4. **refuses** a result that loses more than 2 % of the foods or of the
   identifiers (`--max-loss`), or a fifth of the foods of one source: the old
   file stays, the new one is left in `work/rebuild/` with a report, and the
   command ends with status 2;
5. otherwise moves the new file over the old one in one step. The running
   service notices, opens it, and goes on: requests being answered finish on
   the old file, no request is lost and nothing is restarted.

### A ready-made database

Someone who hosts the service does not need the 8 GB of originals:

```sh
uv run food-data database-files          # lays out release-database/ to publish
uv run food-data install-database --url https://example.org/food/v1
```

`database-files` writes what to publish under one address: the database (in
parts when it exceeds what a release accepts), its manifest, the identifier
list, the layer it was built with, and `DATABASE.json`, which gives the
checksum of each. `install-database` downloads them (resuming an interrupted
download), verifies every checksum, applies the comparison of a rebuild
(`--force` to install anyway), takes the layer over into the live database
and swaps the file in the same way.

### Hosting

**Debian.** `debian/` builds the package `kratheos-food-data` for Debian
trixie (`dpkg-buildpackage -us -uc -b`, or the builder described by
`builder.yml`): code and its Python environment in
`/usr/share/kratheos-food-data`, settings in
`/etc/kratheos-food-data/service.conf` (token and key secret generated at the
first installation), data in `/var/lib/kratheos-food-data`, an unprivileged
user, a sandboxed service and two timers: the export every Monday, the
rebuild on the first Sunday of the month. The package ships no data:

```sh
sudo -u kratheos-food-data git clone <this repository> /var/lib/kratheos-food-data/repository
sudo food-data import-layer                            # takes over the layer of the clone
sudo systemctl start kratheos-food-data-rebuild        # builds from the sources (9 GB), or:
sudo food-data install-database --url https://example.org/food/v1
```

`/usr/bin/food-data` is the command of the package: it reads the settings of
`service.conf`, works as the service user even when root types it, and takes
the clone as its repository.

The timers only run once `/var/lib/kratheos-food-data/repository` exists: it
is the clone that holds `sources.toml`, the stored originals and
`kratheos.sqlite`, where the export is committed and the rebuild works.

**Docker.** One directory of the host holds all the data:

```sh
docker build -t kratheos-food-data .
docker compose -f compose.example.yml up -d
docker compose -f compose.example.yml run --rm food \
    install-database --url https://example.org/food/v1
```

`compose.example.yml` mounts `./data` of the host on `/data`: the databases
live on the host, never in a volume of Docker. The port is published on
`127.0.0.1` only.

## Development

```sh
make test     # no network: publishers are played by a local web server
make lint
```

```
sources.toml             the sources, their files and their licences
kratheos.sqlite          the Kratheos layer
sources/                 stored originals of the small sources
docs/api.md              the API of the service
debian/, builder.yml     the Debian package
Dockerfile, compose.example.yml
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
    versions.py          the rule choosing the version served
    plausibility.py      the quality checks, for one food at a time
    sqlite_export.py     writes food.sqlite and the identifier list
    names.py             the rule for the name to show
    lookup.py            barcode, identifier and name lookups
    fastfood.py          measures prepared dishes and restaurant chains
    manifest.py          writes manifest.json
    service/             the HTTP service
        settings.py      settings, from the environment and the options
        catalogue.py     food.sqlite, read-only, replaced without a pause
        live.py          the live database
        foods.py         a food from both, and its JSON record
        search.py        search in bounded time
        ranking.py       the order of the reference foods, from their names
        validation.py    what a request may contain
        api.py           the endpoints
        httpd.py         the HTTP server
        export.py        live database to kratheos.sqlite, and the commit
        maintenance.py   rebuild, comparison, swap, ready-made database
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
