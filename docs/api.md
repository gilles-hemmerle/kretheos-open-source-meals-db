# The API of the food service

This document is the reference of the HTTP API served by `food-data serve`.

Its first part is the contract agreed with the Kratheos backend, the only
client of the service in production, copied as it was written (version 2)
and amended since for the household portions: `servings` now lists several
portions, and `POST /v1/foods/{id}/portions` adds one. Nothing else moved,
so the version stays 2: a client that reads the first portion of `servings`
as before still works.
Its second part says how this service implements it: what is added, where
the data cannot say what the contract asks and what is served instead, how
search behaves, the errors, the limits.

## Contract between the food service and the Kratheos backend (version 2)

Working note shared by the agents building each side. The food service lives in the
repository `kratheos-food-data`; the Kratheos backend is its only client in production.

### Roles

- The food service reads `food.sqlite` (built from the open sources and the Kratheos layer)
  and owns the live Kratheos layer (products added, versions of the values proposed by
  users, votes, translations, household portions, reports). It knows no person: only an
  opaque contributor key.
- The Kratheos backend authenticates the person, checks consent and rate limits, converts
  nothing (the service is metric only), and copies the values of a food into the person's
  diary, favourites and recipes. The app never calls the food service directly.

### Transport

- JSON over HTTP on a private address. Reads need no authentication. Writes and moderation
  need `Authorization: Bearer <service token>` (one shared secret, set on both sides).
- Errors: `{"code": "<stable_code>", "message": "<English sentence>"}` with a 4xx or 5xx status.
- All quantities are metric: values per 100 g or per 100 ml, energy in kcal, macros and salt
  in grams, portions in g or ml. Carbohydrate is available carbohydrate (fibre excluded).

### Food record

```json
{
  "id": "gtin:3017620422003",
  "kind": "product",
  "barcode": "3017620422003",
  "names": {"fr": "Pâte à tartiner", "en": "Hazelnut spread"},
  "name": "Pâte à tartiner",
  "name_language": "fr",
  "name_origin": "source",
  "brand": "Ferrero",
  "category": "Spreads",
  "countries": ["FR", "CH"],
  "basis": "100g",
  "is_drink": false,
  "nutrients": {
    "kcal": 539, "protein_g": 6.3, "carbs_g": 57.5, "sugars_g": 56.3,
    "fat_g": 30.9, "saturated_fat_g": 10.6, "fibre_g": null, "salt_g": 0.107,
    "alcohol_g": null
  },
  "servings": [{"label": "15 g", "amount": 15, "unit": "g"},
               {"label": "1 cuillère à soupe", "amount": 20, "unit": "g"}],
  "source": {"id": "off", "title": "Open Food Facts", "licence": "ODbL-1.0", "url": "https://..."},
  "version_id": "off:3017620422003",
  "version_origin": "source",
  "votes": 0,
  "versions_count": 2,
  "flags": [],
  "updated_at": "2026-09-30T00:00:00Z"
}
```

- `id` never changes across rebuilds. Shapes: `gtin:<digits>` for a barcoded product,
  `<source>:<id in the source>` for a generic food, `kratheos:<uuid>` for a product created
  in Kratheos without a barcode. Treat it as an opaque string of at most 80 characters.
- `kind`: `generic` or `product`. `basis`: `100g` or `100ml`.
- `names` holds every language the record has. `name` is resolved for the `lang` of the
  request: that language, else English, else any; `name_language` says which one was used and
  `name_origin` whether it comes from the source or from Kratheos (`source` or `kratheos`).
- `nutrients` values are numbers or null when unknown.
- `servings` lists the portions the food offers, each `{label, amount, unit}` with the amount
  in g or ml: the portion of the version served (the serving of a label), when it has one,
  then the household portions of the food: those its source publishes ("1 large", 50 g),
  in the order of the source, then those people added, oldest first. `label` is resolved
  like `name`: the `lang` of the request, else English, else any. May be empty.
- `source.id`: `off`, `usda`, `ciqual`, `swiss`, `chain`, `kratheos`, or another source id.
  It is the source of the version served (see « Versions and votes »).
- `version_id`, `version_origin` (`source` or `user`), `votes`: the version of the values
  served, where it comes from, and how many people voted for it.
- `versions_count`: how many versions of the values this food has (1 when there is no choice).
- `flags`: quality flags (`implausible_energy`, `carbs_converted`, ...); may be empty.
  `versions_disagree` is set when the food has several versions whose values differ.

### Versions and votes

