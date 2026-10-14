# Design

## Flow (per coordinator update)

```
stored refresh token (config entry)
  -> POST im.infomentor.se/Authentication/OAuth2/Token (grant_type=refresh_token)  [rotates]
  -> persist NEW refresh token to the entry BEFORE using anything else
  -> access token (600 s)
  -> GET api-im.infomentor.se/NA1/Authentication/sso  (Bearer) -> JSON string URL
  -> GET hand-off (".../Swedish/Production/mentor/", trailing slash) -> 302
  -> GET hub .../authentication/authentication/login -> hidden oauth_token
  -> POST .../swedish/production/mentor/ (oauth_token, trailing slash) -> 302 LoginCallback
  -> GET LoginCallback -> 302, sets IMHome
  -> hub calls with the cookie jar alone
```

Source of truth for every hop, status code and trap: `api-clients/infomentor/openapi.yaml`
(`mentorSsoHandoff`, `webOAuthRequestToken`, `mentorOAuthTokenSubmit`, `webOAuthLoginCallback`) and
`src/app-bridge.ts`. Constants: client_id `notificationapp`, secret literal `NONE`, scope
`IM2-API-NOTIFICATION`. The refresh-grant body is `refresh_token`, `client_id`, `client_secret`,
`grant_type`, `scope`.

The Python port was ported from `api-clients/infomentor` commit `1eb073e`
("infomentor: app refresh token as main login (+ separate device identity)", merged as PR #54 on
`master`, 2026-10-13). That commit is the source of truth for `src/app-bridge.ts`,
`src/token-verdict.ts`, `src/token-store.ts`, `src/app-auth.ts`, and
`infomentor/docs/auth-research.md`. Re-diff against that hash before any future merge so the port
does not drift from the reference.

## Decisions

1. **Where the token lives.** The config entry (`entry.data`), updated with
   `hass.config_entries.async_update_entry`. It rotates, so an env var or options flow cannot hold
   it. `diagnostics.py` MUST redact `refresh_token` and `access_token`. HA stores entries unencrypted;
   say so in the README.
2. **Persist before use.** The server keeps a short grace window on the old token only. If HA crashes
   between refresh and write-back the entry holds a token that may already be spent. Write the new
   token synchronously after the 200, before the bridge runs.
3. **One holder.** Two holders of one rotating token race and one silently loses. HA is the only
   holder after setup. Therefore the pasted token MUST NOT also stay in the client CLI's
   `.app-tokens.json`. Preferred: mint a token for a separate device identity
   (`DeviceIdentifier`, `DeviceFriendlyName` = "Home Assistant") from the same BankID web session so
   the dev machine and HA hold different tokens. OPEN QUESTION: does the server allow several live
   tokens per account, and does pairing a second device revoke the first? Test with the user's
   consent before relying on it; fall back to "paste = hand over, then `app-revoke`-free delete of the
   local file".
4. **Serialise.** One `asyncio.Lock` per entry around refresh + write-back. The coordinator is the
   only caller; the config flow's validation uses a separate throwaway session and must not refresh a
   token that the entry already holds.
5. **Error taxonomy (the important correction).** Classify by content type and body shape, not by
   status alone, as `api-clients/infomentor/src/token-verdict.ts` does:
   - REVOKED: a JSON OAuth error from the token endpoint -> `ConfigEntryAuthFailed` (reauth).
   - BLOCKED: `text/html` body, 11-byte "Bad Request", or an XHTML challenge page, with F5
     `BIGipServer` cookies -> `UpdateFailed`, keep the entry, retry next interval. The client's own
     probe once logged eight such responses as "token dead".
   - Network error -> `UpdateFailed`.
   Today `coordinator._login` turns any `InvalidAuth` into reauth, which this change would make
   worse (a WAF blip would delete a working credential's usefulness by prompting BankID).
6. **Status plus shape at every bridge hop.** A 200 with an empty body is not data (the access token
   used as a hub Bearer gives `isauthenticated false` and an empty 200). Refuse a jar without
   `IMHome`. Keep the existing `_post_hub` empty-body check.
7. **Keep what already works.** `_follow` (manual redirects), `_post_hub`, pupil handling, plans, the
   empty-`GetAllTasks` rule stay. Only login and the credential change. Reuse the `_follow`
   manual-redirect loop for the bridge hops.

## Phase B — BankID inside HA

We are not a BankID relying party, so the BankID RP API (`auth`/`collect`/`cancel`, QR from
`qrStartToken`/`qrStartSecret`, collect every 2 s) is NOT available. We must drive Stockholm Stad's
pages:

```
sso.infomentor.se/login.ashx?idp=stockholm_par
  -> login001.stockholm.se/affwebservices/public/saml2sso (SAMLRequest)
  -> .../siteminderagent/forms/amedborgare.jsp   ("Hur vill du logga in?")
  -> login003.stockholm.se (PhenixID BankID layer)   <-- NOT CAPTURED
  -> SAMLResponse form -> sso.infomentor.se/login.ashx -> ... -> IMHome
  -> pair/GetAuthenticationData -> LoginOAuth2 (302 code) -> OAuth2/Token
```

HA side: `async_show_progress(progress_action="bankid", progress_task=task)` (HA >= 2024.8 requires the
task), the task polls the IdP, the form description carries the autostart link and a QR image. The
`idp` value differs per school (`docs/idp-federation.json`, 96 entries) and some are not SiteMinder
at all (`ims-grandid-api.infomentor.se/Login/initial?communeId=...` is GrandID). Phase B therefore
starts as Stockholm Stad only and says so; other schools stay on token paste.

Risk: this scripts a third party's login page. It must be user-initiated, low rate, never retry
automatically, and a changed page must fail with a clear "use token paste" message.

## Open questions
1. Multiple live tokens per account? Does a second pairing revoke the first? (decision 3)
2. Upper bound of refresh-token idle lifetime (>= ~6 days known).
3. Does HA's refresh cadence (20 min default) keep the token alive better than idle? Probably moot.
4. Phase B endpoints behind login003 (needs a HAR of one real BankID login; human types nothing
   secret into the capture, no personnummer in files).
