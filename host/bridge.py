"""Async loopback transport for the frozen vla-marionette protocol."""
import asyncio
from dataclasses import dataclass
import json
import struct
import time


def now_ms():
    return time.perf_counter_ns() / 1_000_000


@dataclass
class Observation:
    obs_id: int
    tick: int
    arrival: float
    jpeg: bytes


def camel_fields(fields):
    result = {}
    for key, value in fields.items():
        head, *tail = key.split("_")
        result[head + "".join(part.title() for part in tail)] = value
    return result


class Bridge:
    def __init__(self, log, on_receipt):
        self.log = log
        self.on_receipt = on_receipt
        self.state = {}
        self.frame = self.session = self.writer = self.error = None
        self.changed = asyncio.Event()
        self.requests = {}
        self.request_id = 0

    async def accept(self, reader, writer):
        if self.writer is not None:
            writer.close()
            return
        self.writer = writer
        try:
            while True:
                size, header_size = struct.unpack(">II", await reader.readexactly(8))
                if not 0 < header_size <= size - 4 or size > 32 * 1024**2:
                    raise ValueError("Invalid bridge frame lengths")
                header = json.loads(await reader.readexactly(header_size))
                body = await reader.readexactly(size - 4 - header_size)
                self.dispatch(header, body, now_ms())
                self.changed.set()
        except (Exception, asyncio.CancelledError) as exc:
            self.error = exc
            self.changed.set()
            for reply in self.requests.values():
                if not reply.done():
                    reply.set_exception(ConnectionError("Bridge reader stopped"))
        finally:
            writer.close()

    def dispatch(self, header, body, at):
        kind = header["type"]
        if kind == "SessionInfo":
            image = header["image"]
            capabilities = {"observation", "state", "raw_input", "chat", "query_block", "query_settings", "set_hud"}
            if (self.session or header["mc_version"] != "26.1.2" or
                    (image["width"], image["height"], image["pixel_format"]) != (854, 480, "jpeg") or
                    not capabilities.issubset(header["capabilities"]) or header["limits"]["max_action_frames"] < 1):
                raise ValueError("Incompatible or duplicate bridge session")
            self.session = header
            self.log(kind, **{k:v for k,v in header.items() if k != "type"})
        elif kind == "State":
            self.state = camel_fields({k:v for k,v in header.items() if k not in ("type", "session_id")})
            self.state["capture"] = camel_fields(self.state.get("capture", {}))
            self.log("state", at=at, **self.state)
        elif kind == "Observation" and body:
            self.frame = Observation(header["obs_id"], header["state_tick"], at, body)
            self.log("rgb", at=at, obsId=self.frame.obs_id, tick=self.frame.tick,
                     submitMs=header["submit_ns"]/1e6, bytes=len(body))
        elif kind == "ActionReceipt":
            self.log("receipt", at=at, **{k:header[k] for k in ("seq","status","reason","tick") if k in header})
            self.on_receipt(header, at)
        elif header.get("request_id") in self.requests:
            reply = self.requests[header["request_id"]]
            if not reply.done():
                reply.set_result(header)
        else:
            raise ValueError(f"Unexpected bridge message: {kind}")

    def send(self, kind, **fields):
        if self.error is not None:
            raise ConnectionError("Bridge connection failed") from self.error
        header = json.dumps(dict(type=kind, **fields), separators=(",", ":")).encode()
        self.writer.write(struct.pack(">II", len(header) + 4, len(header)) + header)

    async def request(self, kind, *, allow_miss=False, **fields):
        self.request_id += 1
        key = self.request_id
        reply = self.requests[key] = asyncio.get_running_loop().create_future()
        try:
            self.send(kind, request_id=key, **fields)
            response = await asyncio.wait_for(reply, 5)
            if not response.get("ok") and not (allow_miss and response.get("reason") == "no_target"):
                raise RuntimeError(f"{kind} failed: {response}")
            return response
        finally:
            self.requests.pop(key)

    async def until(self, predicate, timeout=5):
        # The reader updates all state and receipts on this event loop.
        async with asyncio.timeout(timeout):
            while True:
                if self.error is not None:
                    raise ConnectionError("Bridge reader failed") from self.error
                if value := predicate():
                    return value
                self.changed.clear()
                await self.changed.wait()

    def controllable(self):
        s = self.state
        return (s.get("alive") and s["inWorld"] and s["inputLocked"]
                and (not s["screenOpen"] or s["screen"] == "InventoryScreen"))

    def playable(self):
        return self.controllable() and not self.state["screenOpen"]

    async def release(self):
        if self.writer and not self.writer.is_closing():
            self.send("Release")
            await self.writer.drain()