A food can have several versions of its values: one per source that knows it (Open Food
Facts, USDA...) and those proposed by users. Nothing is ever averaged or overwritten.
People vote for the version they find right; the version with the most votes is the one
served.

```json
{
  "version_id": "usda:branded:3017620422003",
  "origin": "source",
  "source": {"id": "usda", "title": "USDA FoodData Central", "licence": "CC0-1.0", "url": "https://..."},
  "countries": ["US"],
  "basis": "100g",
  "nutrients": {"kcal": 541, "protein_g": 5.4, "carbs_g": 56.8, "sugars_g": 54.1,
                "fat_g": 32.4, "saturated_fat_g": 10.8, "fibre_g": 2.7, "salt_g": 0.1},
  "servings": [{"label": "2 tbsp (37 g)", "amount": 37, "unit": "g"}],
  "votes": 3,
  "served": false,
  "flags": [],
  "updated_at": "2026-04-01T00:00:00Z"
}
```

- `version_id`: the record id of the source (`off:...`, `usda:branded:...`) or
  `kratheos:v:<uuid>` for a version proposed by a user (`origin` `user`, source `kratheos`).
- One vote per person and per food: voting again moves the vote. Proposing a version puts
  its author's vote on it.
- Which version is served to a request (a version is "usable" when its values are plausible
  and it has the four macros):
  1. threshold: a `user` version with fewer than 3 votes (a setting of the service) is left
     out when the food has at least one usable `source` version; when no source version is
     usable, every user version counts;
  2. country: when the request gives a `country` and at least one candidate listing it is
     usable (or no candidate at all is usable), the candidates listing other countries are
     dropped; those listing the country or no country at all stay;
  3. order among the candidates, first wins: most votes; plausible values; complete macros;
     lists the country asked; a source version before a user version; among sources the most
     recent, then the fixed source order; among user versions the oldest.
- `served` marks, in a list of versions, the one this request gets.
- `nutrients.alcohol_g` (grams per 100 g or 100 ml, null when unknown) is part of every
  record and version and is accepted on writes; the energy check of the writes counts it.
- `is_drink` false means "not known to be a drink". `flags` are free strings of the build,
  except `versions_disagree`. `servings` of a version holds at most one portion, its own;
  `servings` of a food record holds every portion of the food (see « Food record »).
- The service refuses unknown fields and unknown query parameters (422).

### Read endpoints

- `GET /v1/foods/search?q=<text>&lang=fr&country=FR&kind=&drink=&limit=20&offset=0`
  returns `{"items": [<food record>], "next_offset": 20 | null}`. Matches any name in any
  language, the brand and, when `q` is only digits, the barcode. Records of `country` rank
  first. `kind` and `drink` (`true` / `false`) filter. `limit` at most 50.
- `GET /v1/foods/{id}?lang=fr&country=FR` returns a food record, or 404 `not_found`.
- `GET /v1/foods/{id}/versions?country=FR` returns `{"versions": [<version>]}`, the served
  one first, then by votes.
- `GET /v1/barcodes/{code}` returns the food record of a barcode (any GTIN-8, 12, 13 or 14,
  normalised by the service), or 404 `not_found`, or 422 `invalid_barcode`.
- `GET /v1/meta` returns the build date, the sources with their version, licence and
  attribution text, and the counts.

### Write endpoints (service token)

Every write carries `"contributor": "<opaque key>"`: an HMAC of the account id computed by
the Kratheos backend with its own secret. The service uses it only to count a person once
and never exports it.

- `POST /v1/products` creates a product: `{"barcode": null | "...", "names": {"fr": "..."},
  "brand": null | "...", "category": null | "...", "countries": ["FR"], "basis": "100g",
  "is_drink": false, "nutrients": {...}, "servings": [...], "contributor": "..."}`.
  Returns the food record (201). 409 `barcode_exists` with the existing record id when the
  barcode is already known; 422 `implausible_values` with the failed checks.
- `POST /v1/foods/{id}/versions` with `{"basis": "100g", "nutrients": {...},
  "servings": [...], "countries": ["FR"], "contributor": "..."}` proposes other values: a new
  `user` version, with its author's vote. Returns `{"versions": [...]}` (201). 422
  `implausible_values`; 409 `version_exists` with the id of a version that already has
  exactly these values (vote for it instead).
- `POST /v1/foods/{id}/votes` with `{"version_id": "...", "contributor": "..."}` gives or
  moves the person's vote. Returns `{"versions": [...]}`. 404 `not_found` for an unknown
  version.
- `POST /v1/foods/{id}/translations` with `{"language": "es", "name": "...",
  "contributor": "..."}`. A name given by the source is never replaced: 409 `name_exists`.
