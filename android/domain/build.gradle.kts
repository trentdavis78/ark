plugins {
    kotlin("jvm")
    `java-library`
}

// Target JVM 17 bytecode so the Android client consumes this module unchanged,
// without pinning a toolchain the build machine may not have.
java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
        // The engine must not silently widen a nullable route estimate or a
        // missing card field into a default; warnings here are usually that.
        allWarningsAsErrors.set(true)
    }
}

dependencies {
    testImplementation(kotlin("test"))
}

tasks.test {
    useJUnitPlatform()
    testLogging { events("failed") }
}
