pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
    resolutionStrategy {
        eachPlugin {
            // AGP 的 plugin marker 可能与实现 jar 缓存不同步；
            // 直接指向 com.android.tools.build:gradle 实现，避免要求 marker 存在。
            when (requested.id.id) {
                "com.android.application", "com.android.library" ->
                    useModule("com.android.tools.build:gradle:${requested.version}")
            }
        }
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "dasinong-android"

// 中文目录名映射到 ASCII 模块名
include(":ui", ":backend")
project(":ui").projectDir = file("UI")
project(":backend").projectDir = file("后端")
