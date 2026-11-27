"""Summarize a complete rocket3-live run with nearest-rank percentiles."""

import collections
import json
from itertools import groupby
import math
from pathlib import Path
import sys


def dist(values):
    values = sorted(values)
    return {key: round(values[math.ceil(len(values) * p) - 1], 3)
            for key, p in [("p50", .5), ("p95", .95)]}


def hz(times):
    return round(1000 * (len(times) - 1) / (times[-1] - times[0]), 3)


def input_coverage(steps, states):
    ticks = [s["appliedTick"] for s in steps]
    elapsed = ticks[-1] - ticks[0] + 1
    gaps = [b-a-1 for a, b in zip(ticks, ticks[1:])]
    window = [states.get(t) for t in range(ticks[0], ticks[-1]+1)]
    held_runs = [sum(1 for _ in group) for held, group in groupby(
        window, key=lambda s: bool(s and 0 in s["held"]["buttons"])) if held]
    return dict(actions=len(steps), elapsedTicks=elapsed, uncoveredTicks=elapsed-len(steps),
                gapHistogram=dict(collections.Counter(gaps)),
                hz=hz([s["appliedAt"] for s in steps]) if len(steps)>1 else None,
                requestedAttack=sum("attack" in s["pressed"] for s in steps),
                maxContinuousLeftButtonTicks=max(held_runs, default=0),
                missingStates=window.count(None),
                heldMismatches=sum(1 for s in steps if s["appliedTick"] in states and
                    any(sorted(states[s["appliedTick"]]["held"][k]) != sorted(s["input"][k])
                        for k in ("keys", "buttons"))))


def main(directory):
    rows = [json.loads(line) for line in (directory / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    steps = [r for r in rows if r["type"] == "step"]
    warm = [s for s in steps if s["phase"] == "warm"]
    receipts = [r for r in rows if r["type"] == "receipt"]
    states = {r["tick"]: r for r in rows if r["type"] == "state"}
    rgb = {r["obsId"]: r for r in rows if r["type"] == "rgb"}
    for s in steps:
        applied = [r for r in receipts if r["seq"] == s["seq"] and r["status"] == "applied"]
        assert len(applied) == 1 and applied[0]["tick"] == s["appliedTick"]
    assert len({s["frameTick"] for s in steps}) == len({s["obsId"] for s in steps}) == len(steps)
    assert all(s["appliedTick"] < t["appliedTick"] for s, t in zip(steps, steps[1:]))
    arrivals = [r for r in rgb.values() if warm[0]["arrival"] <= r["at"] <= warm[-1]["arrival"]]
    applied_times = [s["appliedAt"] for s in warm]
    seconds = int((applied_times[-1] - applied_times[0]) / 1000)
    per_second = [sum(i <= (t - applied_times[0]) / 1000 < i+1 for t in applied_times) for i in range(seconds)]

    def timings(s):
        return dict(forwardMs=s["forwardMs"], hostToSendMs=s["sent"]-s["arrival"],
                    frameAgeTicks=s["appliedTick"]-s["frameTick"],
                    captureToHostMs=s["arrival"]-rgb[s["obsId"]]["submitMs"],
                    sendToAppliedReceiptMs=s["appliedAt"]-s["sent"],
                    captureToAppliedReceiptMs=s["appliedAt"]-rgb[s["obsId"]]["submitMs"],
                    hostOtherMs=s["sent"]-s["arrival"]-s["forwardMs"])

    print(json.dumps(dict(
        worker=next(r for r in rows if r["type"] == "worker"),
        cold={k: round(v, 3) for k, v in timings(steps[0]).items()},
        warmN=len(warm), warm={k: dist([timings(s)[k] for s in warm]) for k in timings(steps[0])},
        warmCoverage=input_coverage(warm, states),
        stages={name:input_coverage(list(group), states)
                for name, group in groupby(steps, key=lambda s:s.get("stage", "Approach"))},
        warmAgeHistogram=dict(collections.Counter(s["appliedTick"]-s["frameTick"] for s in warm)),
        rgbN=len(arrivals), rgbHz=hz([r["at"] for r in arrivals]),
        rgbTickGaps=dict(collections.Counter(b["tick"]-a["tick"] for a,b in zip(arrivals,arrivals[1:]))),
        appliedHz=hz(applied_times), perFullSecond=per_second,
        perFullSecondDist=dist(per_second) if seconds else None,
        peakAllocatedMiB=max(s["allocatedMiB"] for s in steps),
        peakReservedMiB=max(s["reservedMiB"] for s in steps),
        receipts=dict(collections.Counter(r["status"] for r in receipts)),
        final=next(r["state"] for r in rows if r["type"] == "final")
    ), indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
