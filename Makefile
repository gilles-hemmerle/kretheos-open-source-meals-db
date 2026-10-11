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
# MIN_FREE_GB: free disk space a build must leave (default 5).
# MIRROR_URL:  base address of a release holding the files of mirror/.

MIN_FREE_GB ?= 5
MIRROR_URL ?=
MIRROR_OPTION = $(if $(MIRROR_URL),--mirror-url $(MIRROR_URL),)

.PHONY: build build-from-mirror download release-files sources fastfood test lint format clean

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
