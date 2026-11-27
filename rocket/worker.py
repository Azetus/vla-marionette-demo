"""Batch-one ROCKET-3 worker. stdin/stdout carry one JSON object per line."""

import argparse
import base64
from collections import OrderedDict
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import ModuleType


def only_submodules(name):
    """Skip eager registries for unrelated models and Gym3's GUI viewers."""
    spec = importlib.util.find_spec(name)
    module = ModuleType(name)
    module.__path__ = list(spec.submodule_search_locations)
    module.__spec__ = spec
    sys.modules[name] = module
    parent, _, leaf = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], leaf, module)


def reply(**fields):
    print(json.dumps(fields), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--goal", type=Path, required=True)
    parser.add_argument("--mask", type=Path, required=True)
    args = parser.parse_args()
    start = time.perf_counter()
    import cv2
    import numpy as np
    import torch

    only_submodules("minestudio.models")
    only_submodules("gym3")
    from rocket3.policy import BINARY_ACTION_KEYS, load_rocket3_policy
    from minestudio.utils.vpt_lib.actions import ActionTransformer
    from minestudio.utils.vpt_lib.action_mapping import CameraHierarchicalMapping

    torch.manual_seed(42)
    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    # Inference fastpath in TransformerEncoderLayer bypasses autocast on some
    # PyTorch versions. Keep the standard autocast-compatible attention path.
    torch.backends.mha.set_fastpath_enabled(False)
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        policy = load_rocket3_policy(args.checkpoint).eval().cuda()
        mapper = CameraHierarchicalMapping(n_camera_bins=11)
        transformer = ActionTransformer(camera_binsize=2, camera_maxval=10,
                                        camera_mu=10, camera_quantization_scheme="mu_law")

        def upload(array):
            return torch.from_numpy(np.ascontiguousarray(array)).cuda()[None, None]

        def rgb(bgr):
            return upload(cv2.cvtColor(cv2.resize(bgr, (224, 224)), cv2.COLOR_BGR2RGB))

        goal = cv2.imread(str(args.goal))
        mask = cv2.imread(str(args.mask), cv2.IMREAD_GRAYSCALE)
        if mask is None or goal is None or mask.shape != goal.shape[:2] or not mask.any():
            raise ValueError("goal and nonempty mask must have matching dimensions")
        # For the fixed scene, a matching trunk edge verifies the small log mask
        # still covers the reference target after window/FOV/pose setup.
        template = cv2.imread(str(args.mask.with_name("goal.jpg")), cv2.IMREAD_GRAYSCALE)
        log_mask = cv2.imread(str(args.mask.with_name("mine-mask.png")), cv2.IMREAD_GRAYSCALE)
        if template is None or log_mask is None or template.shape != mask.shape or log_mask.shape != mask.shape:
            raise ValueError("Reference template or log mask dimensions differ")
        x, y, w, h = cv2.boundingRect(log_mask)
        padding = 5
        patch = template[y-padding:y+h+padding, x-padding:x+w+padding]
        gray = cv2.cvtColor(goal, cv2.COLOR_BGR2GRAY)
        search = gray[y-padding-10:y+h+padding+10, x-padding-10:x+w+padding+10]
        scores = cv2.matchTemplate(search, patch, cv2.TM_CCOEFF_NORMED)
        _, score, _, (dx, dy) = cv2.minMaxLoc(scores)
        if score < 0.55 or max(abs(dx-10), abs(dy-10)) > 3:
            raise ValueError(f"Live tree misaligned with bundled masks: score={score:.2f}, delta={(dx-10,dy-10)}")
        cross = {"cross_view_image": rgb(goal),
                 "cross_view_obj_mask": upload(cv2.resize((mask > 0).astype(np.uint8),
                                               (224, 224), interpolation=cv2.INTER_NEAREST)),
                 "cross_view_obj_id": torch.tensor([[6]], device="cuda")}
        neutral = {k.replace("_", "."): torch.zeros((1, 1), dtype=torch.long, device="cuda")
                   for k in BINARY_ACTION_KEYS}
        neutral["camera"] = torch.zeros((1, 1, 2), device="cuda")
        history = OrderedDict()
        memory = None
        begin = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        torch.cuda.synchronize()
        reply(ready=True, loadMs=(time.perf_counter()-start)*1000, torch=torch.__version__,
              device=torch.cuda.get_device_name(), precision="BF16 autocast / FP32 weights", tf32=False, seed=42,
              parameters=sum(p.numel() for p in policy.parameters()), previousAction=policy.use_prev_action,
              viewTokens=policy.num_view_tokens)
        for line in sys.stdin:
            req = json.loads(line)
            if req.get("beginMine"):
                mine_mask = cv2.imread(str(args.mask.with_name("mine-mask.png")), cv2.IMREAD_GRAYSCALE)
                if mine_mask is None or mine_mask.shape != mask.shape or not mine_mask.any():
                    raise ValueError("missing or invalid target log mask")
                cross["cross_view_obj_mask"] = upload(cv2.resize((mine_mask > 0).astype(np.uint8),
                                                      (224, 224), interpolation=cv2.INTER_NEAREST))
                cross["cross_view_obj_id"].fill_(2)
                memory = None
                history.clear()
            image = cv2.imdecode(np.frombuffer(base64.b64decode(req["jpeg"]), np.uint8),
                                 cv2.IMREAD_COLOR)
            applied_id = req["appliedObsId"]
            obs = {"image": rgb(image), "cross_view": cross,
                   "env_prev_action": neutral if applied_id is None else history[applied_id]}
            begin.record()
            outputs, memory = policy(obs, memory)
            end.record()
            sampled = policy.pi_head.sample(outputs["pi_logits"], deterministic=False)
            ids = {k: v.cpu().numpy().reshape(1, 1) for k, v in sampled.items()}
            env = transformer.policy2env(mapper.to_factored(ids))
            history[req["obsId"]] = {k: upload(np.asarray(env[k][0], dtype=np.float32 if k == "camera" else np.int64)
                                  ).reshape_as(template) for k, template in neutral.items()}
            if len(history) > 16:
                history.popitem(last=False)
            end.synchronize()
            reply(obsId=req["obsId"], forwardMs=begin.elapsed_time(end), camera=env["camera"][0].tolist(),
                  pressed=[k for k, v in env.items() if k != "camera" and v.item()],
                  ids={k: int(v.item()) for k, v in ids.items()},
                  allocatedMiB=torch.cuda.max_memory_allocated()/2**20,
                  reservedMiB=torch.cuda.max_memory_reserved()/2**20)


if __name__ == "__main__":
    main()
