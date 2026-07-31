# Android robustness audit

Date: 2026-07-12

Scope: `android/` admin, free, and paid variants plus their monitor API contract

Baseline reviewed: commit `7dde9b1`

## Executive summary

The Android project has a sensible first-release structure: one shared Retrofit/API layer, an isolated admin flavor, shared client code for free/paid, DataStore persistence, Firebase authentication, and a foreground polling service for LAN-only notification delivery. The backend endpoints and product-flavor boundaries are clear.

The first audit found several release-blocking lifecycle and data-isolation defects. Most importantly, server notification row `1` could reuse notification ID `1` and replace the foreground-service notification; notification cursors were shared between Firebase accounts; signing out did not disable or stop monitoring; and the Calendar tab ignored the saved `calendar_visible` setting. These issues are corrected in this change.

The app remains appropriate only for a trusted LAN. The monitor accepts a caller-supplied Firebase UID without verifying a Firebase ID token, admin actions have no server-side authentication, and client manifests deliberately permit cleartext HTTP. Those are backend/protocol decisions and are documented below as follow-up work rather than hidden by client-only changes.

## Architecture reviewed

- `src/main`: shared Retrofit client, API DTOs, and Compose theme.
- `src/admin`: operator dashboard, five-second status polling, and manual actions.
- `src/clientCommon`: Firebase auth, profile/filter UI, alerts, WebView calendar, settings, and foreground polling.
- `src/free`: client app plus AdMob.
- `src/paid`: client app with no ad SDK behavior.
- API contract: `/api/status`, `/api/actions`, `/api/manual-action`, `/api/client-notifications`, `/api/client-profile`, `/api/filter-suggestions`, and `/client-calendar`.

## Findings and disposition

| Priority | Finding | Risk | Disposition |
|---|---|---|---|
| Critical | Alert IDs directly reused server row IDs, including ID `1`, which is also the foreground notification ID. | An alert could replace the required foreground notification and destabilize or terminate monitoring. | Fixed: alert IDs are mapped to a reserved range above `10,000`. |
| High | One notification cursor was shared by every signed-in Firebase account. | A second account could skip all rows below the first account's cursor. | Fixed: DataStore cursors are keyed by Firebase UID. |
| High | Sign-out cleared UI state but left the persisted monitoring flag and service active. | Background work could survive logout, waste battery, and resume unexpectedly. | Fixed: sign-out stops the service and persists monitoring off. |
| High | The service did not re-check the persisted monitoring flag after a sticky restart. | Android could restart a service the user had already disabled. | Fixed: every polling loop checks the flag and self-stops when disabled. |
| High | The Calendar tab was always shown, contrary to `calendar_visible`. | Profile privacy/preferences were not reflected in the app. | Fixed: tab composition follows the loaded profile and safely changes selection. |
| Medium | Every API access built a new Retrofit and OkHttp client. | Repeated connection pools and threads increase resource use, especially every 30 seconds. | Fixed: one hardened OkHttp client and per-base-URL Retrofit cache are reused. |
| Medium | The in-app “Recent alerts” request returned the oldest 200 rows. | Clients with more than 200 alerts could not see current history. | Fixed: the endpoint has an explicit latest-first mode; incremental service polling remains oldest-first and cursor-safe. |
| Medium | Server input was not validated and Retrofit can throw for malformed base URLs. | A user typo could crash setup/connect flows. | Fixed: addresses are validated, normalized, and errors are shown in both apps. Missing schemes default to `http://` for LAN usability. |
| Medium | BASIC HTTP logging ran in all builds and URLs contain `uid` query values. | Release logs could expose stable client identifiers. | Fixed: logging is debug-only and `uid`/legacy `code` query values are redacted. |
| Medium | Calendar WebView reloaded during recomposition and had no explicit teardown. | Lost scroll state, unnecessary traffic, and leaked WebView resources. | Fixed: reload only when the URL changes, disable file/content access, and destroy on release. |
| Medium | Google sign-in errors and missing ID tokens were silently ignored. | Users received no actionable feedback. | Fixed: authentication errors now reach the existing error surface. |
| Medium | Notification permission denial and foreground-service startup errors were silent. | The switch appeared ineffective with no recovery guidance. | Fixed: failures disable the persisted toggle and show a user-facing reason. |
| Medium | There was no way for a client to change an already saved server without clearing app data. | Network changes could strand the app on an old LAN address. | Fixed: the top bar exposes a server-reset action that also stops monitoring. |
| Medium | StateFlow read-copy-write updates occurred in concurrent coroutines. | A profile, auth, or notification result could overwrite another state change. | Fixed: state mutations now use atomic `MutableStateFlow.update`. |
| Low | Compose collected flows even when the Activity was not in an active lifecycle state. | Unnecessary background UI work. | Fixed: both activities use lifecycle-aware collection. |
| Low | Alert notifications did not open the app. | Poor recovery/navigation experience. | Fixed: alerts include an immutable content `PendingIntent`. |
| Low | URL normalization behavior had no automated regression tests. | Easy to reintroduce setup crashes. | Fixed: focused JVM tests cover scheme insertion, path/query normalization, and invalid protocols. |

