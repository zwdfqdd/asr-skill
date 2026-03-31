#!/usr/bin/env python3
"""ASR iStarShine V1 — installation and pipeline test."""

import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))


def find_onnx(path):
    if not path.exists():
        return None
    if path.is_file() and path.suffix == ".onnx":
        return path
    if path.is_dir():
        for c in path.glob("*.onnx"):
            if c.stem in ("model", "vad", "punc"):
                return c
        candidates = list(path.glob("*.onnx"))
        return max(candidates, key=lambda p: p.stat().st_size) if candidates else None
    return None


def check_deps():
    print("=== Dependencies ===")
    ok = True
    for mod, pkg in {"onnxruntime": "onnxruntime", "numpy": "numpy", "yaml": "pyyaml", "soundfile": "soundfile"}.items():
        try:
            m = __import__(mod)
            print(f"  [OK] {pkg} ({getattr(m, '__version__', 'ok')})")
        except ImportError:
            print(f"  [MISSING] {pkg}")
            ok = False
    return ok


def check_models():
    print("\n=== Models ===")
    import yaml
    with open(SKILL_DIR / "assets" / "asr_config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    models = config.get("models", {})
    ok = True
    for key in ("vad", "asr", "punc"):
        p = SKILL_DIR / models.get(key, "")
        onnx = find_onnx(p)
        if onnx:
            print(f"  [OK] {key}: {onnx.name} ({onnx.stat().st_size/(1024*1024):.1f}MB)")
        else:
            print(f"  [MISSING] {key}: {p}")
            ok = False
    if not ok:
        print(f"\n  Run: python {SKILL_DIR}/scripts/download_models.py")
    return ok, config


def test_vad(config):
    print("\n=== VAD Test ===")
    import numpy as np
    from vad_onnx import SileroVAD
    vad_path = SKILL_DIR / config["models"]["vad"]
    vad = SileroVAD(str(vad_path), threshold=config.get("vad", {}).get("threshold", 0.5))
    segs = vad.detect(np.zeros(32000, dtype=np.float32))
    print(f"  [OK] Silence: {len(segs)} segments (expected 0)")


def test_asr(config):
    print("\n=== ASR Test ===")
    import numpy as np
    from paraformer_onnx import ParaformerONNX
    asr = ParaformerONNX(str(SKILL_DIR / config["models"]["asr"]),
                         n_mels=config.get("features", {}).get("n_mels", 80))
    print(f"  [OK] Loaded, vocab={len(asr.vocab)}, inputs={asr.input_names}")
    text = asr.recognize(np.zeros(16000, dtype=np.float32))
    print(f"  [OK] Silence result: '{text}'")


def test_punc(config):
    print("\n=== Punc Test ===")
    from punc_onnx import PunctuationONNX
    punc = PunctuationONNX(str(SKILL_DIR / config["models"]["punc"]))
    result = punc.punctuate("你好欢迎使用语音识别系统")
    print(f"  [OK] '{result}'")


def main():
    print("ASR iStarShine V1 -- Test\n")
    if not check_deps():
        sys.exit(1)
    ok, config = check_models()
    if not ok:
        sys.exit(1)
    for test in (test_vad, test_asr, test_punc):
        try:
            test(config)
        except Exception as e:
            print(f"\n[FAIL] {e}")
            sys.exit(1)
    print("\n=== All Passed ===")


if __name__ == "__main__":
    main()
