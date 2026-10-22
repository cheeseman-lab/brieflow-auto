# brieflow-auto plugin — dev/test runner.
#
# Canonical environment: conda + uv (the lab standard). One-time setup:
#     make test-env
# Then:
#     make test       # full unit suite + (in this env) the marimo live test
#     make lint       # ruff
#
# The live-kernel marimo test (tests/test_marimo_live.py) only runs when marimo
# is importable; it is pytest.importorskip-guarded, so envs without marimo just
# skip it and the rest of the suite still passes.

ENV ?= brieflow_auto_dev
PY := conda run -n $(ENV)
# A couple of helpers read these GCS bucket vars at import time in some tests.
TEST_ENV := BRIEFLOW_BUCKET_OUTPUT=gs://o BRIEFLOW_BUCKET_INPUT=gs://i

.PHONY: test-env test lint marimo-test

test-env:
	conda create -n $(ENV) -c conda-forge python=3.11 uv pip -y
	$(PY) uv pip install -r requirements-dev.txt

test:
	$(TEST_ENV) $(PY) python -m pytest tests/ -q

# Just the live-kernel marimo integration test (verbose).
marimo-test:
	$(TEST_ENV) $(PY) python -m pytest tests/test_marimo_live.py -v

lint:
	$(PY) ruff check scripts/ tests/
