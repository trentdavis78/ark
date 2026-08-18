plugins {
    id("com.android.application")
    kotlin("android")
    id("org.jetbrains.kotlin.plugin.compose")
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

    buildTypes {
        release {
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
