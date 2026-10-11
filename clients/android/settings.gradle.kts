pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "LychDCompanion"
include(":core", ":interaction")
// Explicit opt-in for running protocol tests on a machine without an Android SDK.
if (providers.gradleProperty("coreOnly").orNull != "true") include(":app")

// Isolated offline text prototype; never replaces the Companion app.
if (providers.gradleProperty("includeOfflineApp").orNull == "true") include(":offline")
