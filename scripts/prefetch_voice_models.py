#!/usr/bin/env python
"""Pre-download all voice-mode model assets.

Why: TudouClaw's voice mode lazy-loads VoxCPM (TTS) + FunASR (STT) on
first use, which means the user waits 1-3 minutes for downloads at the
worst possible moment (mid-conversation). This script pulls everything
ahead of time so first-call latency is just the model load (~30s), not
download.

Models pulled (~6 GB total):
  • openbmb/VoxCPM2          ~5 GB    (local TTS)
  • paraformer-zh            ~800 MB  (FunASR Chinese ASR)
  • fsmn-vad                 ~30 MB   (FunASR VAD)
  • ct-punc                  ~200 MB  (FunASR punctuation)

Cache location: respects TudouClaw's HF_HOME override
(~/.tudou_claw/hf_cache/) so the models live alongside other TudouClaw
caches and aren't duplicated in the system-wide ~/.cache/huggingface/.

Usage:
  source ~/tudou-env/bin/activate
  python scripts/prefetch_voice_models.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Match TudouClaw's HF_HOME override BEFORE any HF imports.
# (See app/__init__.py — TudouClaw points HF cache at its own dir.)
_TUDOU_HF = os.path.expanduser("~/.tudou_claw/hf_cache")
os.makedirs(_TUDOU_HF, exist_ok=True)
os.environ.setdefault("HF_HOME", _TUDOU_HF)
os.environ.setdefault("HF_HUB_CACHE", os.path.join(_TUDOU_HF, "hub"))


def _du(path: str) -> str:
    if not os.path.isdir(path):
        return "0 B"
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while total >= 1024 and i < len(units) - 1:
        total /= 1024
        i += 1
    return f"{total:.1f} {units[i]}"


def fetch_voxcpm() -> bool:
    """Pull openbmb/VoxCPM2 weights via huggingface_hub."""
    print("─" * 56)
    print("[1/4] VoxCPM2 (~5 GB)")
    print("─" * 56)
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  ✗ huggingface_hub not installed — skipping")
        return False
    target_dir = os.path.join(_TUDOU_HF, "hub")
    existing = os.path.join(target_dir,
                            "models--openbmb--VoxCPM2")
    if os.path.isdir(existing):
        print(f"  Already present at {existing}")
        print(f"  Size: {_du(existing)}")
        return True
    t0 = time.time()
    try:
        path = snapshot_download(
            repo_id="openbmb/VoxCPM2",
            cache_dir=target_dir,
        )
        print(f"  ✓ Downloaded to {path}")
        print(f"  Size: {_du(existing)}")
        print(f"  Took: {time.time() - t0:.1f}s")
        return True
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        return False


def fetch_funasr_models() -> bool:
    """Trigger FunASR's own model loader (downloads via modelscope)."""
    print("─" * 56)
    print("[2-4/4] FunASR paraformer-zh + VAD + punc (~1 GB)")
    print("─" * 56)
    try:
        from funasr import AutoModel
    except ImportError:
        print("  ✗ funasr not installed — run `pip install funasr` first")
        return False
    t0 = time.time()
    try:
        # Loading the model triggers download. We don't need to actually
        # call generate() — instantiation is enough to fetch weights.
        print("  Pulling paraformer-zh / fsmn-vad / ct-punc (modelscope)…")
        AutoModel(
            model="paraformer-zh",
            vad_model="fsmn-vad",
            punc_model="ct-punc",
            disable_update=True,
        )
        print(f"  ✓ FunASR models cached")
        print(f"  Took: {time.time() - t0:.1f}s")
        # FunASR uses modelscope cache, NOT HF cache — show its location
        msc = os.path.expanduser("~/.cache/modelscope")
        if os.path.isdir(msc):
            print(f"  Modelscope cache: {msc} ({_du(msc)})")
        return True
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        return False


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skip-tts", action="store_true",
                   help="Skip VoxCPM2 download (~5 GB)")
    p.add_argument("--skip-stt", action="store_true",
                   help="Skip FunASR models (~1 GB)")
    args = p.parse_args(argv)

    print("TudouClaw voice-model pre-fetch")
    print(f"HF_HOME = {os.environ['HF_HOME']}")
    print(f"Current HF cache size: {_du(os.environ['HF_HOME'])}")
    print()

    results = {}
    if not args.skip_tts:
        results["voxcpm"] = fetch_voxcpm()
    else:
        print("[skipped] VoxCPM2")
    print()
    if not args.skip_stt:
        results["funasr"] = fetch_funasr_models()
    else:
        print("[skipped] FunASR")
    print()

    print("─" * 56)
    print("SUMMARY")
    print("─" * 56)
    for k, ok in results.items():
        print(f"  {k:10s}: {'✓' if ok else '✗'}")
    print(f"  HF cache total: {_du(os.environ['HF_HOME'])}")
    fail = sum(1 for ok in results.values() if not ok)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
