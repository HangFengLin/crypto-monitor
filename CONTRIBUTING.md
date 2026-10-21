# Contributing

## Workflow

1. Branch from `main` using `feature/<topic>`, `fix/<topic>`, or `codex/<topic>`.
2. Keep changes narrow. Do not alter `ProjectSignalEngine` while fixing execution, persistence, or UI issues unless the strategy change is explicitly reviewed.
3. Never commit `.env`, API credentials, runtime state, event logs, or generated reports.
4. Run the checks below before opening a pull request.

```bash
python3 -m pip install --require-hashes -r locks/validation-py311-linux-x86_64.txt
ruff check --select E9,F63,F7,F82 .
python3 -m py_compile app.py config.py data_client.py indicators.py position_manager.py runtime_utils.py strategy.py
python3 -m unittest discover -s tests -p "test_*.py"
```

The example uses Linux x86_64 and Python 3.11. Use the matching Python and
platform lock described in [locks/README.md](locks/README.md) for macOS. CI also
builds the Docker image and starts its real application entrypoint in a
credential-free container with external networking disabled. Its image ID and
runtime receipt are retained as workflow artifacts; a local test pass does not
replace these image checks.

Changes to order execution must include tests for dry-run defaults, side/position-side handling, state persistence, and failure reporting. Never use production credentials in tests. A reviewer must verify that Demo order placement still requires explicit opt-in.
