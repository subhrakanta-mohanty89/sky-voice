#!/usr/bin/env bash
#
# Build an installable APK for the Sky Voice AI React Native app.
#
# Prerequisites (already present on the original build machine):
#   - Node 18+ and npm
#   - JDK 17
#   - Android SDK (platform 34, build-tools 34, an NDK) with licenses accepted
#   - android/local.properties pointing sdk.dir at your SDK
#
# Usage:
#   ./build.sh            # release APK (standalone, JS bundled in — sideload & run)
#   ./build.sh debug      # debug APK (needs Metro running: `npm start` + `adb reverse`)
#
# Output:
#   dist/SkyVoiceAI-release.apk   (default)
#   dist/SkyVoiceAI-debug.apk     (debug)
set -euo pipefail

VARIANT="${1:-release}"
case "$VARIANT" in
  release|debug) ;;
  *) echo "Unknown variant '$VARIANT' (use 'release' or 'debug')"; exit 1 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> Installing JS dependencies (if needed)"
if [ ! -d node_modules ]; then
  npm install
fi

echo "==> Resolving JDK 17"
if command -v /usr/libexec/java_home >/dev/null 2>&1; then
  export JAVA_HOME="$(/usr/libexec/java_home -v 17)"
fi
echo "    JAVA_HOME=${JAVA_HOME:-<system default>}"

echo "==> Resolving Android SDK"
if [ -f android/local.properties ]; then
  SDK_DIR="$(grep -E '^sdk\.dir=' android/local.properties | head -1 | cut -d= -f2-)"
  export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$SDK_DIR}"
  export ANDROID_HOME="${ANDROID_HOME:-$SDK_DIR}"
fi
echo "    ANDROID_SDK_ROOT=${ANDROID_SDK_ROOT:-<unset>}"

if [ "$VARIANT" = "release" ]; then
  echo "==> Gradle assembleRelease (standalone, JS + Hermes bundled in)"
  ( cd android && ./gradlew --no-daemon assembleRelease )
  APK="android/app/build/outputs/apk/release/app-release.apk"
  OUT="dist/SkyVoiceAI-release.apk"
else
  echo "==> Gradle assembleDebug (requires Metro: npm start)"
  ( cd android && ./gradlew --no-daemon assembleDebug )
  APK="android/app/build/outputs/apk/debug/app-debug.apk"
  OUT="dist/SkyVoiceAI-debug.apk"
fi

mkdir -p dist
cp "$APK" "$OUT"
echo "==> Done: $OUT"
echo "    Install on a USB-connected phone (USB debugging on) with:"
echo "      adb install -r $OUT"