Android 14 foreground-service requirements were also checked. Both client manifests already declare `FOREGROUND_SERVICE_DATA_SYNC`, and the service already declares `foregroundServiceType="dataSync"`; no adjustment was needed there.

## Remaining risks and recommended next work

### 1. Authenticate the monitor API (highest priority)

The client currently sends a Firebase UID as a query/form value, but the server does not verify that the caller owns it. The robust design is:

1. Obtain the current Firebase ID token in the app.
2. Send it as `Authorization: Bearer <token>` through an OkHttp interceptor.
3. Verify the signature, issuer, audience, expiry, and UID with Firebase Admin on the monitor.
4. Derive the profile UID from the verified token instead of accepting `uid` from the request.
5. Add separate operator authentication and authorization for admin status/actions.

Until then, keep `PC_MONITOR_HOST` and port reachable only on a trusted, firewalled LAN.

### 2. Replace or constrain cleartext HTTP

All variants intentionally support a plain-HTTP local monitor. For broader deployment, add TLS (a reverse proxy with a trusted/local certificate is enough), turn off global cleartext support in release variants, and use a Network Security Config for any narrowly scoped development exception.

### 3. Device and release testing

Before distribution, validate:

- free/paid builds with a real, untracked `google-services.json`;
- email and Google auth on a device;
- Android 13 notification denial/retry behavior;
- Android 14 foreground service startup and stop behavior;
- account A to account B switching with independent cursors;
- process death, sticky service restart, offline monitor, Wi-Fi changes, and OEM battery restrictions;
- Calendar state retention and WebView cleanup;
- signed release builds, versioning, R8/resource shrinking, and Play policy declarations for ads/data collection.

### 4. Background delivery architecture

A 30-second foreground poll is understandable for a LAN-only server, but it is battery-intensive and some OEMs will still restrict it. Longer-term options are an internet-reachable authenticated push bridge using FCM, a user-configurable polling interval, or a less frequent WorkManager sync when immediate alerts are not required.

### 5. Product quality

Move user-facing text into string resources, add Spanish/English localization, add content descriptions and larger-touch-target review, separate profile-save and notification-refresh loading flags, and add Compose UI tests for login/setup/tab/monitoring flows.

## Validation strategy

- JVM unit tests for URL normalization.
- `android/check.sh` selects the wrapper or system Gradle and validates every configured flavor.
- Compile all three debug variants (`admin`, `free`, `paid`) when Firebase configuration is present for client variants.
- Android lint for all variants.
- Repository diff review to ensure changes remain scoped to Android and this report.

## Validation results for this change

- `git diff --check`: passed.
- `bash -n android/check.sh`: passed.
- `.venv/bin/python -m py_compile src/40_monitor/001b-monitor-web.py`: passed.
- `android/check.sh`: correctly detected that neither system Gradle nor a generated wrapper is currently available and exited with setup guidance. Android compile, unit-test, and lint tasks remain pending until the parallel system dependency setup completes.
