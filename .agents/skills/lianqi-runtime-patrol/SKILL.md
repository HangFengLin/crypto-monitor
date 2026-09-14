---
name: lianqi-runtime-patrol
description: Verify the live runtime of the 炼气 crypto monitoring and OKX bot stack in `/Users/meta-lin/Documents/New project`. Use for requests about current VPS health, SSH or Docker reachability, HTTP and report routes, Discord delivery state, bot scans, errors or positions, deployed-code freshness, and concise read-only production patrols. Do not use for repository-only review or for deployment and restart requests.
---

# Lianqi Runtime Patrol

Treat current machine evidence as authoritative. Keep the patrol read-only unless the user separately authorizes a repair.

## Guardrails

- Do not place orders, restart or recreate containers, edit files or configuration, clear state, send Discord test messages, or deploy code during a patrol.
- Never print `.env`, credentials, webhook URLs, request authorization headers, or other secrets. Report only whether configuration exists and whether recent delivery succeeded.
- Do not infer live health from source code, historical notes, container uptime alone, or a previously healthy result.
- If SSH or HTTP evidence cannot be collected, report the affected claim as `无法验证`; do not call the service healthy or down.
- Treat `/api/okx-bot/state` as non-canonical. Its `404` alone is not a bot failure.
- Treat positions, errors, scans, and Discord status as timestamped snapshots.

## Patrol workflow

1. Confirm that the target is this repository and record the user's boundaries and requested surfaces.
2. Resolve the SSH alias before using remembered host details:

   ```sh
   ssh -G crypto-vps | sed -n '/^user /p;/^hostname /p;/^port /p'
   ```

3. Verify reachability without mutation:

   ```sh
   ssh -o BatchMode=yes -o ConnectTimeout=12 crypto-vps hostname
   ssh crypto-vps 'sudo -n docker ps'
   ```

   If SSH fails, probe the public HTTP surface and ports narrowly. Distinguish workstation restrictions from a VPS or network incident.

4. Inspect the expected containers, their health, configured command, restart count, and recent timestamps. Prefer `sudo -n docker ps`, targeted `docker inspect`, and `docker top`; do not assume the image contains `ps` or `curl`.
5. Query canonical surfaces from the VPS host when possible:

   ```sh
   ssh crypto-vps 'for path in /api/health /api/state /api/reports /reports.html /api/okx-bot/health "/api/okx-bot/errors?limit=20"; do curl -sS --max-time 12 -o /dev/null -w "$path %{http_code}\n" "http://127.0.0.1$path"; done'
   ```

   Fetch the JSON bodies needed for the requested claim after recording the status codes. Inspect `/api/site-monitor` only when site-probe status is in scope.
6. For the OKX bot, pair `/api/okx-bot/health` with recent structured events and a bounded log tail. Verify scan freshness, current error, disabled symbols, position snapshot, and real `error` or `order_error` events. Strategy-filter outcomes such as `entry_filter_failed` are not order failures.
7. For Discord, use `/api/health` delivery fields or run `python3 discord_diagnose.py --no-send`. Never send a live diagnostic message without explicit authorization.
8. When code freshness is in scope, compare the local no-send fingerprint with the live `/api/health` fingerprint. A mismatch proves drift; a match does not replace runtime checks.
9. Stop when the requested claims are supported. Do not broaden a healthy patrol into deployment or cleanup.

## Reporting contract

Lead with one of `正常`, `部分异常`, `异常`, or `无法验证`, followed by the observation timestamp. Then give only:

- container and process evidence;
- canonical endpoint evidence;
- bot scan, position, and error evidence when requested;
- Discord delivery evidence with secrets redacted;
- impact-ordered anomalies and the smallest repair recommendation.

Label historical evidence as historical. If no live evidence was obtained, say so explicitly.
