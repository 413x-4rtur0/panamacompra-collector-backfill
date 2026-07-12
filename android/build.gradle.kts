plugins {
    id("com.android.application") version "8.2.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.22" apply false
    id("org.jetbrains.kotlin.plugin.serialization") version "1.9.22" apply false
    // Applied conditionally in app/build.gradle.kts (only when
    // app/google-services.json exists) — see android/README.md. This makes
    // the classpath available without forcing every flavor (incl. admin,
    // which needs no Firebase at all) to have that file present just to sync.
    id("com.google.gms.google-services") version "4.4.2" apply false
}