- `POST /v1/foods/{id}/portions?lang=fr&country=FR` with `{"language": "fr", "label":
  "1 œuf moyen", "amount": 50, "unit": "g" | "ml", "contributor": "..."}` adds a household
  portion to a food, for everyone. Returns `{"portion_id": "kratheos:p:<uuid>", "food":
  <food record>}` (201), the record as `lang` and `country` read it. 409 `portion_exists`
  with the `portion_id` of the portion the food already has (same label once case, accents
  and spaces are put aside, same amount within 1 g or ml); 422 `implausible_portion` for an
  amount under 1 or above 2000; 409 `too_many_portions` beyond 10 added portions per food.
- `POST /v1/foods/{id}/reports` with `{"reason": "wrong_values" | "duplicate" |
  "inappropriate" | "other", "note": "", "contributor": "..."}`. Returns 202.

### Moderation endpoints (service token)

- `GET /v1/moderation/reports?state=open` lists the reports with the record concerned.
- `POST /v1/moderation/foods/{id}/hide` and `/restore`: a hidden record is returned by no
  read endpoint and is left out of the public export.
- `POST /v1/moderation/reports/{report_id}/close`.

## How this service implements the contract

### Where the data and the contract do not match

Each point says what the contract asks, what the data hold, and what is
served.

1. **`flags` are free strings of the build**, except the two the service
   adds, `versions_disagree` and `in_store_barcode`:
   show them or test for one, never expect a closed list. They carry the
   names of the build, not those the contract gives as examples
   (`implausible_energy`, `carbs_converted`). Today the quality flags are `value_out_of_range`, `energy_out_of_range`,
   `sugars_exceed_carbohydrate`, `saturated_exceed_fat`,
   `macros_sum_over_100`, `energy_macro_mismatch`; the informational ones
   `energy_from_kj`, `salt_from_sodium`, `alcohol_from_abv`, `basis_assumed`,
   `carbohydrate_fiber_subtracted`, `carbohydrate_includes_fiber`,
   `inexact_values`, `obsolete`, `barcode_invalid`, `barcode_restricted`.
   The README describes each.
2. **`versions_disagree`** is set when at least two visible versions have
   the four values (energy, protein, carbohydrate, fat) and either do not use
   the same basis (100 g and 100 ml) or differ beyond the tolerance the build
   already uses between sources: energy by more than 10 kcal and 10 %, or a
   macro by more than 2 g and 20 %. Versions that differ by the rounding of a
   label (539 and 541 kcal) do not raise it. It is a flag of the food record,
   never of a version.
3. **`in_store_barcode`** is set on a food whose barcode is a number a shop
   puts on what it weighs or packs itself: a GTIN-13 beginning with 02, 04
   or 20 to 29 (02 and 04 are the 12-digit UPC-A numbers beginning with 2
   and 4), or an 8-digit number beginning the same way. Such a number names
   another food in the next shop. About one food in ten carries it (380 000,
   nearly all from Open Food Facts). The record is served like any other and
   a lookup by barcode is unchanged; a search by name puts those of 13
   digits after every other product (see « Search »). It is a flag of the
   food record, never of a version. The build has a wider flag on the record of the
   source, `barcode_restricted`: the GS1 restricted circulation numbers,
   which also hold every 8-digit number beginning with 0, the own-brand
   products of a few retailers (Trader Joe's, Sainsbury's, Marks & Spencer,
   Tesco), which rank like any product.
4. **`is_drink` false means "not known to be a drink"**, never "known not to
   be one". The database holds three states (yes, no, unknown: 1.8 of 3.6
   million foods) and the contract a boolean. `drink=true` returns what a
   source classifies as a beverage, `drink=false` everything else.
5. **`servings` holds several portions in a food record, and at most one
   in a version.** A version has the one portion its record gives (the
   serving of a label); the layer stores one per version too. A request
   that creates a product or proposes a version may send several: the first
   is kept, the others are ignored, and the answer shows what was kept. A
   record whose source words a portion without giving its size has none.
   When the source gives no label, the label is the size (`"30 g"`).
   A food record lists that portion of the version served first, then the
   household portions of the food (table `portions` of `food.sqlite`, and
   those added since the build). Those belong to the food, not to a
   version: they do not change with the votes or the country. The sources
   that publish some are USDA (in English) and the Canadian Nutrient File
   (in English and in French); a food of Ciqual, of the Swiss table or of
   CoFID has those people added, or none. Their amounts are weights in
   grams, whatever the label says ("250 ml" of a food of the Canadian file
   weighs what the file gives); a portion people add is in `g` or in `ml`
   as they say. The service never converts one into the other: a client
   uses the portions whose unit it can count the food in.
