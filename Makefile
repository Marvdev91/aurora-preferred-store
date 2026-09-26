.PHONY: all build dryrun test clean

all: build dryrun

build:
	python3 -m src.pipeline generate
	python3 -m src.pipeline build

dryrun:
	python3 -m src.pipeline load --dry-run
	python3 -m src.pipeline segments --dry-run

test:
	python3 tests/test_preferred_store.py

clean:
	rm -rf data/*.json data/*.jsonl __pycache__ src/__pycache__
