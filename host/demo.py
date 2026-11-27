"""Run one real-time ROCKET-3 trial with independent bridge and inference I/O."""
import asyncio
import base64
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import sys

from bridge import Bridge, camel_fields, now_ms
from scene import AIR, LOG, TRUNK, build_scene, to_frame


class Trial:
    def __init__(self, directory):
        self.out = directory
        self.rows = []
        self.link = Bridge(self.log, self.receipt)
        self.worker = self.poll = None
        self.stage = "Approach"
        self.stage_step = self.count = self.seq = 0
        self.last_tick = self.last_start = -1
        self.pending = None
        self.in_flight = {}
        self.acks = {}
        self.applied_frames = {}
        self.reached = self.mined = False

    def log(self, kind, **fields):
        self.rows.append(dict(type=kind, **fields))

    def receipt(self, r, at):
        seq, status = r["seq"], r["status"]
        step = self.in_flight[seq]
        if status in ("received", "rejected"):
            self.acks[seq] = r
        elif status == "applied" and r["tick"] == step["startTick"]:
            step.update(appliedAt=at, appliedTick=r["tick"])
            self.applied_frames[r["tick"]] = step["obsId"]
            self.pending = None
        elif status == "ended" and r["tick"] == step["appliedTick"] + 1:
            self.log("step", **step, endedTick=r["tick"], state=self.link.state)
            del self.in_flight[seq]
        else:
            raise RuntimeError(f"Unexpected receipt: {r}")

    async def inspect_trunk(self):
        blocks = await asyncio.gather(*(self.link.request("QueryBlock", pos=p) for p in TRUNK))
        self.log("trunk", at=now_ms(), blocks=blocks)
        return [b["block_state"] for b in blocks]

    async def check_mining(self):
        blocks, crosshair = await asyncio.gather(self.inspect_trunk(), self.link.request("QueryBlock", allow_miss=True))
        self.log("crosshair", reply=crosshair)
        self.mined = AIR in blocks

    async def drain_and_release(self):
        await self.link.until(lambda: not self.in_flight)
        await self.link.release()
        def released():
            s = self.link.state
            return s["tick"] > self.last_start and not s["held"]["keys"] and not s["held"]["buttons"]
        await self.link.until(released)

    async def read_worker(self, timeout):
        line = await asyncio.wait_for(self.worker.stdout.readline(), timeout)
        if not line:
            raise RuntimeError(f"ROCKET-3 worker exited ({await self.worker.wait()})")
        return json.loads(line)

    async def submit(self, frame, action, input_frame, applied_id):
        link = self.link
        for attempt in range(3):
            if not link.controllable():
                raise RuntimeError("Lost control before action submission")
            self.seq += 1
            seq = self.seq
            start = max(frame.tick + 2, link.state["tick"] + 1 + attempt, link.frame.tick + 1, self.last_start + 1)
            phase = "cold" if self.count == 0 else "warmup" if self.count <= 10 else "warm"
            step = dict(i=self.count, phase=phase, stage=self.stage, stageStep=self.stage_step, seq=seq,
                        frameTick=frame.tick, arrival=frame.arrival, appliedObsId=applied_id,
                        startTick=start, input=input_frame, **action)
            self.pending = seq
            self.in_flight[seq] = step
            link.send("ActionChunk", seq=seq, start_tick=start, frames=[input_frame])
            step["sent"] = now_ms()
            ack = await link.until(lambda: self.acks.pop(seq, None))
            if ack["status"] == "received":
                self.last_start = start
                return
            if ack.get("reason") != "late":
                raise RuntimeError(f"Action rejected: {ack}")
            self.pending = None
            del self.in_flight[seq]
        raise RuntimeError("Repeated late scheduling")

    async def run(self):
        link = self.link
        await link.until(lambda: link.session, timeout=120)
        await link.until(link.playable, timeout=30)
        print("Connected. Preparing the single-tree arena...")
        link.send("SetHud", visible=True)
        await build_scene(link)
        await link.until(lambda: link.playable() and link.frame and link.frame.tick >= link.state["tick"] - 1)
        (self.out / "goal.jpg").write_bytes(link.frame.jpeg)
        settings = await link.request("QuerySettings")
        if (settings["smooth_camera"] or settings["scoping"] or settings["fov_mode"] != "fixed" or
                settings["hide_gui"] or settings["camera_type"] != "first_person" or settings["gamemode"] != "survival"):
            raise ValueError("Unsupported game settings")
        self.log("settings", **camel_fields({k:v for k,v in settings.items() if k != "type"}))
        if await self.inspect_trunk() != [LOG] * 4:
            raise ValueError("Incomplete right tree")
        self.log("trialStart", at=now_ms(), state=link.state)
        print("Loading ROCKET-3. Watch the tree on the right.")
        started = now_ms()
        self.worker = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "rocket/worker.py", "--checkpoint", os.environ["ROCKET_CKPT"],
            "--goal", str(self.out / "goal.jpg"), "--mask", "assets/mask.png",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        ready = await self.read_worker(120)
        if not ready.get("ready"):
            raise RuntimeError(f"Worker failed to initialize: {ready}")
        self.log("worker", **ready, processToReadyMs=now_ms() - started)
        print("Approach: predicting actions on", ready["device"])
        while True:
            s = link.state
            if self.stage == "Approach" and link.playable() and math.hypot(s["x"] + 5.5, s["z"] - 14.5) <= 3:
                self.reached = True
                self.log("arrival", at=now_ms(), state=s)
                await self.drain_and_release()
                if await self.inspect_trunk() != [LOG] * 4:
                    raise RuntimeError("Trunk changed before mining")
                self.stage = "Mine"
                self.stage_step = 0
                self.applied_frames.clear()
                (self.out / "arrival.jpg").write_bytes(link.frame.jpeg)
                self.log("mineStart", at=now_ms(), state=link.state)
                print("At the right tree. Mining the marked log.")
            if self.poll and self.poll.done():
                await self.poll
                self.poll = None
            if self.mined or self.stage_step >= 1200:
                break
            frame = await link.until(lambda: link.frame if link.frame.tick > self.last_tick else None)
            if not link.controllable():
                raise RuntimeError(f"Game no longer controllable: {link.state}")
            self.last_tick = frame.tick
            applied_id = self.applied_frames.get(frame.tick)
            request = dict(obsId=frame.obs_id, jpeg=base64.b64encode(frame.jpeg).decode("ascii"),
                           appliedObsId=applied_id, beginMine=self.stage == "Mine" and self.stage_step == 0)
            self.worker.stdin.write(json.dumps(request).encode() + b"\n")
            await self.worker.stdin.drain()
            action = await self.read_worker(30)
            if action["obsId"] != frame.obs_id:
                raise RuntimeError("Mismatched inference response")
            # Start inference immediately. Only sending waits for the scheduled slot.
            input_frame = to_frame(action, settings)
            await link.until(lambda: self.pending is None)
            if self.mined:
                break
            await self.submit(frame, action, input_frame, applied_id)
            if self.stage == "Mine" and self.stage_step % 4 == 0 and self.poll is None:
                self.poll = asyncio.create_task(self.check_mining())
            self.stage_step += 1
            self.count += 1

        await self.drain_and_release()
        if self.poll:
            await self.poll
        await asyncio.sleep(0.25)
        blocks = await self.inspect_trunk()
        success = self.reached and blocks == [LOG, AIR, LOG, LOG]
        result = dict(scene="single-oak", success=success, reached=self.reached, target=TRUNK[1],
                      blocks=blocks, stage=self.stage, actions=self.count)
        (self.out / "result.json").write_text(json.dumps(result, indent=2))
        (self.out / "final.jpg").write_bytes(link.frame.jpeg)
        self.log("mineResult", **result)
        self.log("final", state=link.state)
        print("PASS: target log mined." if success else "FAIL: target or stage deadline missed.")
        return 0 if success else 2

    async def close(self):
        if self.poll:
            self.poll.cancel()
            await asyncio.gather(self.poll, return_exceptions=True)
        with suppress(ConnectionError, OSError):
            await self.link.release()
        if self.link.writer:
            self.link.writer.close()
        if self.worker:
            self.worker.stdin.close()
            if self.worker.returncode is None:
                with suppress(ProcessLookupError):
                    self.worker.kill()
            await self.worker.wait()


async def main(directory, port):
    trial = Trial(directory)
    with (directory / "run.jsonl").open("x", encoding="utf-8") as log:
        async with await asyncio.start_server(trial.link.accept, "127.0.0.1", port):
            try:
                return await trial.run()
            except Exception as exc:
                trial.log("error", message=str(exc))
                raise
            finally:
                try:
                    await trial.close()
                finally:
                    for row in trial.rows:
                        log.write(json.dumps(row, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    sys.exit(asyncio.run(main(Path(sys.argv[1]), int(sys.argv[2]))))
