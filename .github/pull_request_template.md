## What changed

<!-- Describe the smallest user-visible or operational outcome. -->

## Risk and safety

- [ ] No secrets, runtime state, logs, or reports are included.
- [ ] Order placement remains explicit opt-in.
- [ ] Existing strategy behavior is unchanged, or the strategy change is explained and tested.

## Verification

- [ ] `ruff check --select E9,F63,F7,F82 .`
- [ ] `python3 -m unittest discover -s tests -p "test_*.py"`
- [ ] Relevant Docker/config validation completed.
