# Security

This bot places orders with exchange API keys. Please report anything that could leak a key, bypass the live
gate (`LIVE_ACCEPTANCE_APPROVED`, `MODE=live` + `LIVE_CONFIRM`), skip the risk engine or send an order that was
not intended **privately** through
[GitHub security advisories](https://github.com/thanatchon36/maeklong/security/advisories/new), not as a public
issue.

Keys are read only from the environment or a secret file outside git. Never paste keys, secrets or account
identifiers into issues, PRs or logs you share.
