# PanamaCompra Monitor — Android (scaffold)

One Gradle project, three build flavors sharing a common core:

| Flavor  | Who it's for | What it does | Ads |
|---------|---------------|---------------|-----|
| `admin` | The operator (you) | Dashboard mirroring `/api/status`, manual-action buttons (`/api/manual-action`) | none |
| `free`  | A signed-up client | Login (email or Google), self-service notification filters, Alerts inbox, filtered Calendar | AdMob banner (test ad unit) |
| `paid`  | Same as free | Identical, no ad code compiled in at all | none |

All three talk to the existing web monitor's local HTTP API
(`src/40_monitor/001b-monitor-web.py`) over the LAN for everything except
sign-in itself. There's no cloud backend for the *data* — no FCM push,
nothing hosted — but **login is a cloud service** (Firebase Auth), a real
architecture tradeoff explained below.

## Not verified in this sandbox

This dev environment has no Android SDK, no Gradle, and only Java 8 (Android
needs JDK 17). Written against known-current library versions but **not
built or run**. Open in Android Studio (bundles its own JDK 17) to sync and
catch anything that needs adjusting.

The Gradle wrapper jar/scripts (`gradlew`, `gradlew.bat`,
`gradle/wrapper/gradle-wrapper.jar`) aren't included — this sandbox can't
fetch the binary. Android Studio offers to generate them on first open, or
run `gradle wrapper --gradle-version 8.4` with a system Gradle install.

The **backend changes** this app depends on *were* verified in this sandbox —
compiled and smoke-tested end-to-end against the real monitor process and
SQLite DB (profile upsert, filter suggestions, client-scoped calendar, the
app-only notification path for clients with no WhatsApp group). Only the
Android/Kotlin side is unverified.

## Hybrid architecture: cloud auth, local everything else

Email/Google sign-in needs an identity provider — this uses **Firebase
Auth**, Google's free hosted auth service. That means:
- **Login itself needs internet.** Firebase Auth is a cloud service.
- **Everything after login stays local.** Once signed in, the app uses the
  Firebase UID as its identity when talking to the LAN-local monitor —
  notifications, profile, filters, calendar. None of that data goes through
  Firebase or any cloud backend.
