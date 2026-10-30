## What changed

<!-- Describe the smallest user-visible or operational outcome. -->

## Risk and safety

- [ ] No secrets, runtime state, logs, or reports are included.
- [ ] Order placement remains explicit opt-in.
- [ ] Existing strategy behavior is unchanged, or the strategy change is explained and tested.

## Verification

- [ ] `ruff check .`
- [ ] `mypy --config-file pyproject.toml backtest_statistics.py position_manager.py runtime_utils.py config.py`
- [ ] `coverage run -m unittest discover -s tests -p "test_*.py" && coverage report --fail-under=1`
- [ ] Relevant Docker/config validation completed.

## Agent collaboration (when applicable)

- Task and handoff records:
- Base commit SHA and PR head reviewed:
- Agent reviewer, review scope, and findings (or pending):
- Actual verification commands, results, and CI link:
- Checks not run and remaining risks:

<!-- An agent review is not a human approval. Do not include secrets or private trading data. -->
