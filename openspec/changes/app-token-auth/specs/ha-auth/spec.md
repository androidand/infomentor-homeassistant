# Capability delta: ha-auth

## ADDED Requirements

### Requirement: The refresh token is the stored credential

The integration SHALL store the app OAuth2 refresh token in the config entry and SHALL NOT store a
password. It SHALL derive the hub session from that token on every update that finds no valid hub
session.

#### Scenario: Hub session lapses between updates
- GIVEN a config entry holding a live refresh token
- AND the hub cookie jar has expired
- WHEN the coordinator updates
- THEN it refreshes the token, bridges a new hub session, and fetches data with no user interaction

#### Scenario: No password is persisted
- GIVEN a completed setup
- THEN the config entry data contains no `password` key
- AND diagnostics output contains neither the refresh nor the access token

### Requirement: Rotation is persisted before use

The integration SHALL write the rotated refresh token to the config entry immediately after a
successful refresh and before any further request, and SHALL serialise refreshes per entry.

#### Scenario: Two updates race
- GIVEN two coroutines request a refresh at once
- THEN exactly one token request is made and both receive the same new access token

#### Scenario: Crash after refresh
- GIVEN a refresh succeeded
- WHEN the process stops before the bridge runs
- THEN the entry already holds the new refresh token

### Requirement: A blocked request is not a revoked credential

The integration SHALL classify a failed token or bridge request as REVOKED only when the token
endpoint answers with an OAuth error in a JSON body. Any other failure (HTML body, front-end
"Bad Request", challenge page, timeout) SHALL be BLOCKED or a network error and SHALL NOT start a
reauth.

#### Scenario: WAF refusal
- GIVEN the token endpoint answers 400 with `text/html` and body `Bad Request`
- THEN the update fails with a retryable error
- AND no reauth flow is created

#### Scenario: Revoked token
- GIVEN the token endpoint answers a JSON `invalid_grant`
- THEN a reauth flow is created and the entry is kept

### Requirement: The bridge never returns an unusable session

The integration SHALL check status and shape at each bridge hop, SHALL fail naming the hop, and SHALL
treat a jar without `IMHome` as a failure.

#### Scenario: Lost trailing slash
- GIVEN the hand-off answers 200 with no `Location`
- THEN the bridge raises an error naming the hand-off hop

#### Scenario: Empty 200 from the hub
- GIVEN a hub call answers 200 with an empty body
- THEN it is treated as an unauthenticated session, not as an empty result

### Requirement: Setup works for BankID-only accounts

A user whose account enforces BankID SHALL be able to complete setup and reauth without ever typing a
password.

#### Scenario: Token paste
- GIVEN a valid refresh token pasted into the setup form
- THEN the flow validates it with one refresh and one bridge, creates the entry, and lists the children

#### Scenario: Invalid pasted token
- GIVEN a pasted token the server rejects as REVOKED
- THEN the form shows `invalid_auth` and creates nothing

### Requirement: BankID inside the flow (Phase B)

Where the school's IdP is supported, the config flow SHALL start BankID, show an autostart link and QR
while polling, and SHALL abort cleanly on user cancel, timeout, or an unrecognised IdP page.

#### Scenario: Unrecognised page
- GIVEN the IdP returns a page the flow does not recognise
- THEN the flow aborts with a message pointing to token paste, and makes no further requests