- **The local server does not verify Firebase ID tokens cryptographically.**
  It trusts whatever `uid` the app sends. That matches this whole project's
  existing threat model (the monitor has zero auth anywhere else either —
  admin's DANGER actions are unauthenticated too), but means anyone on the
  same LAN who learns another client's uid could read/edit that client's
  profile. Fine for a trusted home/office network; don't expose this monitor
  beyond one.

### Required one-time setup: a Firebase project

Both `free` and `paid` **will not compile** without this — the
`com.google.gms.google-services` Gradle plugin (which generates
`R.string.default_web_client_id`, used by Google Sign-In) only applies when
`app/google-services.json` exists. `admin` is unaffected and builds with zero
Firebase setup (the plugin is gated behind a file-existence check in
`app/build.gradle.kts` specifically so admin doesn't need this).

1. Create a project at https://console.firebase.google.com
2. Add an Android app for each client applicationId: `com.panamacompra.monitor.free`
   and `com.panamacompra.monitor.paid` (one Firebase project can hold both).
3. Enable **Authentication → Sign-in method → Email/Password** and **Google**.
4. Download `google-services.json`, place it at `android/app/google-services.json`.
5. Sync — `R.string.default_web_client_id` will now exist and both client
   flavors will compile.

## Building a specific flavor

Android Studio: Build Variants panel → pick `freeDebug` / `paidDebug` /
`adminDebug`. CLI equivalent once the wrapper exists:
`./gradlew assembleFreeDebug` (or `assemblePaidDebug` / `assembleAdminDebug`).

## Before connecting (all flavors)

1. **Bind the monitor beyond loopback.** `PC_MONITOR_HOST` defaults to
   `127.0.0.1` (loopback-only). Set it to `0.0.0.0` (web monitor Settings tab
   → "Monitor bind host", or the env var) and restart the monitor so phones
   on the LAN can reach it.
2. **Same network.** Phone and monitor host must be on the same LAN/WiFi.
   Find the host's LAN IP and the monitor's port (`PC_MONITOR_PORT`, default
   `8766`).
3. **No auth, no TLS on the monitor itself.** Local dev tool, not
   internet-facing. Manifests enable cleartext traffic deliberately — only
   point any of these apps at a monitor on a trusted network.

## admin flavor

Same as before: tap **Connect**, enter `http://<lan-ip>:<port>/`. Dashboard +
manual actions, with a confirm gate on DANGER-labelled actions (e.g. "STOP
all runners").

## free / paid flavor (client apps)

### First launch flow

1. **Server address** — `http://<lan-ip>:<port>/`.
2. **Sign in / sign up** — email+password or Google. This is what identifies
   the client now (no more manually-typed codes — the legacy `app_code`
   field still works for profiles an operator sets up manually in the web/Tk
   monitor, but the app itself always uses the signed-in Firebase uid).
3. **Profile tab** — phone number, profession/location/institution (each
   with tap-to-fill suggestion chips pulled from `GET /api/filter-suggestions`,
   which are the actual distinct `entidad`/`dependencia`/`grupo` values
   already in the archive — no hand-maintained list to keep in sync), which
   notification types to receive (new opportunities / item details / status
   changes), whether the Calendar tab is shown, and an optional advanced
   keyword filter (same OR/AND/NOT syntax as the WhatsApp filters:
   `salud + insumos, -construccion`). Saving POSTs straight to the monitor —
   fully self-service, no operator approval step.
4. **Alerts tab** — toggle **Monitoring** on (Android 13+ prompts for the
   notification permission first). Starts `NotificationPollingService`, a
   foreground service (persistent low-priority "Monitoring for new
   opportunities…" notification) that polls `/api/client-notifications`
   every 30s and posts a real Android notification for each new row. The
   in-app "Recent alerts" list is a separate, independent fetch — doesn't
   need Monitoring on, useful for browsing history.
5. **Calendar tab** — a WebView loading `/client-calendar?uid=<uid>`, the
   same month/week/day/year hourly-grid calendar built for the web/Tk
   monitors, server-filtered to just this client's own purposes/filters
   (`GET /api/client-calendar-grid`). Hidden if the client turned off
   "Show calendar matching my filters" in their profile.

### Why a foreground service instead of push

The monitor is a local-LAN-only HTTP server with no internet-reachable
backend, so there's no FCM/cloud push to hook into for the actual
notification *data* (only login goes through the cloud). A foreground
service polling every 30s is the standard fallback for "deliver
notifications from a local server while the app is backgrounded." Some OEMs
(aggressive battery optimization) may still kill it — worth telling clients
to whitelist the app from battery optimization if alerts stop arriving.

### How self-service filters reach WhatsApp too

A client's `purposes`/`filters` live in the same
`data/config/waha_clients.json` profile the WhatsApp notifier already reads
— editing them from the app changes what that client's WhatsApp group
receives too, not just app notifications. If the client signed up entirely
through the app (never had a WhatsApp group), their profile gets a synthetic
`chat_id` of `app:<firebase_uid>`; `notify_whatsapp.send_text()` recognizes
that prefix and logs directly to `app_notifications` instead of attempting a
WhatsApp send that would just fail.

## AdMob (free flavor)

`src/free/AndroidManifest.xml` and `src/free/java/.../ads/BannerAd.kt` both
currently use **Google's published TEST ad unit/app IDs** (safe for
development, will only ever show test ads). Before any release build:
1. Create a real AdMob app + banner ad unit at https://apps.admob.com
2. Replace the `APPLICATION_ID` meta-data value in
   `src/free/AndroidManifest.xml`.
3. Replace `TEST_BANNER_AD_UNIT_ID` in
   `src/free/java/com/panamacompra/monitor/ads/BannerAd.kt`.

## Paid tier mechanism

Currently just "a separate flavor with no ad code" — install it (or list it
on Play as its own paid listing) and there are no ads, full stop. Turning
this into an in-app "remove ads" purchase on top of the free flavor (Google
Play Billing) instead of/alongside a separate paid listing is a reasonable
next step but isn't wired up here.

## Project layout

```
app/src/
  main/         shared: Retrofit client (ApiClient, MonitorApi + all DTOs),
                Compose theme
  admin/        operator dashboard: MainActivity, MonitorViewModel,
                SettingsStore (own DataStore: "monitor_settings")
  clientCommon/ client app shared by free+paid: MainActivity (auth screens +
                Alerts/Calendar/Profile tabs), ClientViewModel,
                ClientSettingsStore (own DataStore: "client_settings" — just
                base URL + monitoring toggle + notification cursor; identity
                comes from Firebase, not stored here), AuthRepository
                (Firebase wrapper), NotificationPollingService — wired into
                both flavors via sourceSets{} in app/build.gradle.kts, not a
                "real" AGP flavor
  free/         AdsInit.kt + BannerAd.kt (real AdMob), manifest with AdMob
                meta-data + POST_NOTIFICATIONS/FOREGROUND_SERVICE perms
  paid/         AdsInit.kt + BannerAd.kt (both no-ops), same manifest minus
                AdMob meta-data
```

`app/google-services.json` (you provide, gitignored) enables Firebase for
`free`/`paid`; `admin` never needs it.

## Backend changes that came with this

In `src/40_monitor/001b-monitor-web.py`:
- `GET /api/actions` — MANUAL_ACTIONS as JSON (admin flavor).
- `GET /api/notifications?since=<id>` — full firehose of every outbound
  WhatsApp send, for the admin flavor.
- `GET /api/client-notifications?uid=<firebase_uid>` (or legacy
  `?code=<app_code>`) `&since=<id>` — scoped to one client profile's
  `chat_id`.
- `GET /api/client-profile?uid=` / `POST /api/client-profile` (form field
  `profile` = JSON object string, same convention as the existing
  `/api/waha-clients`) — self-service profile read/upsert, concurrency-safe
  via `WAHA_CLIENTS_LOCK`. The POST body is deliberately narrow (only
  client-editable fields) so it can never clobber operator-set fields like
  `chat_id`/`app_code`/`enabled`.
- `GET /api/filter-suggestions` — distinct `entidad`/`dependencia`/`grupo`
  values from the archive (institutions/locations/professions), top 40 each
  by frequency.
- `GET /api/client-calendar-grid` / `GET /client-calendar` — the calendar
  grid JSON and the standalone HTML page the client apps' WebView loads,
  both scoped to one client's filters via a new `filter_fn` reused from
  `notify_whatsapp.evaluate_filter()`/`row_filter_haystack()` so the calendar
  and WhatsApp/app notifications always agree on what matches.
- `_normalize_client_profile()` extracted as a shared helper (was
  duplicated), now includes `app_code`, `firebase_uid`, `email`, `phone`,
  `profession`, `location`, `institution`, `calendar_visible`.

In `src/40_monitor/001a-monitor-tk.py`: its own client-profile save path
(`save_clients()`) preserves the same new fields, so editing profiles from
either monitor never drops what a client set from the app.

In `src/common.py`: `app_notifications` table (`ensure_db_schema()`) +
`log_app_notification()` helper.

In `src/30_notify/010-waha-client.py`: `send_text()` calls
`pc_common.log_app_notification()` right after a successful WAHA send — the
single choke point every outbound WhatsApp message passes through regardless
of caller, so this mirrors WhatsApp delivery 1:1 without touching every call
site.

In `src/20_pipeline/020-notify-whatsapp.py`: the per-client-profile fan-out
loop in `send_text()` now special-cases `chat_id` values starting with
`"app:"` (app-only clients) — logs locally instead of attempting a WhatsApp
send.

In `src/50_tools/030-opportunity-calendar.py`: `fetch_events()`/`render_view()`
take an optional `filter_fn`, and `fetch_events()`'s SELECT now also pulls
`entidad`/`dependencia`/`modalidad`/`detail_json_path` (needed to build the
same filter-match haystack the WhatsApp notifier uses).

## Suggested next steps

- Play Billing "remove ads" IAP if you want one listing instead of two.
- Tapping a notification could deep-link into the app instead of just
  opening it.
- Password reset / email verification flows (Firebase supports both, not
  wired into the UI here).
- If this ever leaves a trusted LAN: verify Firebase ID tokens server-side
  instead of trusting the uid outright, and add a shared-secret/API-key
  header check for the rest of the API.
