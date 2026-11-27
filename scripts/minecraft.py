"""Build an argv list for the installed Windows x64 NeoForge client."""
import os
from pathlib import Path


def allowed(rules):
    features = {"has_custom_resolution", "has_quick_plays_support", "is_quick_play_singleplayer"}
    enabled = not rules
    for rule in rules:
        system = rule.get("os", {})
        if system.get("name", "windows") != "windows" or system.get("arch", "x86_64") != "x86_64":
            continue
        if all((key in features) == value for key, value in rule.get("features", {}).items()):
            enabled = rule["action"] == "allow"
    return enabled


def launch_args(config, version, directory, run, port):
    root = directory.parents[1]
    libraries = Path(config.get("librariesDir", root / "libraries")).resolve(strict=True)
    assets = Path(config.get("assetsDir", root / "assets")).resolve(strict=True)
    natives = Path(config.get("nativesDir", directory / (directory.name + "-natives"))).resolve(strict=True)
    (natives / "glfw.dll").resolve(strict=True)
    classpath = [(directory / (directory.name + ".jar")).resolve(strict=True)]
    for lib in version["libraries"]:
        artifact = lib.get("downloads", {}).get("artifact", {})
        if artifact.get("path") and allowed(lib.get("rules", [])):
            classpath.append((libraries / artifact["path"]).resolve(strict=True))
    asset_index = version.get("assets") or version.get("assetIndex", {}).get("id", "30")
    jvm = [
        "-Xms1G", "-Xmx4G", "-XX:+UseG1GC", f"-Djava.library.path={natives}",
        f"-Djna.tmpdir={natives}", f"-Dorg.lwjgl.system.SharedLibraryExtractPath={natives}",
        f"-Dio.netty.native.workdir={natives}", "-Dminecraft.launcher.brand=VLA-Marionette",
        "-Dminecraft.launcher.version=0.1.0", f"-DlibraryDirectory={libraries}",
        "-Djava.net.preferIPv6Addresses=system", "-Dvla.bridge.host=127.0.0.1",
        f"-Dvla.bridge.port={port}", f"-Dvla.run.id={run.name}", "-Dvla.fov.mode=fixed",
        "-Dvla.fov.vertical=70", "--sun-misc-unsafe-memory-access=allow",
        "--enable-native-access=ALL-UNNAMED", "--add-opens", "java.base/java.lang.invoke=ALL-UNNAMED",
        "--add-exports", "jdk.naming.dns/com.sun.jndi.dns=java.naming",
        "-cp", os.pathsep.join(map(str, classpath)),
    ]
    game = [
        "--username", "VLA", "--version", directory.name, "--gameDir", str(run / "game"),
        "--assetsDir", str(assets), "--assetIndex", str(asset_index), "--uuid", "0" * 32,
        "--accessToken", "0", "--clientId", "0", "--xuid", "0", "--versionType", "release",
        "--width", "854", "--height", "480", "--quickPlaySingleplayer", "demo",
        "--fml.neoForgeVersion", "26.1.2.126", "--fml.mcVersion", "26.1.2", "--fml.neoFormVersion", "1",
    ]
    return [*jvm, version["mainClass"], *game]
