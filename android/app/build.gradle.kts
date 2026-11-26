import java.util.Properties

plugins {
    // Kotlin is built into AGP 9: no kotlin-android plugin.
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// The repo: android/ sits in it.
val repoDir: File = rootProject.projectDir.parentFile

// The owner's release key, outside every repo in ../xrn-signing.properties next to the
// repo (storeFile, storePassword, keyAlias, keyPassword; storeFile relative to that file's
// folder). It is found only from the main checkout; a worktree's release build is unsigned.
val xrnSigningFile: File = repoDir.resolve("../xrn-signing.properties")
val xrnSigning: Properties? = xrnSigningFile.takeIf { it.isFile }?.let { f ->
    Properties().apply { f.inputStream().use { load(it) } }
}

// The hub's address stays out of the repo: it comes from local.properties (xrnmoni.hub), which
// git ignores, and only fills in the setup screen.
val machine = Properties().apply {
    rootProject.file("local.properties").takeIf { it.exists() }?.inputStream()?.use(::load)
}

fun filled(name: String): String = (findProperty(name) ?: machine.getProperty(name) ?: "").toString()

android {
    namespace = "com.xern.xrnmoni"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.xern.xrnmoni"
        // 28: the lock screen (KEYGUARD_SHOWN/HIDDEN) and shutdown/startup usage events start there.
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        buildConfigField("String", "DEFAULT_HUB", "\"${filled("xrnmoni.hub")}\"")
    }

    signingConfigs {
        if (xrnSigning != null) {
            create("xrn") {
                storeFile = xrnSigningFile.parentFile.resolve(xrnSigning.getProperty("storeFile"))
                storePassword = xrnSigning.getProperty("storePassword")
                keyAlias = xrnSigning.getProperty("keyAlias")
                keyPassword = xrnSigning.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfigs.findByName("xrn")?.let { signingConfig = it }
        }
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.work.runtime.ktx)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.kotlinx.serialization.json)
    debugImplementation(libs.androidx.compose.ui.tooling)
    testImplementation(libs.junit)
}
