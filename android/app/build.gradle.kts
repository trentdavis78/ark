import java.io.FileInputStream
import java.util.Properties

plugins {
    id("com.android.application")
    kotlin("android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Release signing. PRD §11 expects direct-APK distribution, since accessibility
// services used for non-accessibility purposes need strong Play Store
// justification — so a signed release build is the primary artifact, not an
// afterthought.
//
// Credentials come from an untracked keystore.properties (see
// keystore.properties.example). Without it, release builds are simply
// unsigned rather than failing, so a fresh clone still builds.
val keystoreProperties = Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) FileInputStream(f).use { load(it) }
}

android {
    namespace = "com.blacktop"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.blacktop"
        minSdk = 29          // Android 10: overlay + accessibility behaviour we rely on
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"

        // The vision fallback endpoint. Empty by default: the app is fully
        // functional on node capture alone, and a driver who never sets this
        // simply never uses the fallback. Point it at `server/` with:
        //   ./gradlew assembleDebug -PextractEndpoint=http://10.0.2.2:8787/api/extract
        val extractEndpoint = (project.findProperty("extractEndpoint") as String?).orEmpty()
        buildConfigField("String", "EXTRACT_ENDPOINT", "\"$extractEndpoint\"")
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    signingConfigs {
        if (keystoreProperties.isNotEmpty()) {
            create("release") {
                storeFile = rootProject.file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.findByName("release")
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"),
                          "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions { jvmTarget = "17" }

    testOptions {
        unitTests.all { it.useJUnitPlatform() }
    }
}

dependencies {
    implementation(project(":domain"))

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation(platform("androidx.compose:compose-bom:2024.10.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.datastore:datastore-preferences:1.1.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    debugImplementation("androidx.compose.ui:ui-tooling")
    testImplementation(kotlin("test"))
}
