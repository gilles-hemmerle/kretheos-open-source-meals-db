# The API of the food service

This document is the reference of the HTTP API served by `food-data serve`.

Its first part is the contract agreed with the Kratheos backend, the only
client of the service in production, copied as it was written (version 2).
Its second part says how this service implements it: what is added, where
the data cannot say what the contract asks and what is served instead, how
search behaves, the errors, the limits.

## Contract between the food service and the Kratheos backend (version 2)

Working note shared by the agents building each side. The food service lives in the
repository `kratheos-food-data`; the Kratheos backend is its only client in production.

### Roles

- The food service reads `food.sqlite` (built from the open sources and the Kratheos layer)
  and owns the live Kratheos layer (products added, versions of the values proposed by
  users, votes, translations, reports). It knows no person: only an opaque contributor key.
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
    "fat_g": 30.9, "saturated_fat_g": 10.6, "fibre_g": null, "salt_g": 0.107
  },
  "servings": [{"label": "15 g", "amount": 15, "unit": "g"}],
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
- Which version is served to a request:
  1. the candidates are every visible version of the food; when the request gives a
     `country` and at least one version lists it, only those versions;
  2. the candidate with the most votes wins, except that a `user` version needs at least
     3 votes (a setting of the service) to be served instead of a `source` version;
  3. a tie, or no vote at all: the order of the build (plausible values, complete macros,
     most recent, fixed source order).
- `served` marks, in a list of versions, the one this request gets.

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

1. **`flags` carries the names of the build**, not those the contract gives
   as examples (`implausible_energy`, `carbs_converted`). The quality flags
   are `value_out_of_range`, `energy_out_of_range`,
   `sugars_exceed_carbohydrate`, `saturated_exceed_fat`,
   `macros_sum_over_100`, `energy_macro_mismatch`; the informational ones
   `energy_from_kj`, `salt_from_sodium`, `alcohol_from_abv`, `basis_assumed`,
   `carbohydrate_fiber_subtracted`, `carbohydrate_includes_fiber`,
   `inexact_values`, `obsolete`, `barcode_invalid`, `barcode_restricted`.
   The README describes each. `versions_disagree` is added by the service.
2. **`versions_disagree`** is set when at least two visible versions have
   the four values (energy, protein, carbohydrate, fat) and either do not use
   the same basis (100 g and 100 ml) or differ beyond the tolerance the build
   already uses between sources: energy by more than 10 kcal and 10 %, or a
   macro by more than 2 g and 20 %. Versions that differ by the rounding of a
   label (539 and 541 kcal) do not raise it. It is a flag of the food record,
   never of a version.
3. **`is_drink` is `false` when the source does not say.** The database
   holds three states (yes, no, unknown: 1.8 of 3.6 million foods); the
   contract has a boolean. `drink=true` returns what a source classifies as
   a beverage, `drink=false` everything else.
4. **`servings` holds at most one portion.** Every source gives at most one
   portion per record and the layer stores one per version. A request may
   send several: the first is kept, the others are ignored, and the answer
   shows what was kept. A record whose source words a portion without giving
   its size has none. When the source gives no label, the label is the size
   (`"30 g"`).
5. **`updated_at` is a day**, written at midnight UTC: the sources and the
   layer store dates.
6. **Alcohol.** `nutrients` has the eight values of the contract and no
   alcohol, but the energy check needs it: without it every wine would be
   refused as implausible. `POST /v1/products` and
   `POST /v1/foods/{id}/versions` accept an optional `nutrients.alcohol_g`
   (grams per 100 g or 100 ml). A version proposed without it keeps the
   alcohol the food is known with. Alcohol is never part of an answer.
7. **`name_origin`.** `kratheos` marks a name added with
   `POST .../translations`. The names a product was created with in Kratheos
   are its own names: their origin is `source`, and they cannot be replaced
   either.
8. **`id` of a product without barcode coming from a source** is
   `<source>:<id in the source>` too (`off:...`, `chain:...`,
   `usda:branded:...`), not only that of a generic food. No identifier is
   longer than 80 characters.
9. **The build date** is not inside `food.sqlite` (two builds of the same
   inputs give the same file). `GET /v1/meta` reads it from the
   `manifest.json` kept next to the database, and falls back to the
   modification time of the file.
10. **`brand` and `category`** are as the source writes them: a brand may
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
- **The rule**, as `versions.choose_served` applies it:
  1. a `user` version with fewer votes than the threshold is set aside as
     long as the food has a visible `source` version. This comes first, so
     that such a version is never served instead of a source, even to the
     only country that lists it. A food no source knows has only `user`
     versions: they all count;
  2. when the request gives a `country` and at least one of the versions
     left lists it, only those are considered;
  3. the most voted wins;
  4. a tie, or no vote: the order of the build (plausible values, complete
     macros, most recent, fixed source order, then the identifier).
- **Countries decide before votes do.** A version that lists no country is
  never the one served to a country another version lists, however many
  votes it has: send `countries` with every version proposed (the country of
  the person who proposes it, at least). The other way round, a country only
  one version lists gets that version even when the votes, or the quality
  checks, prefer another.
- **Votes belong to the food, not to a country**: there is one count per
  version, and the rule compares the counts of the versions left for the
  request.
