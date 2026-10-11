# Entry points of kratheos-food-data. `make build` rebuilds the whole database
# from nothing; every other target is a part of it or a check.
#
#   make build              obtain the sources and rebuild dist/
#   make build-from-mirror  rebuild from the stored copies only, no publisher contacted
#   make download           obtain or verify the source files, without building
#   make release-files      lay out in release/ what a release must carry
#   make sources            list the sources, their licence and their state
#   make fastfood           measure prepared dishes and restaurant chains in dist/
#   make test               run the tests (no network: the publishers are simulated)
#   make lint / make format check or fix the code style
#   make clean              remove work/ and dist/ (the stored originals stay)
#
# The service (see the README and docs/api.md):
#
#   make serve              run the service on dist/food.sqlite (TOKEN=... to allow writes)
#   make export             write kratheos.sqlite from the live database
#   make rebuild            export, rebuild, compare with dist/food.sqlite, swap it in
#   make database-files     lay out in release-database/ what others install from
#   make requirements       write requirements.txt from uv.lock (Debian package, Docker)
#   make install            install into DESTDIR, for the Debian package (debian/rules)
#
# MIN_FREE_GB: free disk space a build must leave (default 5).
# MIRROR_URL:  base address of a release holding the files of mirror/.
# TOKEN:       shared secret of the writes of `make serve`; reads need none.
# PORT:        port of `make serve` (default 8750).

MIN_FREE_GB ?= 5
MIRROR_URL ?=
MIRROR_OPTION = $(if $(MIRROR_URL),--mirror-url $(MIRROR_URL),)
TOKEN ?=
PORT ?= 8750

# Layout of the Debian package (see debian/): the code with its own Python
# environment, the settings, the data directory.
PACKAGE = kratheos-food-data
SHARE = /usr/share/$(PACKAGE)
ETC = /etc/$(PACKAGE)
VAR = /var/lib/$(PACKAGE)

.PHONY: build build-from-mirror download release-files sources fastfood test lint format clean \
	serve export rebuild database-files requirements install

# `make` alone builds the database, as it always did. The Debian package never
# calls it: debian/rules only runs `make install`.
.DEFAULT_GOAL := build

build:
	uv run food-data build --min-free-gb $(MIN_FREE_GB) $(MIRROR_OPTION)

build-from-mirror:
	uv run food-data build --from-mirror --min-free-gb $(MIN_FREE_GB) $(MIRROR_OPTION)

download:
	uv run food-data download --min-free-gb $(MIN_FREE_GB) $(MIRROR_OPTION)

release-files:
	uv run food-data release-files

sources:
	uv run food-data sources

fastfood:
	uv run food-data fastfood

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

format:
	uv run ruff check --fix src tests
	uv run ruff format src tests

clean:
	rm -rf work dist

serve:
	FOOD_DATA_TOKEN="$(TOKEN)" uv run food-data serve --database dist/food.sqlite --port $(PORT)

export:
	uv run food-data export-layer

rebuild:
	uv run food-data rebuild --database dist/food.sqlite --min-free-gb $(MIN_FREE_GB) $(MIRROR_OPTION)

database-files:
	uv run food-data database-files --database dist/food.sqlite

requirements:
	uv export --frozen --no-dev --no-emit-project --format requirements-txt \
		--output-file requirements.txt

# Called by debhelper as `make install DESTDIR=<package root>`. The Python
# environment is created at its final path, so that its scripts point at the
# right interpreter, then moved into the package. Only what requirements.txt
# pins, with its checksums, is installed, then the code itself.
install:
	mkdir -p $(SHARE)
	python3 -m venv $(SHARE)/venv
	$(SHARE)/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.txt
	$(SHARE)/venv/bin/pip install --no-cache-dir --no-deps .
	rm -rf build
	mkdir -p $(DESTDIR)$(SHARE) $(DESTDIR)$(ETC) $(DESTDIR)$(VAR) $(DESTDIR)/usr/bin
	mv $(SHARE)/venv $(DESTDIR)$(SHARE)/
	cp LICENSE LICENSE-DATA ATTRIBUTION $(DESTDIR)$(SHARE)/
	cp debian/service.conf $(DESTDIR)$(ETC)/service.conf
	install -m 755 debian/food-data $(DESTDIR)/usr/bin/food-data
