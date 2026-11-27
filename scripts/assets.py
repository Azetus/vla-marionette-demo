"""Fetch the three pinned upstream files and check their SHA-256 hashes."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from urllib.request import urlopen

from huggingface_hub import hf_hub_download

CACHE = Path(".cache").resolve()
MOD = "vla-marionette-26.1.2-neoforge-0.1.1.jar"
REV = "91a3bd12115befb210158d37ab302265deff4f5d"
SOURCE = "ddc91e4f3f62d51a9227d95c93a631294926a61c"
MOD_HASH = "c64014349a70d6d2f925f336e37d485e36df39d29f15db05f1244cf9a9c5e85e"
WEIGHT_HASH = "e484da8c417f5fa18767e669222575d8c61ee135e022de3953cad12a226dc259"
SOURCE_HASH = "4a9200f0d2c083b974d2f881b059fa6b29ec56776767e508cdf6912301080516"


def verify(path, expected):
    with Path(path).open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch for {path}: {actual}")
    return Path(path).resolve()


def download(url, path, expected):
    if path.is_file():
        return verify(path, expected)
    print(f"Downloading {url}", flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        staging = Path(tmp.name)
    try:
        with urlopen(url, timeout=120) as remote, staging.open("wb") as dest:
            shutil.copyfileobj(remote, dest)
        verify(staging, expected)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)
    return path


def prepare(config):
    if config.get("modJar"):
        jar = verify(config["modJar"], MOD_HASH)
    else:
        jar = download(f"https://github.com/Azetus/vla-marionette/releases/download/v0.1.1/{MOD}",
                       CACHE / MOD, MOD_HASH)
    entry = download(f"https://raw.githubusercontent.com/CraftJarvis/ROCKET-3/{SOURCE}/rocket3/policy.py",
                     CACHE / SOURCE / "rocket3" / "policy.py", SOURCE_HASH)
    checkpoint = config.get("checkpoint")
    if not checkpoint:
        print("Resolving ROCKET-3-1.5x (750 MB on first run)...", flush=True)
        checkpoint = hf_hub_download("CraftJarvis/ROCKET-3-1.5x", "rocket3.ckpt",
                                     revision=REV, cache_dir=CACHE / "hub")
    return jar, verify(checkpoint, WEIGHT_HASH), entry.parents[1]

