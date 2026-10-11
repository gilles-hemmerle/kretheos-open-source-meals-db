"""The food service: `food.sqlite` over HTTP, with the live Kratheos layer on top.

One process and two SQLite files:

* `food.sqlite`, built from the open sources and the Kratheos layer, opened
  read-only and reopened when a new build replaces it (`catalogue.py`);
* `live.sqlite`, which the service owns and writes to: the products added,
  the versions of the values proposed by users, the votes, the translations,
  the reports and what moderation hid (`live.py`).

A read merges both, so what is written is visible at once, and applies the
rule choosing the version served to the country of the request (`foods.py`,
`search.py`). `api.py` holds the endpoints, `validation.py` what a request
may contain, `httpd.py` the HTTP server. `export.py` writes the public
`kratheos.sqlite` from the live database and `maintenance.py` rebuilds or
installs `food.sqlite` and swaps it in.

The reference of the API is `docs/api.md`.
"""