6. **`updated_at` is a day**, written at midnight UTC: the sources and the
   layer store dates.
7. **Alcohol.** `nutrients.alcohol_g` (grams per 100 g or 100 ml) is part
   of every food record and every version, `null` when unknown, and is
   accepted on `POST /v1/products` and `POST /v1/foods/{id}/versions`. The
   energy check counts it (7 kcal per gram): send it for alcoholic drinks,
   or their energy will not match their macros. On a write it may be left
   out, unlike the other nutrients: a version proposed without it keeps the
   alcohol the food is known with; an explicit `null` says it is unknown.
8. **`name_origin`.** `kratheos` marks a name added with
   `POST .../translations`. The names a product was created with in Kratheos
   are its own names: their origin is `source`, and they cannot be replaced
   either.
9. **`id` of a product without barcode coming from a source** is
   `<source>:<id in the source>` too (`off:...`, `chain:...`,
   `usda:branded:...`), not only that of a generic food. No identifier is
   longer than 80 characters.
10. **The build date** is not inside `food.sqlite` (two builds of the same
   inputs give the same file). `GET /v1/meta` reads it from the
   `manifest.json` kept next to the database, and falls back to the
   modification time of the file.
11. **`brand` and `category`** are as the source writes them: a brand may
    list several names (`"Nutella, Ferrero"`), a category is the label of the
    source's own classification (`"en:hazelnut-spreads"`).

### Versions and votes, precisely

- **What a food is and what its values are.** `id`, `kind`, `barcode`,
  `names`, `brand`, `category` and `is_drink` say who the food is: they come
  from one record (the one the build prefers among the sources) and do not
  change with the votes or the country. `countries`, `basis`, `nutrients`,
  `servings`, `source`, `flags`, `updated_at`, `version_id`, `version_origin`
  and `votes` are those of the version served to the request.
- **`origin`.** `source` for a record of a source; `user` for everything
  entered in Kratheos (source `kratheos`): a version proposed by a user
  (`kratheos:v:<uuid>`) and the values a product was created with
  (`kratheos:<uuid>`). Creating a product puts its author's vote on it.
- **The rule**, as `versions.choose_served` applies it. A version is
  "usable" when its values are plausible (no quality flag) and it has the
  four macros (energy, protein, carbohydrate, fat).
  1. *Threshold.* A `user` version with fewer votes than the threshold is
     left out when the food has at least one usable `source` version. This
     comes before the country, so that such a version is never served
     instead of a usable source, even to the only country that lists it.
     When no `source` version is usable (there is none, or all are
     implausible or incomplete), every `user` version counts: one person's
     plausible entry replaces values the build itself flags.
  2. *Country.* When the request gives a `country`, the candidates that list
     other countries are dropped; those that list the country, or no country
     at all, stay. This only happens when at least one candidate listing the
     country is usable, or when no candidate at all is usable: a country
     never trades usable values for values that are not. It does not happen
     either when it would leave no candidate (nothing is usable and every
     version lists other countries): then all of them stay.
  3. *Order among the candidates*, the first wins: the most votes; plausible
     values; complete macros; listing the country asked for; a `source`
     version before a `user` version; then, among `source` versions, the
     most recent, the fixed source order and the identifier, and among
     `user` versions the oldest and the identifier.
- **What follows from the order.** A `user` version needs strictly more
  votes than a `source` version to pass it, and a later proposal strictly
  more votes than an earlier one. One vote is enough to switch between two
  `source` versions. Dates are days: two proposals of the same day are
  ordered by their identifier, which does not change.
- **Versions without a country.** A version that lists no country is not
  taken to be "elsewhere": it stays a candidate for every country, behind a
  version that lists the country when votes and quality are equal. Send
  `countries` with every version proposed all the same: it is what puts a
  version in front for its market.
- **Votes belong to the food, not to a country**: there is one count per
  version, and the rule compares the counts of the candidates left for the
  request.
- **The threshold** is `FOOD_DATA_USER_VERSION_VOTES` (3 by default), the
  same for the service and for the build, which records it in
  `meta.user_version_votes` of `food.sqlite`.
- **`country` on the writes.** `POST /v1/foods/{id}/versions` and
  `POST /v1/foods/{id}/votes` accept `?country=`, like the read, so that
  `served` in their answer is the one of the person. `POST /v1/products`,
  `POST /v1/foods/{id}/translations` and `POST /v1/foods/{id}/portions`
  accept `?lang=` and `?country=`.
