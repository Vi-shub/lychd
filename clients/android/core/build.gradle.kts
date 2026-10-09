plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}
kotlin {
    jvmToolchain(17)
    sourceSets.main { kotlin.srcDir(layout.buildDirectory.dir("generated/wire")) }
}
val generateWire by tasks.registering(Exec::class) {
    val schema = rootProject.file("../web/openapi.json")
    val generator = rootProject.file("tools/generate_wire.py")
    val output = layout.buildDirectory.file("generated/wire/io/github/hexanomicon/lychd/companion/wire/Models.kt")
    inputs.files(schema, generator)
    outputs.file(output)
    commandLine("python3", generator, "--schema", schema, "--output", output.get().asFile)
}
tasks.named("compileKotlin") { dependsOn(generateWire) }
val verifyWireGenerator by tasks.registering(Exec::class) {
    workingDir(rootProject.file("tools"))
    commandLine("python3", "-m", "unittest", "-v", "test_generate_wire")
}
tasks.named("check") { dependsOn(verifyWireGenerator) }
dependencies {
    implementation(libs.ktor.core)
    implementation(libs.ktor.mock)
    implementation(libs.coroutines.core)
    implementation(libs.serialization.json)
    testImplementation(libs.junit)
    testImplementation(libs.coroutines.test)
}
