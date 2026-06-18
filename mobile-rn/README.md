# Sky Voice AI — React Native app (`mobile-rn`)

A native **React Native** softphone for the Sky Voice AI Twilio backend. It is
a sibling to:

- `frontend/` — the web dashboard (React + Vite, browser `@twilio/voice-sdk`)
- `frontend-exe/` — the desktop wrapper (WKWebView/WebView2)
- `mobile-apk/` — the **Capacitor** Android wrapper (ships the web bundle in a
  WebView and reuses the browser SDK)

**Why a separate codebase from `mobile-apk`?** The web/desktop/Capacitor apps
all use `@twilio/voice-sdk`, which is a **browser-only WebRTC** library. It has
no working implementation in a bare React Native JS runtime. This app instead
uses **`@twilio/voice-react-native-sdk`**, which wraps the native Twilio iOS and
Android calling libraries. Different SDK, different API, so the call layer is
rebuilt — but it talks to the **exact same backend** and the **same**
`POST /api/v1/token` access-token endpoint.

---

## Status (what works today)

| Capability | Status |
| --- | --- |
| Login against `/api/v1/auth/login` | ✅ Works |
| Voice access token via `/api/v1/token` | ✅ Works (same token as web) |
| **Outbound** calls (native audio) | ✅ Works |
| In-call mute / hangup / timer | ✅ Works |
| Call history, contacts (local), presence | ✅ Works |
| Standalone release APK builds & installs | ✅ Verified (`./build.sh`) |
| **Incoming** calls | ⚠️ Needs push setup — see below |