- **`version_exists`** compares `basis` and the nine nutrients, after the
  rounding of the build (4 decimals), with every visible version of the
  food. Portions and countries are not values: proposing the same values
  with another portion is refused.
- A food can have at most 20 versions proposed by users (409
  `too_many_versions`).
- **Voting.** The answer of a vote for the version a person already votes
  for is the same list, unchanged. Votes taken over from a `kratheos.sqlite`
  written elsewhere are counts without a key: they count, and nobody can
  move them.

### Portions added by people, precisely

- **Identifiers.** A portion added through the service is
  `kratheos:p:<uuid>`. A portion of a source is `<food id>:p:<position>`
  (`cnf:125:p:1`): its place in the order of the source, which may move
  when the publisher reorders its file. Neither is part of `servings`; they
  serve `portion_exists` and moderation.
- **The same portion.** `portion_exists` compares the new portion with every
  visible portion of the food, of the source or added: the same unit, an
  amount within 1 (g or ml), and a label that is the same as one of its
  labels, in any language, once both are folded (lower case, accents
  removed, `œ` as `oe`, spaces collapsed). "1 Œuf  moyen" and "1 oeuf
  moyen" are the same; "1 œuf moyen" at 45 g and at 50 g are two portions,
  and so are the same words in `g` and in `ml`. The serving of a label is
  not compared.
- **Plausible.** From 1 to 2000 g or ml, both included
  (`implausible_portion` outside). The label has 120 characters at most.
- **Ten per food.** A food has at most 10 visible portions added by people
  (`too_many_portions`); those of its source do not count.
- **Order.** After the portions of the source, the added ones come oldest
  first. Dates are days: two portions added the same day are ordered by
  their identifier, which does not change, so a rebuild keeps the order.
- **No vote, and no edit.** A portion is not voted for and cannot be
  changed: a wrong one is reported with its food, and hidden by moderation.

### Search

- Every word typed is the beginning of a word of a name (in any language) or
  of the brand, and all of them must be found. Accents and case are ignored.
  A word of one letter is a whole word.
- **Fewer than three letters or digits in all**: only the generic foods and
  the products without barcode are searched (160 000 foods). From the third
  letter on, everything. This is what keeps a search within a fraction of a
  second: two letters match hundreds of thousands of barcoded products.
- **Digits** are also a barcode: the food carrying it comes first (the text
  is normalised like `GET /v1/barcodes/{code}`). The barcodes beginning with
  the digits typed (from four digits on) and the names holding them are
  found too, in the order below.