- **The threshold** is `FOOD_DATA_USER_VERSION_VOTES` (3 by default), the
  same for the service and for the build, which records it in
  `meta.user_version_votes` of `food.sqlite`.
- **`country` on the writes.** `POST /v1/foods/{id}/versions` and
  `POST /v1/foods/{id}/votes` accept `?country=`, like the read, so that
  `served` in their answer is the one of the person. `POST /v1/products` and
  `POST /v1/foods/{id}/translations` accept `?lang=` and `?country=`.
- **`version_exists`** compares `basis` and the eight nutrients, after the
  rounding of the build (4 decimals), with every visible version of the
  food. Portions and countries are not values: proposing the same values
  with another portion is refused.
- A food can have at most 20 versions proposed by users (409
  `too_many_versions`).
- **Voting.** The answer of a vote for the version a person already votes
  for is the same list, unchanged. Votes taken over from a `kratheos.sqlite`
  written elsewhere are counts without a key: they count, and nobody can
  move them.

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
- **Order**: the barcode typed; the foods of `country` (the version served
  lists it); a name equal to the text, then beginning with it, then the
  rest; generic foods before products; the most scanned products; the
  shortest name; the identifier.
- **Depth.** A search ranks the best matches of the index (about 500 foods at
  most) and pages through them: `next_offset` becomes `null` at the end of
  that list, not at the end of the database. `offset` may not exceed 1000.
- **Time.** A search has a budget (`FOOD_DATA_SEARCH_BUDGET_MS`, 500 by
  default) for the full-text index. When it is spent the answer holds what
  was found until then, generic foods and foods of the country first.
- An empty `q`, or one without a letter or a digit, returns no item.
  `q` may not exceed 100 characters.

### Errors

Every error is `{"code": ..., "message": ...}`; some carry one more field.

| Status | Code | When | Extra field |
| --- | --- | --- | --- |
| 400 | `invalid_json` | the body is not a JSON object | |
| 401 | `unauthorized` | a write or a moderation request without the service token | |
| 404 | `not_found` | no such food, version, report or endpoint; a hidden food | |
| 405 | `method_not_allowed` | the endpoint does not answer this method | |
| 409 | `barcode_exists` | a food already has the barcode (hidden or not) | `id` |
| 409 | `version_exists` | a version already has exactly these values | `version_id` |
| 409 | `name_exists` | the source already gives a name in this language | |
| 409 | `too_many_versions` | the food already has 20 versions proposed by users | |
| 413 | `payload_too_large` | a body above 64 KiB | |
| 422 | `invalid_request` | a parameter or a field is missing, unknown, of the wrong type or too long | |
| 422 | `invalid_barcode` | not a GTIN with a valid check digit | |
| 422 | `implausible_values` | the values fail the quality checks of the build | `checks` |
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
- `nutrients`: numbers or `null`; `kcal` is required. Values are rounded to
  4 decimals and must pass the quality checks listed in the README.
- `servings`: at most 10, each `{"label"?, "amount" > 0, "unit": "g" | "ml"}`.
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
    "layout_version": 2,
    "licence": {"id": "ODbL-1.0", "name": "...", "url": "...", "notice": "...", "contents": "..."},
    "sources": [{"id": "off", "title": "Open Food Facts", "publisher": "...", "homepage": "...",
                 "version": "...", "released": "...", "licence": "ODbL-1.0", "licence_name": "...",
                 "licence_url": "...", "attribution": "...", "foods": 3367825}],
    "counts": {"foods": 3564569, "generic_foods": 26950, "products_with_barcode": 3404293,
               "products_without_barcode": 133326, "names": 4000064,
               "foods_with_several_versions": 352584, "by_source": {"off": 3367825},
               "added_since_the_build": {"products": 0, "versions": 0, "translations": 0,
                                         "votes": 0}}
  }
  ```

  `licence.notice` and each `sources[].attribution` are the texts to
  reproduce (the same as in `ATTRIBUTION`). The counts other than `foods`
  and `by_source` come from the manifest and are `null` without it;
  `added_since_the_build` counts the live database.
- `GET /v1/moderation/reports?state=open|closed|all&limit=50&offset=0`
  (`limit` at most 100) returns `{"items": [...], "next_offset": ...}`, each
  item `{"id", "food_id", "reason", "note", "state", "created_at",
  "closed_at", "hidden", "food"}`: `food` is the food record, served even
  when the food is hidden (`hidden` says so), `null` when it no longer
  exists. `lang` and `country` are accepted for that record. Who reported is
  never returned.
- `POST /v1/foods/{id}/reports` answers 202 with `{"id": <number>, "state":
  "open"}`.
- `POST /v1/moderation/foods/{id}/hide` and `/restore` answer `{"id": ...,
  "hidden": true | false}`. `{id}` is the identifier of a food, or of one
  version (`kratheos:v:<uuid>`, `usda:branded:...`): a hidden version is not
  listed, cannot be served or voted for, and is left out of the export with
  its votes, while the food stays. A food whose versions are all hidden is
  not found. A hidden food keeps its barcode: creating a product with it
  answers `barcode_exists`.
- `POST /v1/moderation/reports/{report_id}/close` answers `{"id": ...,
  "state": "closed"}`.
- `POST /v1/foods/{id}/translations` answers the food record (200). A name
  added in Kratheos can be replaced by a later one.
- `HEAD` is answered like `GET`, without the body.
