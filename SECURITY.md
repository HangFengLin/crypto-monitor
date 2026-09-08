# Security Policy

## Supported Status

This project is a personal strategy research, monitoring, and paper signal tracking system. Exchange account access and order execution are retired.

## Reporting A Vulnerability

Do not post secrets, API keys, Discord webhooks, account screenshots, or private trading logs in public issues.

If you discover a security issue:

1. Rotate any exposed credential immediately.
2. Disable OKX API keys that can trade until the issue is understood.
3. Share a minimal reproduction privately with the project owner.

## Operational Guardrails

- The application does not need exchange account credentials. Public historical research data does not require trading keys.
- Retired robot entrypoints exit before starting their loops. The private OKX request adapter rejects all requests.
- Previously deployed containers are independent of local changes; retirement of a running remote service requires a separate runtime migration.
- Store runtime state, event logs, and reports outside the image through mounted volumes.