- **Reference foods** (`kind=generic`). Every generic food matching is read
  (they are 27 000, in a few blocks of the index) and put in an order made
  of rules on the words of the names, with no list of foods
  (`service/ranking.py`):
  1. a name in the language of the request (`lang`) that matches, before a
     food that only matches through another language;
  2. a name that is, or begins with, the words typed, before a name that
     only holds them;
  3. the table of the `country` of the request;
  4. how the name matches: the name is the text; its first part, before the
     first comma or parenthesis, is the text ("Pomme, chair et peau, crue"
     for "pomme"); the first part begins with the words typed ("Pomme de
     terre", "Riz blanc, cru"); it begins with the text inside a longer word
     ("Apfelsaft" for "apfel"); every word typed is a word of the name, in
     any order ("Chicken, broilers or fryers, breast, meat only, raw" for
     "chicken breast"); every word typed begins a word of the name. A plural
     or a feminine is the same word ("Apples", "Eier"); a compound is not;
  5. for a name that begins with the text, the fewest words in its first
     part ("Lait entier, UHT" before "Lait fermenté à boire, nature");
  6. plain before processed, from short lists of words in French, English,
     German, Italian and Spanish: raw, fresh and unflavoured first; then the
     names that say nothing; then cooked; last dried, powdered, canned,
     pre-packed and baby food. A form word does not count in the first part
     unless it was typed: "Oeuf cru" is the egg, like "Oeuf, cru";
  7. the shortest name, then the identifier.

  A language answers with the names the sources have: French and English
  for every table that has them, German and Italian only in the Swiss table
  (1 200 foods), Spanish nowhere. A text in a language without a name finds
  the foods whose name in another language holds it ("arroz" finds the
  English names of USDA that hold it, "manzana" nothing), and German
  compounds are found from their beginning only ("Milch" finds
  "Milchschokolade", not "Vollmilch").
- **Products** (`kind=product`) are ordered by: the barcode typed; a barcode
  that is not a 13-digit in-store number before one that is; the foods of `country`
  (the version served lists it); a name equal to the text, then beginning
  with it, then the rest; the most scanned products; the shortest name; the
  identifier.
- **In-store barcodes** (flag `in_store_barcode`). Those of 13 digits, the
  labels of the counter (320 000 foods, hardly ever scanned twice), come
  after every other product: when 120 other products match, those fill the
  answer and none of them is in it; when fewer do, the best 20 of them are
  added at the very end. Those of 8 digits are the own-brand products of
  Lidl, Aldi, Penny or Marks & Spencer: they carry the flag and rank like
  any product. A lookup by barcode, and a search by the digits of one, are
  not concerned.
- **Without `kind`** both are searched. The reference foods whose name, or
  first part, is the text typed come first, in the order of the reference
  foods (40 at most: "pomme" has 37 of them in the five tables, "riz" 3);
  everything else follows in the order of the products, generic foods
  before products where that order does not tell them apart.
- **Depth.** A search ranks the best matches of the index (a few hundred
  foods at most, 200 for `kind=generic`) and pages through them:
  `next_offset` becomes `null` at the end of that list, not at the end of
  the database. `offset` may not exceed 1000.
- **Time.** A search has a budget (`FOOD_DATA_SEARCH_BUDGET_MS`, 500 by
  default) for the full-text index. When it is spent the answer holds what
  was found until then, reference foods and foods of the country first.
- An empty `q`, or one without a letter or a digit, returns no item.
  `q` may not exceed 100 characters.

### Errors

Every error is `{"code": ..., "message": ...}`; some carry one more field.

| Status | Code | When | Extra field |
| --- | --- | --- | --- |
| 400 | `invalid_json` | the body is not a JSON object | |
| 401 | `unauthorized` | a write or a moderation request without the service token | |
| 403 | `contributor_blocked` | a write carrying a contributor key moderation blocked | |
| 404 | `not_found` | no such food, version, report or endpoint; a hidden food | |
| 405 | `method_not_allowed` | the endpoint does not answer this method | |
| 409 | `barcode_exists` | a food already has the barcode (hidden or not) | `id` |
| 409 | `version_exists` | a version already has exactly these values | `version_id` |
| 409 | `name_exists` | the source already gives a name in this language | |
| 409 | `too_many_versions` | the food already has 20 versions proposed by users | |
| 409 | `portion_exists` | the food already has this portion (same label, same amount within 1) | `portion_id` |
| 409 | `too_many_portions` | the food already has 10 portions added by users | |
| 413 | `payload_too_large` | a body above 64 KiB | |
| 422 | `invalid_request` | a parameter or a field is missing, unknown, of the wrong type or too long | |
| 422 | `invalid_barcode` | not a GTIN with a valid check digit | |
| 422 | `implausible_values` | the values fail the quality checks of the build | `checks` |
| 422 | `implausible_portion` | a portion added weighs or holds less than 1 or more than 2000 | |
| 500 | `internal_error` | the service failed | |
| 503 | `database_unavailable` | no `food.sqlite` is installed yet | |
| 503 | `writes_disabled` | the service runs without a token | |

An unknown parameter or field is refused (`invalid_request`), not ignored: a
misspelt name would otherwise be lost silently.

### What a request may contain

- A body: JSON, at most 64 KiB, with a `Content-Length`.
- `contributor`: 8 to 200 visible ASCII characters. It is stored as an
  HMAC-SHA-256 with a secret of this installation, never as received.
- `names`: 1 to 30 languages, each name at most 200 characters. The first
  language of the object is the main one of the product. Language codes look
  like `fr` or `pt-br`.
- `brand`, `category`, a portion label: at most 120 characters. `note`: 1000.
- `countries`: at most 60 ISO 3166-1 alpha-2 codes, in any case.
- `nutrients`: numbers or `null`; `kcal` is required, and `alcohol_g` may be
  left out. Values are rounded to 4 decimals and must pass the quality
  checks listed in the README.
- `servings`: at most 10, each `{"label"?, "amount" > 0, "unit": "g" | "ml"}`.
- A portion added (`POST /v1/foods/{id}/portions`): `language` (a language
  code), `label` (1 to 120 characters), `amount` (a number from 1 to 2000)
  and `unit` (`g` or `ml`), all required.
- `barcode`: digits, spaces and dashes; normalised (a UPC on 12 digits
  becomes a GTIN-13) and its check digit verified.
- An identifier in an address: at most 80 visible ASCII characters; write
  reserved characters as the address requires (`gtin%3A3017620422003` and
  `gtin:3017620422003` are both accepted).

### Endpoints beyond the contract

- `GET /v1/health` returns `{"status": "ok", "foods": ..., "built_at": ...}`,
  or 503 `database_unavailable`. For a load balancer or a container check.
- `GET /v1/meta` returns:

  ```json
  {
    "service": {"name": "kratheos-food-data", "version": "0.1.0", "contract": 2,
                "user_version_votes": 3},
    "built_at": "2026-10-11T12:58:40+00:00",
    "served_since": "2026-10-11T13:19:34+00:00",
    "layout_version": 3,
    "licence": {"id": "ODbL-1.0", "name": "...", "url": "...", "notice": "...", "contents": "..."},
    "sources": [{"id": "off", "title": "Open Food Facts", "publisher": "...", "homepage": "...",
                 "version": "...", "released": "...", "licence": "ODbL-1.0", "licence_name": "...",
                 "licence_url": "...", "attribution": "...", "foods": 3367825}],
    "counts": {"foods": 3564569, "generic_foods": 26950, "products_with_barcode": 3404293,
               "products_without_barcode": 133326, "names": 4000064,
               "foods_with_several_versions": 352584, "by_source": {"off": 3367825},
               "added_since_the_build": {"products": 0, "versions": 0, "translations": 0,
                                         "votes": 0, "portions": 0}}
  }
  ```

  `licence.notice` and each `sources[].attribution` are the texts to
  reproduce (the same as in `ATTRIBUTION`). The counts other than `foods`
  and `by_source` come from the manifest and are `null` without it;
  `added_since_the_build` counts the live database. `layout_version` is 3,
  or 2 for a database built before the household portions, which is still
  served: its foods then only list the portions people added.
- `GET /v1/moderation/reports?state=open|closed|all&limit=50&offset=0`
  (`limit` at most 100) returns `{"items": [...], "next_offset": ...}`, each
  item `{"id", "food_id", "reason", "note", "state", "created_at",
  "closed_at", "hidden", "food"}`: `food` is the food record, served even
  when the food is hidden (`hidden` says so), `null` when it no longer
  exists. `lang` and `country` are accepted for that record. Who reported is
  never returned. One line per report, oldest first; `GET
  /v1/moderation/foods` (below) gives them food by food.
- `POST /v1/foods/{id}/reports` answers 202 with `{"id": <number>, "state":
  "open", "replaced": false, "reporters": 3, "reasons": {"inappropriate": 2,
  "wrong_values": 1}}`. A person has one open report per food: reporting the
  food again replaces the reason, the note and the date of the earlier one,
  which keeps its number (`replaced` is then true). `reporters` is how many
  different people have an open report on the food once this one is
  recorded, `reasons` how many of them for each reason. Reports hide nothing
  by themselves, whatever their number.
- `POST /v1/foods/{id}/versions` also answers `"version_id"`, the identifier
  of the version it created, beside `"versions"`.
- `POST /v1/moderation/foods/{id}/hide` and `/restore` answer `{"id": ...,
  "hidden": true | false}`. `{id}` is the identifier of a food, of one
  version (`kratheos:v:<uuid>`, `usda:branded:...`) or of one portion
  (`kratheos:p:<uuid>`, `cnf:125:p:1`): a hidden version is not
  listed, cannot be served or voted for, and is left out of the export with
  its votes, while the food stays. A hidden portion is no longer listed in
  `servings` nor exported, and no longer counts among the ten; a hidden food
  takes its portions with it. A food whose versions are all hidden is
  not found. A hidden food keeps its barcode: creating a product with it
  answers `barcode_exists`.
- `POST /v1/moderation/reports/{report_id}/close` answers `{"id": ...,
  "state": "closed"}`.
- `POST /v1/foods/{id}/translations` answers the food record (200). A name
  added in Kratheos can be replaced by a later one.
- `HEAD` is answered like `GET`, without the body.

### Moderation: the reported foods

- `GET /v1/moderation/foods?state=open|closed|all&limit=50&offset=0`
  (`limit` at most 100, `lang` and `country` accepted for the record) lists
  the foods people reported, the most reported first, then the most recently
  reported. It returns `{"items": [...], "next_offset": ...}`, each item:

  ```json
  {
    "food_id": "gtin:3017620422003",
    "reporters": 3,
    "reasons": {"inappropriate": 2, "other": 1},
    "first_reported_at": "2026-10-11T14:03:22Z",
    "last_reported_at": "2026-10-11T18:40:02Z",
    "reports": [{"id": 2, "reason": "inappropriate", "note": "...", "state": "open",
                 "created_at": "2026-10-11T14:03:22Z", "closed_at": null}],
    "hidden": false,
    "food": {"id": "gtin:3017620422003", "...": "the food record"}
  }
  ```

  `reporters` counts the different people among the reports of the state
  asked, `reasons` how many of them gave each reason (the most given first),
  `reports` those reports, oldest first. `food` is as in the list of the
  reports: served even when hidden, `null` when the food no longer exists.
  Who reported is never returned.
- `POST /v1/moderation/foods/{id}/reports/close` closes every open report
  about a food and answers `{"id": ..., "closed": <how many>}`.

### Moderation: one contributor key

The public tables carry no author. The service keeps, in a history that is
never exported, which keyed hash wrote each product, each version, each
name and each portion; this is what lets a moderator undo the work of one
key. The contract
says the key only serves to count a person once: it now also serves this,
and nothing else. A key is sent in the body of a request, never in its
address (addresses are logged); what comes back is its keyed hash, never the
key.

- `POST /v1/moderation/contributors/lookup` with `{"contributor": "<key>"}`
  tells what the key wrote:

  ```json
  {
    "contributor": "<keyed hash>", "blocked": false, "blocked_at": null,
    "counts": {"products": 1, "versions": 1, "translations": 1, "portions": 1, "votes": 3,
               "reports": 1},
    "products": [{"food_id": "gtin:...", "record_id": "kratheos:<uuid>", "name": "...",
                  "at": "2026-10-11T14:03:22Z", "hidden": false}],
    "versions": [{"version_id": "kratheos:v:<uuid>", "food_id": "...", "at": "...", "hidden": false}],
    "translations": [{"food_id": "...", "language": "de", "name": "...", "at": "...", "current": true}],
    "portions": [{"portion_id": "kratheos:p:<uuid>", "food_id": "...", "language": "fr",
                  "label": "1 œuf moyen", "amount": 50, "unit": "g", "at": "...", "hidden": false}],
    "votes": [{"food_id": "...", "version_id": "...", "at": "..."}],
    "reports": [{"id": 4, "food_id": "...", "reason": "other", "note": "", "state": "open", "at": "..."}]
  }
  ```

  Each list holds the 500 most recent rows, `counts` all of them. `hidden`
  says whether a product, a version or a portion is hidden, one by one or
  with its key;
  `current` whether a name is still the one of the food in that language
  (somebody else may have given another since). A key the service never saw
  has empty lists. What a layer written elsewhere brought has no author.
- `POST /v1/moderation/contributors/block` with `{"contributor": "<key>"}`
  hides everything the key wrote and refuses its later writes, in one call.
  Its products, its versions, its portions and the names it added that are
  still current become invisible like any hidden record (not found, not
  listed, not searched, left out of the export); its votes are no longer
  counted, so the version served may change back; its reports are left out
  of the moderation lists. Every later write carrying the key
  (`POST /v1/products`, `.../versions`, `.../votes`, `.../translations`,
  `.../portions`, `.../reports`) answers 403 `contributor_blocked`. It
  answers `{"blocked": true, "contributor": "<keyed hash>", "blocked_at":
  "...", "withdrawn": {"products": 2, "versions": 1, "translations": 1,
  "portions": 1}}`. Blocking a key that already is changes nothing and
  answers the same.
- `POST /v1/moderation/contributors/unblock` with `{"contributor": "<key>"}`
  undoes both and answers `{"blocked": false, "blocked_at": null,
  "contributor": "<keyed hash>", "restored": {"products": 2, "versions": 1,
  "translations": 1, "portions": 1}}`. What the key wrote is served again exactly as
  before, its votes count again and its reports are listed again. What
  moderation hid one by one (`.../hide`) is a separate fact and stays
  hidden; a name somebody else gave to a food while the key was blocked
  stays the name of the food.
- `GET /v1/moderation/foods/{id}/contributors` gives the keyed hashes of who
  wrote a record, so that the backend can check its own log against the
  history: `{"id": ..., "contributors": [{"contributor": "<keyed hash>",
  "actions": ["create_product"], "first_at": "...", "last_at": "..."}]}`,
  in the order of their first write. `{id}` is a food (who created it, who
  added a name: `create_product`, `translate`), a version (who proposed it,
  who voted for it: `add_version`, `vote`) or a portion (who added it:
  `add_portion`); the record of a product added in
  Kratheos (`kratheos:<uuid>`) also names whoever created it when the food
  is named after its barcode. 404 `not_found` for an unknown record; an
  empty list for a record of a source nobody wrote to. Compare with the
  `contributor` that `lookup` answers for a key.

None of this is ever exported: the weekly export holds no key, no keyed
hash, no history and no list of blocked keys, only what is left visible.
