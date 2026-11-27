"""Single-oak arena, start pose and live-binding action conversion."""
import asyncio
import math

TRUNK = [(-6, y, 14) for y in (100, 101, 102, 103)]
LOG = "minecraft:oak_log"
AIR = "minecraft:air"


async def build_scene(link):
    async def command(text):
        await link.request("Chat", text="/" + text)
        await asyncio.sleep(0.12)

    await command("gamemode spectator @s")
    await command("tp @s 0.5 110 0.5 0 0")
    await link.until(lambda: abs(link.state["y"] - 110) < 0.1)
    await asyncio.sleep(2)
    for text in (
        "gamerule send_command_feedback false", "difficulty peaceful", "gamerule spawn_mobs false",
        "gamerule advance_time false", "gamerule advance_weather false", "time set noon", "weather clear",
        "fill -20 100 -20 20 110 20 minecraft:air", "fill -20 99 -20 20 99 20 minecraft:stone",
        "fill -20 100 -20 20 101 -20 minecraft:stone", "fill -20 100 20 20 101 20 minecraft:stone",
        "fill -20 100 -20 -20 101 20 minecraft:stone", "fill 20 100 -20 20 101 20 minecraft:stone",
        "clear @s", "effect clear @s", "fill -6 100 14 -6 103 14 minecraft:oak_log",
        "fill -8 104 12 -4 106 16 minecraft:oak_leaves[persistent=true]",
    ):
        await command(text)
    for pos, expected in [((0,99,0), "minecraft:stone"), ((6,100,14), AIR), ((6,104,14), AIR),
                          *((pos, LOG) for pos in TRUNK)]:
        block = await link.request("QueryBlock", pos=pos)
        if block["block_state"] != expected:
            raise ValueError(f"Scene verification failed at {pos}. Enable commands in the test world.")
    await command("tp @s 0.5 100 0.5 0 0")
    await command("gamemode survival @s")

    def at_start():
        s = link.state
        return (s["alive"] and math.dist((s["x"], s["y"], s["z"]), (0.5,100,0.5)) < 0.05
                and abs(s["yaw"]) < 0.1 and abs(s["pitch"]) < 0.1)

    await link.until(at_start)
    await asyncio.sleep(12)  # Let chat and the spectator HUD fade.


def to_frame(action, settings):
    keys, buttons = [], []
    for name in action["pressed"]:
        binding = settings["keybinds"]["key." + name]
        code = binding["code"]
        if type(code) is not int:
            raise ValueError(f"Invalid code for {name}: {code}")
        if binding["type"] == "mouse" and 0 <= code <= 7:
            target = buttons
        elif binding["type"] == "keysym" and 32 <= code <= 348 and code not in (256, 300):
            target = keys
        else:
            raise ValueError(f"Unsupported binding for {name}: {binding}")
        if code not in target:
            target.append(code)
    pitch, yaw = action["camera"]
    degrees_per_pixel = 1.2 * (settings["sensitivity"] * 0.6 + 0.2) ** 3
    return dict(keys=keys, buttons=buttons, mouse=dict(
        dx=yaw / degrees_per_pixel * (-1 if settings.get("invert_mouse_x") else 1),
        dy=pitch / degrees_per_pixel * (-1 if settings.get("invert_mouse_y") else 1), scroll=0))
