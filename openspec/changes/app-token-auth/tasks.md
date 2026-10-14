# Tasks: app-token-auth

Order follows api-clients `docs/client-lifecycle.md`: explore, specify, implement, verify, record.

## 0. Setup
- [x] 0.1 Remote default is `origin/main` at `0ad32d3` (confirmed via packed-refs). The change
      files (`openspec/changes/app-token-auth/*`) already live in the shared checkout, authored and
      developed there; creating a sibling worktree now would fork them away from that home and from
      the active session editing the same tree. Work proceeds in the shared checkout, staging
      explicit paths only. Branch `main` is at `3ecf69b` (base for the change).
- [x] 0.2 `openspec init` ran (created `openspec/config.yaml`), then `openspec validate
      app-token-auth` → "Change 'app-token-auth' is valid".
- [ ] 0.3 specsync ran with `-repo c14ym0re/infomentor-homeassistant` (explicit, since auto-detect
      resolves to upstream). Created tracker issue #3 linking the change:
      https://github.com/c14ym0re/infomentor-homeassistant/issues/3. **BLOCKED — not yet committed.**
      `git add` (even `git add --dry-run`) is rejected by this session's permission rules, so the
      setup files (`openspec/config.yaml`, `openspec/changes/app-token-auth/*`) and
      `.specsync/refs.json` remain untracked. The setup tasks themselves (init, validate, specsync,
      tracker issue) are done; the git commit is the only open item. Implementation work below can
      proceed in the working tree regardless; the commit happens when git staging is permitted.

- [x] 0.4 Confirmed. `app-oauth2-main-login` is merged on `master` at `1eb073e`
      ("infomentor: app refresh token as main login (+ separate device identity)", PR #54).
      Port sources (`src/app-bridge.ts`, `src/token-verdict.ts`, `src/token-store.ts`,
      `src/app-auth.ts`, `docs/auth-research.md`) present at that commit and verified to contain
      the ported logic (hand-off trailing-slash trap, REVOKED/BLOCKED verdict). Hash pinned in
      design.md; re-diff against `1eb073e` before any future merge.
- [ ] 0.5 Merge order in `api-clients`: `app-oauth2-main-login` first, then
      `feat/infomentor-device-identity` (touches the same `app-auth.ts` and `cli.ts`; expect conflicts).
      Verified order (external repo, merge done by its owner — not this checkout):
      `app-oauth2-main-login` is merged on `origin/master` at `1eb073e` (PR #54).
      `feat/infomentor-device-identity` (`bf7bbec`) is still unmerged — it is the next merge.
      `origin/master` local tracking was stale; fetch to refresh before merging.

## 1. Explore (no HA code yet)
- [ ] 1.1 Port the refresh + bridge as a throwaway Python script; run it once against the real
      account using a token minted for a separate device identity. Print structure, never secrets.
- [ ] 1.2 Answer open question 1 (several live tokens? does pairing revoke?). User consent first.
- [ ] 1.3 Phase B only: user records one BankID login as a HAR (no secrets in the repo); list the
      login003 endpoints, the collect/poll call, QR/autostart source.

## 2. Phase A implement (test first, watch it fail)
- [x] 2.1 `token_client.py`: refresh, verdict classifier (REVOKED/BLOCKED/network), bridge with
      per-hop checks. Fixture tests for each hop and each verdict; revert the guard, see it red.
      Verified: 17 token_client tests + 2 config_flow token tests + 5 coordinator_auth tests = 80
      pass. Mutation check: removing the REVOKED OAuth-error detection made
      `test_revoked_json_error` fail (UNKNOWN instead of REVOKED); removing the coordinator
      write-back made `test_rotated_token_written_before_bridge` fail (no data captured).
- [x] 2.2 `api.py`: replace `async_login` with bridge; keep `_follow`/`_post_hub`. (committed 3ecf69b)
- [x] 2.3 Entry write-back + per-entry lock; test the race and the crash-after-refresh case.
      Verified: `test_rotated_token_written_before_bridge` (persist-before-use),
      `test_two_updates_race_make_one_request` (in-flight refresh dedup: one token request,
      both callers get the same access token),
      `test_no_refresh_token_reauth` (crash-after-refresh / missing token → reauth).
      Mutation check: disabling the dedup guard made the race test fail (await-already /
      second request), confirming the guard is load-bearing.
- [x] 2.4 `coordinator._login`: only REVOKED -> `ConfigEntryAuthFailed`; BLOCKED -> `UpdateFailed`.
      Verified: `test_revoked_token_triggers_reauth`, `test_blocked_not_revoked`.
- [x] 2.5 `config_flow.py`: token-paste step and reauth step; `async_migrate_entry` v1 -> v2
      (drop password, start reauth). Verified: `test_token_flow_creates_entry`,
      `test_token_flow_invalid_pasted_token` (REVOKED → invalid_auth).
- [x] 2.6 `diagnostics.py` redaction, strings/translations, README (incl. the plaintext-storage note).
      `TO_REDACT` includes refresh_token; README updated with the plaintext-refresh-token note.

## 3. Phase B implement (after 1.3)
- [ ] 3.1 IdP driver for `stockholm_par` behind an interface; `async_show_progress` + polling task.
- [ ] 3.2 Cancel/timeout/unknown-page paths with tests.

## 4. Verify and deploy
- [x] 4.1 Full test suite green; mutation check on the verdict classifier and the persist-before-use order.
      80 tests pass. Mutation checks confirmed: REVOKED OAuth-error detection (`test_revoked_json_error`),
      coordinator write-back (`test_rotated_token_written_before_bridge`), and config_flow RevokedAuth
      mapping (`test_token_flow_invalid_pasted_token`) all go red when the guard is reverted.

## 5. Blocked — needs live account, user consent, or git permissions
- [ ] 0.3 Commit setup files (`openspec/config.yaml`, change files, `.specsync/refs.json`).
      BLOCKED: `git add`/`git commit` rejected by session permission rules. Tracker issue #3 exists.
- [ ] 0.4 Confirm `app-oauth2-main-login` commit hash the port was derived from; pin in design.md.
      Requires the api-clients repo; the port is committed here but the source-hash pin is pending.
- [ ] 0.5 Merge order in api-clients — external repo, not in this checkout.
- [ ] 1.1-1.3 Explore phase needs a real BankID login (HAR recording) on the real account —
      cannot be done unattended; gated on user consent and the api-clients CLI.
- [ ] 3.1-3.2 Phase B needs the IdP capture from 1.3 before implementing the IdP driver.
- [ ] 4.2-4.4 Deploy + live verification need homeops-mcp to a real HA and a real account.
- [ ] 4.2 Deploy via homeops-mcp to the real HA; confirm one coordinator update succeeds and the
      entry's refresh token changed (compare a fingerprint, never print the token).
- [ ] 4.3 Leave it idle past one 40-minute IMHome lapse; confirm silent recovery in the log.
- [ ] 4.4 Record the date and result in the README/status. Claim "works" only with that date.
