plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization")
}

// google-services (Firebase Auth for the free/paid client flavors) only
// applies once you've dropped your own Firebase project's
// app/google-services.json in place — see android/README.md. Without it,
// the plugin fails the whole module's Gradle sync, which would break the
// admin flavor too even though admin never touches Firebase; gating it here
// keeps `admin` buildable with zero Firebase setup.
if (file("google-services.json").exists()) {
    apply(plugin = "com.google.gms.google-services")
}

android {
    namespace = "com.panamacompra.monitor"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.panamacompra.monitor"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    // Three variants sharing one codebase:
    //  - free:  client notification app, AdMob banner ad
    //  - paid:  client notification app, no ad SDK at all
    //  - admin: operator dashboard + manual-action controls (the original scaffold)
    // free/paid additionally pull in src/clientCommon (see sourceSets below);
    // admin's Kotlin lives entirely in src/admin. src/main holds only what's
    // truly shared: the Retrofit client, DTOs, and the Compose theme.
    flavorDimensions += "tier"
    productFlavors {
        create("free") {
            dimension = "tier"
            applicationIdSuffix = ".free"
            versionNameSuffix = "-free"
        }
        create("paid") {
            dimension = "tier"
            applicationIdSuffix = ".paid"
            versionNameSuffix = "-paid"
        }
        create("admin") {
            dimension = "tier"
            applicationIdSuffix = ".admin"
            versionNameSuffix = "-admin"
        }
    }

    sourceSets {
        getByName("free") {
            java.srcDirs("src/clientCommon/java")
            res.srcDirs("src/clientCommon/res")
        }
        getByName("paid") {
            java.srcDirs("src/clientCommon/java")
            res.srcDirs("src/clientCommon/res")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.10"
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.0")
    implementation("androidx.activity:activity-compose:1.9.0")

    implementation(platform("androidx.compose:compose-bom:2024.05.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    debugImplementation("androidx.compose.ui:ui-tooling")

    implementation("androidx.datastore:datastore-preferences:1.1.1")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")

    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.jakewharton.retrofit:retrofit2-kotlinx-serialization-converter:1.0.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")

    // AdMob only in the free flavor — the paid flavor has zero ad code/deps.
    "freeImplementation"("com.google.android.gms:play-services-ads:23.1.0")

    // Firebase Auth (email/password + Google Sign-In) for both client
    // flavors — the admin flavor never touches auth. Needs
    // app/google-services.json from your own Firebase project (see README);
    // the BOM pins compatible versions so no individual version numbers below.
    "freeImplementation"(platform("com.google.firebase:firebase-bom:33.1.2"))
    "freeImplementation"("com.google.firebase:firebase-auth-ktx")
    "freeImplementation"("com.google.android.gms:play-services-auth:21.2.0")
    "paidImplementation"(platform("com.google.firebase:firebase-bom:33.1.2"))
    "paidImplementation"("com.google.firebase:firebase-auth-ktx")
    "paidImplementation"("com.google.android.gms:play-services-auth:21.2.0")
}
