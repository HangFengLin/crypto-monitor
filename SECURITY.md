# Security Policy

## Supported Status

This project is currently an early-stage personal trading and monitoring system. Treat all deployments as self-managed and verify configuration before enabling order placement.

## Reporting A Vulnerability

Do not post secrets, API keys, Discord webhooks, account screenshots, or private trading logs in public issues.

If you discover a security issue:

1. Rotate any exposed credential immediately.
2. Disable OKX API keys that can trade until the issue is understood.
3. Share a minimal reproduction privately with the project owner.

## Operational Guardrails

- Keep OKX API keys scoped to demo trading where possible.
- Do not enable withdrawal permissions for keys used by this project.
- Run the monitoring dashboard separately from order-placement bots unless you intentionally enable the `trading` Docker Compose profile.
- Store runtime state, event logs, and reports outside the image through mounted volumes.
