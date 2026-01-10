# Makefile — DuckDB-only incremental demo (article version)
#
# Assumes:
# - you are in the project root (where profiles.yml / project.yml live)
# - seeds/ contains seed_events_v1.csv and seed_events_v2.csv
# - fft CLI is installed and on PATH
#
# Usage examples:
#   make init
#   make run-v1
#   make noop
#   make run-v2
#   make dag
#   make test

SHELL := /usr/bin/env bash

# ---- Config (tweak as needed) ---------------------------------------------

ENV            ?= dev_duckdb
DB_PATH        ?= .local/incremental_demo.duckdb
SEEDS_DIR      ?= .local/seeds
ACTIVE_SEED    ?= $(SEEDS_DIR)/seed_events.csv

# Exported env for fft / profiles.yml templating
export FFT_ACTIVE_ENV := $(ENV)
export FF_ENGINE      := duckdb
export FF_DUCKDB_PATH := $(DB_PATH)

# ---- Convenience ----------------------------------------------------------

.PHONY: help
help:
	@echo ""
	@echo "Targets:"
	@echo "  make init     - create local folders"
	@echo "  make clean    - remove local DB + generated files"
	@echo "  make seed-v1  - copy v1 seed into place"
	@echo "  make seed-v2  - copy v2 seed into place"
	@echo "  make seed     - fft seed using FFT_SEEDS_DIR"
	@echo "  make run      - fft run (cached, rw)"
	@echo "  make run-v1   - seed v1 + seed + run"
	@echo "  make noop     - run again unchanged (should mostly skip)"
	@echo "  make run-v2   - seed v2 + seed + run (incremental update+insert)"
	@echo "  make dag      - generate HTML DAG"
	@echo "  make test     - run tests (adjust selector as you like)"
	@echo ""
	@echo "Config (override like: make run DB_PATH=.local/foo.duckdb):"
	@echo "  ENV=$(ENV)"
	@echo "  DB_PATH=$(DB_PATH)"
	@echo "  SEEDS_DIR=$(SEEDS_DIR)"
	@echo ""

.PHONY: init
init:
	@mkdir -p .local
	@mkdir -p $(SEEDS_DIR)
	@echo "Initialized .local/ and $(SEEDS_DIR)/"

.PHONY: clean
clean:
	@rm -f $(DB_PATH)
	@rm -rf site
	@rm -rf .cache .ff_cache 2>/dev/null || true
	@rm -f $(ACTIVE_SEED)
	@echo "Cleaned local artifacts"

# ---- Seed switching -------------------------------------------------------

.PHONY: seed-v1
seed-v1: init
	@cp -f seeds/seed_events_v1.csv $(ACTIVE_SEED)
	@echo "Activated seed v1 -> $(ACTIVE_SEED)"

.PHONY: seed-v2
seed-v2: init
	@cp -f seeds/seed_events_v2.csv $(ACTIVE_SEED)
	@echo "Activated seed v2 -> $(ACTIVE_SEED)"

# ---- fft commands ---------------------------------------------------------

.PHONY: seed
seed: init
	@echo "Seeding from FFT_SEEDS_DIR=$(SEEDS_DIR) into DuckDB=$(DB_PATH)"
	@FFT_SEEDS_DIR=$(SEEDS_DIR) fft seed . --env $(ENV)

.PHONY: run
run: init
	@echo "Running DAG (env=$(ENV), cache=rw, db=$(DB_PATH))"
	@fft run . --env $(ENV) --cache=rw

.PHONY: run-v1
run-v1: seed-v1 seed run

.PHONY: noop
noop:
	@echo "No-op run (should mostly skip if nothing changed)"
	@fft run . --env $(ENV) --cache=rw

.PHONY: run-v2
run-v2: seed-v2 seed run

.PHONY: dag
dag: init
	@echo "Generating HTML DAG..."
	@fft dag . --env $(ENV) --html
	@echo "Open: site/dag/index.html"

.PHONY: test
test:
	@echo "Running tests (adjust selector to your project)..."
	@fft test . --env $(ENV)
