---
name: lianqi-safe-release
description: Validate and safely release changes for the 炼气 repository at `/Users/meta-lin/Documents/New project`, including the VPS web service, OKX bot image or restart, and `sites/lianqi` Sites frontend. Use only when the user explicitly asks to deploy, publish, release, sync, build a remote image, or restart an affected service. Do not use for read-only patrols, code review, or local implementation alone.
---

# Lianqi Safe Release

Release the smallest authorized surface and prove the resulting live state. Reuse existing project scripts and platform workflows.

## Authorization gates

- A request to inspect, diagnose, review, test, or fix local code does not authorize deployment.
- Default VPS scope is `crypto-project` only. Do not restart `okx-strategy-bot` unless the user explicitly authorizes that restart in the current task.
- A general bot change or `trading` profile request does not authorize order mode. Set `ENABLE_OKX_ORDER_MODE=yes` only when the user explicitly requests continued OKX Demo order placement.
- Never place a manual order as part of release verification.
- Before a bot restart, capture live positions, bot health, scan freshness, and recent error events. If live position state cannot be verified, prefer build-only and report the blocker.
- Never expose or sync `.env`, credentials, webhooks, runtime state, reports, caches, or backups.
- Preserve unrelated user changes. Because `deploy_vps.sh` syncs the working tree with `rsync --delete`, do not run it while unreviewed or unrelated working-tree changes could enter the release.

## Release workflow

1. Classify the requested scope as one or more of:
   - `sites-frontend`
   - `vps-web`
   - `bot-build-only`
   - `bot-restart`
   - `order-mode`
2. From the repository root, inspect `git status --short`, the relevant diff, and the exact files that the release will include. Stop if the release set cannot be separated safely from unrelated changes.
3. Run tests proportional to the changed surface. For Python or runtime changes, run targeted tests first and the full suite before release:

   ```sh
   PYTHONPYCACHEPREFIX=/tmp/lianqi-pycache python3 -m unittest discover -s tests
   ```

   Run configured lint and type checks when their tooling is available. Strategy or sizing changes also require the repository's backtest, data-quality, and risk checks; passing unit tests alone is insufficient.
4. For `sites-frontend`, work in `sites/lianqi` and require these gates:

   ```sh
   pnpm run build
   node --test tests/rendered-html.test.mjs
   pnpm exec eslint app worker tests
   ```

   Use the bundled workspace Node and pnpm runtime if the system path lacks them. Publish through the Sites hosting workflow using the saved opaque project and version identifiers. Keep `LIANQI_ORIGIN_URL` server-side and never bake the VPS origin or authentication headers into client code.
5. Before a VPS release, resolve `ssh -G crypto-vps`, confirm the current containers, and capture `/api/health`. For bot-related releases, also capture `/api/okx-bot/health`, `/api/okx-bot/errors?limit=20`, recent structured events, and positions.
6. Run exactly one authorized VPS path from the repository root:

   ```sh
   # Web only: default and preferred VPS release.
   SERVICE='crypto-project' ./deploy_vps.sh

   # Build the bot image without restarting it.
   BUILD_ONLY=yes SERVICE='okx-strategy-bot' ./deploy_vps.sh

   # Restart the bot only after explicit authorization and live-position review.
   CONFIRM_RESTART_OKX_BOT=yes SERVICE='crypto-project okx-strategy-bot' ./deploy_vps.sh

   # Preserve explicitly requested OKX Demo order mode only after separate authorization.
   ENABLE_OKX_ORDER_MODE=yes CONFIRM_RESTART_OKX_BOT=yes SERVICE='crypto-project okx-strategy-bot' ./deploy_vps.sh
   ```

7. Verify the changed live surface after release. At minimum, confirm container health and `/api/health`; add `/api/state`, `/api/reports`, `/reports.html`, `/api/okx-bot/health`, recent bot events, and position reconciliation when relevant. Compare the live code fingerprint with the released source when available.
8. If verification fails, perform read-only diagnosis first. Do not broaden the restart set, enable order mode, delete state, or roll back automatically without authorization.

## Completion report

State the released scope, tests and gates passed, exact services restarted or not restarted, order-mode disposition, live verification evidence, and any remaining uncertainty. A successful upload or build without live verification is incomplete, not a successful production release.
