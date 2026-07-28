#!/usr/bin/env bash
set -euo pipefail

ANDROID_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ANDROID_ROOT"

if [[ -x ./gradlew ]]; then
  GRADLE=(./gradlew)
elif command -v gradle >/dev/null 2>&1; then
  GRADLE=(gradle)
else
  echo "Android validation requires Gradle or a generated ./gradlew wrapper." >&2
  echo "After Gradle is installed, run: gradle wrapper --gradle-version 8.4" >&2
  exit 2
fi

tasks=(
  testAdminDebugUnitTest
  lintAdminDebug
  assembleAdminDebug
)

if [[ -f app/google-services.json ]]; then
  tasks+=(
    testFreeDebugUnitTest
    testPaidDebugUnitTest
    lintFreeDebug
    lintPaidDebug
    assembleFreeDebug
    assemblePaidDebug
  )
else
  echo "Firebase config not found; validating admin only (free/paid skipped)." >&2
fi

"${GRADLE[@]}" --stacktrace "${tasks[@]}"
