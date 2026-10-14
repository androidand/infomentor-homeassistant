# Replace username/password with the app refresh token (BankID once)

## Why

The integration logs in by scraping the web form with username and password (`api.py`
`async_login`). Stockholm Stad guardian accounts enforce BankID: the form is visible and the
server rejects it, so these users cannot set the integration up at all.

The sibling client (`api-clients/infomentor`, `docs/auth-research.md`) already solved the
credential problem. BankID is needed once; it yields the Android app's rotating OAuth2 refresh
token, which survived >= ~6 days idle (upper bound unmeasured). Each run refreshes it, asks
`/NA1/Authentication/sso` for a hand-off URL, follows it to a fresh `IMHome`, and every existing hub
call then works. No `SMSESSION`, no BankID again, no password stored.

## Depends on

`api-clients` change `app-oauth2-main-login` (the bridge, token store, REVOKED/BLOCKED verdict,
spec corrections). As of 2026-10-13 that work is **uncommitted in the shared `api-clients` tree**: no
branch, not on master. `src/app-bridge.ts`, `src/token-verdict.ts`, `src/token-store.ts` and the
`/NA1/Authentication/sso` spec correction exist only there. This change reads them as the reference
for a Python port; if they are lost or change, the port loses its source of truth. Its owner must
commit and land it first (or in parallel); this change records the commit it was ported from.

## What Changes

- **Phase A — token credential.** Config entry stores the refresh token, not a password. A Python
  port of refresh + bridge replaces `async_login`. The first token is pasted into the form (minted
  by the client CLI on a machine with a browser). Rotated tokens are written back to the entry.
- **Phase B — BankID inside HA.** The config flow drives the IdP chain itself and shows the BankID
  QR / autostart link with `async_show_progress` and a polling task. Gated on an explore step: the
  Stockholm Stad SiteMinder/PhenixID BankID endpoints are not captured yet.
- **Error taxonomy.** Revoked token -> reauth. A blocked request (F5 `400 text/html` "Bad Request",
  or a `401` XHTML page) -> retry later, never reauth. Today every `InvalidAuth` becomes a reauth.
- **Honest verification.** Fixture-driven tests that were seen failing without their guard, then a
  dated live check in the real HA, deployed via homeops-mcp.

## Capabilities

### New Capabilities
- `ha-auth`: how the integration obtains, stores, rotates and recovers its credential.

## Impact

- `api.py` (login/transport), `config_flow.py`, `coordinator.py` (`_login`, retry/reauth split),
  `__init__.py` (token write-back), `diagnostics.py` (redaction), `strings.json` + translations,
  `manifest.json` version, `README.md`, `tests/`.
- Breaking: existing entries hold `username`/`password`. Provide a migration that triggers reauth
  (config entry `VERSION` 1 -> 2) and drops the stored password.
- No new dependencies (aiohttp ships with HA).
- Other repo: `api-clients/infomentor` gains a documented way to mint a token for a separate
  device identity (see design, "One holder").
