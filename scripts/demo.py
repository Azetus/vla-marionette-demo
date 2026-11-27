"""Create an isolated game directory and run the ROCKET-3 spectator demo."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

from assets import prepare
from minecraft import launch_args


def client_paths(config):
    version_dir = Path(config["minecraftVersionDir"]).resolve()
    metadata = version_dir / (version_dir.name + ".json")
    version = json.loads(metadata.read_text(encoding="utf-8-sig"))
    arguments = version["arguments"]["game"]
    def flag_value(flag):
        return arguments[arguments.index(flag) + 1] if flag in arguments else None
    if (version.get("inheritsFrom") or version["mainClass"] != "net.neoforged.fml.startup.Client"
            or flag_value("--fml.neoForgeVersion") != "26.1.2.126" or flag_value("--fml.mcVersion") != "26.1.2"):
        raise ValueError("Expected a self-contained Minecraft 26.1.2 / NeoForge 26.1.2.126 version profile")
    world = Path(config["worldSource"]).resolve()
    if not (world / "level.dat").is_file():
        raise ValueError(f"Expected a closed single-player test save with level.dat at {world}")
    java = Path(config["java"]).resolve(strict=True)
    info = subprocess.run([java, "-version"], capture_output=True, text=True, check=True)
    if 'version "25.' not in info.stderr:
        raise ValueError("Set java to a Java 25 executable")
    return version_dir, version, world, java


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    version_dir, version, world, java = client_paths(config)
    run = Path("runs", datetime.now().strftime("%Y%m%d-%H%M%S-%f")).resolve()
    if run.is_relative_to(world):
        raise ValueError("worldSource must point to a separate Minecraft save")
    run.mkdir(parents=True)
    print(f"Run directory: {run}", flush=True)
    game = run / "game"
    # Give this run its own loopback port so an older spectator client cannot
    # reconnect into a new scene when it polls after disconnection.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    args = launch_args(config, version, version_dir, run, port)
    (run / "argv.json").write_text(json.dumps({"args": args}, indent=2), encoding="utf-8")
    jar, checkpoint, source = prepare(config)
    (game / "mods").mkdir(parents=True)
    (game / "config").mkdir()
    shutil.copy2(jar, game / "mods" / jar.name)
    shutil.copytree(world, game / "saves" / "demo", ignore=shutil.ignore_patterns("session.lock"))
    options = version_dir / "options.txt"
    lines = options.read_text(encoding="utf-8-sig").splitlines() if options.is_file() else []
    fixed = {"pauseOnLostFocus": "false", "onboardAccessibility": "false", "fullscreen": "false",
             "bobView": "false", "resourcePacks": "[]", "hideGui": "false", "tutorialStep": "none",
             "overrideWidth": "854", "overrideHeight": "480", "smoothCamera": "false"}
    lines = [line for line in lines if line.partition(":")[0] not in fixed]
    (game / "options.txt").write_text("\n".join(lines + [f"{k}:{v}" for k,v in fixed.items()]) + "\n", encoding="utf-8")
    (game / "config/vla_marionette-client.toml").write_text(
        f'[bridge]\nhost="127.0.0.1"\nport={port}\n[capture]\nwidth=854\nheight=480\n'
        'mode="tick"\ntick_interval=1\n[state]\nmode="tick"\ntick_interval=1\n[fov]\nmode="fixed"\n')
    env = dict(os.environ, PYTHONPATH=str(source),
               ROCKET_CKPT=str(checkpoint), PYTHONNOUSERSITE="1")
    print("Opening Minecraft. Watch the game window; the host will build the scene.", flush=True)
    with (run / "java.stdout.log").open("wb") as out, (run / "java.stderr.log").open("wb") as err:
        client = subprocess.Popen([java, *args], cwd=game,
                                  stdout=out, stderr=err)
    (run / "client.pid").write_text(str(client.pid))
    host = subprocess.Popen([sys.executable, "-u", "host/demo.py", str(run), str(port)], env=env)
    try:
        while host.poll() is None:
            try:
                host.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if client.poll() is not None:
                    raise RuntimeError(f"Minecraft closed. See {run / 'java.stderr.log'}")
    finally:
        if host.poll() is None:
            host.terminate()
            host.wait()
    if (run / "result.json").is_file():
        print((run / "result.json").read_text(), flush=True)
    if (run / "run.jsonl").is_file() and (run / "result.json").is_file():
        with (run / "summary.json").open("w", encoding="utf-8") as summary:
            subprocess.run([sys.executable, "scripts/summary.py", str(run)], stdout=summary, check=True)
        stats = json.loads((run / "summary.json").read_text())
        print(f"Warm: {stats['warmN']} actions, {stats['appliedHz']} Hz, "
              f"frame age {stats['warm']['frameAgeTicks']} ticks.")
    print("Inputs released. Close the Minecraft window when finished.", flush=True)
    sys.exit(host.returncode)


if __name__ == "__main__":
    main()