> **Incoming calls require extra setup.** Unlike the browser SDK (which holds a
> WebSocket open), the native SDK delivers incoming calls as a **push
> notification** (`CallInvite`). That needs (1) a Firebase project +
> `google-services.json`, (2) a **Twilio Push Credential** created from the FCM
> key, and (3) the credential's SID embedded in the access token. The backend
> already supports (3) — set `TWILIO_PUSH_CREDENTIAL_SID` (see
> [Incoming-call push](#incoming-call-push-fcm)). Until FCM is wired,
> `voice.register()` fails gracefully and outbound calling is unaffected.

---

## Prerequisites

The original build machine (a Mac mini) had:

- Node 18+ and npm
- JDK 17 (`/usr/libexec/java_home -v 17`)
- Android SDK: platform 34, build-tools 34.0.0, an NDK (26.3.x), licenses
  accepted
- `android/local.properties` with `sdk.dir=...` (per-machine, gitignored)

No Android Studio is required — the Gradle wrapper (`gradlew`) is used directly.

## Install & build (standalone APK)

```bash
cd mobile-rn
npm install
./build.sh          # → dist/SkyVoiceAI-release.apk  (standalone, installable)
```

`build.sh` resolves JDK 17 + the Android SDK and runs `./gradlew
assembleRelease`. The **release** APK bundles the JS (`assets/index.android.bundle`)
and Hermes engine **inside the APK**, so it runs without Metro — just sideload it:

```bash
adb install -r dist/SkyVoiceAI-release.apk
```

It is signed with the standard RN `debug.keystore` (fine for sideloading; see
[Release signing](#release-signing) for a Play Store keystore). Verified output:
package `com.skyvoiceai`, label "Sky Voice AI", minSdk 24 / target 34, ~101 MB,
with `RECORD_AUDIO` / `MODIFY_AUDIO_SETTINGS` / `POST_NOTIFICATIONS`.

A **debug** APK (needs Metro running) can be built with `./build.sh debug` →
`dist/SkyVoiceAI-debug.apk`. Equivalent manual steps:

```bash
export JAVA_HOME="$(/usr/libexec/java_home -v 17)"
export ANDROID_SDK_ROOT=/opt/homebrew/share/android-commandlinetools
cd android && ./gradlew assembleRelease   # or assembleDebug
# release APK: android/app/build/outputs/apk/release/app-release.apk
```

## Run on a device/emulator (dev)

```bash
npm start            # Metro bundler (own terminal)
npm run android      # build + install + launch
```

The Android emulator reaches a local backend at `http://10.0.2.2:<port>`; set
`API_BASE_URL` in [`src/config.ts`](src/config.ts) accordingly. By default it
points at the production Cloud Run backend.

---

## Architecture

```
App.tsx
  └─ SafeAreaProvider
     └─ AuthProvider            (login state, JWT hydrate/refresh)
        └─ VoiceProvider        (subscribes to the voiceService singleton)
           └─ RootNavigator     (auth gate → bottom tabs)
              ├─ LoginScreen
              ├─ Dashboard / Dialer / History / Contacts / Settings
              └─ CallOverlay    (incoming + active-call full-screen UI)
```

- **`src/services/voice.ts`** — the heart of the app. A singleton wrapping the
  native `Voice` object: `init()` (fetch token + register), `connect()`
  (outbound, passes `{ To, FromNumber }` TwiML params), `accept()/reject()`
  (incoming `CallInvite`), `hangup()`, `toggleMute()`. Emits typed events that
  `VoiceContext` turns into React state.
- **`src/services/http.ts`** — fetch wrapper mirroring the web app, but the JWT
  lives in AsyncStorage (hydrated once at startup so `apiFetch` can read it
  synchronously).
- **`src/services/api.ts` / `auth.ts`** — the REST endpoints the softphone
  uses, reusing the web app's request shapes.
- **`src/types.ts`** — copied verbatim from `frontend/src/types.ts`.

### Token flow (identical to web/desktop)

1. `POST /api/v1/auth/login` → JWT (stored in AsyncStorage).
2. `POST /api/v1/token` (Bearer JWT) → Twilio `AccessToken` (VoiceGrant).
3. `voice.connect(token, { params })` for outbound; `voice.register(token)` for
   inbound.

---

## Incoming-call push (FCM)

To enable incoming calls end-to-end:

1. Create a Firebase project; add an Android app with applicationId
   `com.skyvoiceai`; download `google-services.json` into `android/app/`.
2. Add the Google Services Gradle plugin + `firebase-messaging` (see the Twilio
   Voice RN SDK Android quickstart) and a `FirebaseMessagingService` that hands
   the data message to `Voice.handleFirebaseMessage`.
3. In the Twilio Console, create a **Push Credential** (FCM v1) from the
   Firebase service-account key. Note its `CR…` SID.
4. On the backend, set `TWILIO_PUSH_CREDENTIAL_SID=CR…`. The token endpoint
   already attaches it to the grant when present
   (`backend/app/services/twilio_service.py` → `issue_access_token`), so **no
   web/desktop behaviour changes** when it is unset.

This step needs a Firebase account + a physical device for testing and could
not be completed/verified in the autonomous build session that created this app.

---

## Permissions

- Android: `RECORD_AUDIO` is requested at call time
  (`src/services/permissions.ts`); manifest also declares
  `MODIFY_AUDIO_SETTINGS`, Bluetooth, foreground-service and `POST_NOTIFICATIONS`.
- iOS scaffolding exists (`ios/`) but is not configured for calling — Android is
  the supported target here.

## Release signing

The release APK produced by `./build.sh` is currently signed with the standard
RN `debug.keystore` (the scaffold's default `signingConfig` for the release
build type). That makes it **sideload-installable today**, but it is **not**
suitable for the Play Store. For a publishable AAB/APK, generate your own
upload keystore and point `signingConfigs.release` in
`android/app/build.gradle` at it; see the React Native "Publishing to Google
Play" guide.

---

## Notes / limitations

- This project is intentionally **not** a git repo of its own — it lives inside
  the workspace like the other `mobile-*` / `frontend-*` folders.
- `mobile-apk/` (Capacitor) remains the proven, fully-built fallback; this RN
  app is the native-SDK alternative.
- Contacts are stored locally (AsyncStorage), matching the web app which has no
  backend contacts endpoint yet.

