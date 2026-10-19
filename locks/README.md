# Rebuilding the Python environment

These files pin the complete Python dependency graph and package hashes. They
supplement the direct requirements files; a new direct requirement needs a new
lock and verification. Frontend dependencies remain bound by
`sites/lianqi/pnpm-lock.yaml` and `pnpm install --frozen-lockfile`.

| Lock | Supported target | Purpose |
|---|---|---|
| `validation-py312-macos-arm64.txt` | CPython 3.12, macOS arm64 | Local tests and offline research |
| `validation-py311-linux-x86_64.txt` | CPython 3.11, Linux x86_64 glibc | CI tests and offline research |
| `runtime-py311-linux-x86_64.txt` | CPython 3.11, Linux x86_64 glibc | Docker application runtime |

Create an empty virtual environment with the matching Python version, then use
the matching lock. For example, on Linux:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r locks/validation-py311-linux-x86_64.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

On macOS arm64, use Python 3.12 and the macOS lock. Installation needs network
access or a prepopulated package cache. Tests must use isolated data directories
and no credentials; a dependency lock does not establish data provenance or
strategy profitability. No other OS, CPU, or Python version is certified by these
locks. CI and Docker use the Linux locks directly.

The locks were generated with `uv pip compile --generate-hashes`, the matching
`--python-version` and `--python-platform`, and the public PyPI index. The runtime
lock is constrained to the Linux validation lock so shared packages have identical
versions. Base Docker images are still upstream tags; the CI receipt records the
resulting immutable image ID and runtime checks rather than claiming byte-identical
images across future rebuilds.

To reproduce the frozen trend study in another checkout, stage the four exposed
market inputs at the relative paths recorded in
`research_projects/trend_portfolio_v1/source_audit.json`, verifying their original
hashes, then run:

```sh
.venv/bin/python research_projects/trend_portfolio_v1/reproduce_frozen_study.py --verify-only
.venv/bin/python research_projects/trend_portfolio_v1/reproduce_frozen_study.py --output /absolute/new/output
```

The reproduction entrypoint verifies the original pre-PnL test receipt and all
ten frozen source hashes before mapping checkout directory prefixes. It preserves
the numerical implementation and records logical and physical provenance. Missing
or altered market inputs still fail the original loader. This is a reproduction
of exposed evidence, not another candidate search or a fresh holdout test.
