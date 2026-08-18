pluginManagement {
    repositories {
        gradlePluginPortal()
        google()
        mavenCentral()
    }
}

dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "blacktop"

// The decision engine is a pure-Kotlin JVM module with no Android
// dependencies, so it compiles and unit-tests without an SDK, emulator, or
// device. That is deliberate: the math that decides a driver's income should
// be testable in a plain JVM, and the boundary keeps it free of platform
// coupling.
include(":domain")

// The client needs the Android SDK. Include it only when one is actually
// configured, so `gradle :domain:test` still works on a bare machine and in
// CI rather than failing at configuration time.
val hasAndroidSdk = System.getenv("ANDROID_HOME") != null ||
        System.getenv("ANDROID_SDK_ROOT") != null ||
        file("local.properties").let { it.exists() && it.readText().contains("sdk.dir") }

if (hasAndroidSdk) {
    include(":app")
} else {
    logger.lifecycle(
        "No Android SDK found — configuring :domain only. " +
        "Set ANDROID_HOME (or local.properties sdk.dir) to build :app."
    )
}
