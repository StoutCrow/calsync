# Security

calsync handles API keys, calendar data and (optionally) email access, and its code was written entirely by an AI and has not been independently audited. Please take reports seriously and read [the privacy and security notes](README.md#privacy-and-security-plainly).

## Reporting a problem
Use GitHub's private reporting: **Security → Report a vulnerability** on this repository. Please don't post keys, calendar contents or your `calsync.env` in a public issue.

## Design choices worth knowing
- The server listens on `127.0.0.1` by default and every API call needs a secret access code; `--phone` and the home-server mode deliberately widen this (the latter only through Tailscale).
- Keys are stored in plain text in `config/calsync.env` (never committed; see `.gitignore`).
- Secrets are never sent back to the web page; the page only learns whether one is set.
- Updates install only when the user presses a button, only from this project's release page, and the `.exe` must match `SHA256SUMS`.
- Google sign-in uses the standard OAuth "desktop" flow with a random `state` check and stores only a refresh token.
