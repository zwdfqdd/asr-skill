---
name: asr_istarshine_v1
description: "Local offline Chinese speech-to-text using pure ONNX Runtime inference. Three-stage pipeline: voice activity detection → speech recognition → punctuation restoration. Minimal dependencies (onnxruntime + numpy), cross-platform (Windows/macOS/Linux). Use when: user asks to transcribe Chinese audio, convert speech to text, do ASR, voice recognition. Triggers: transcribe, speech to text. 语音识别, 转录, 语音转文字."
---

# ASR iStarShine V1

Pure ONNX offline Chinese ASR. Pipeline:

```
Audio → [VAD] → Speech Segments → [ASR] → Raw Text → [Punc] → Punctuated Text
```

## Prerequisites

- Python 3.8+
- `onnxruntime`, `numpy`, `pyyaml`, `soundfile`
- `ffmpeg` (recommended, required for non-WAV/FLAC/OGG formats)
- Models downloaded to `models/` (see `references/MODELS.md`)

## Supported Audio Formats

- Native (no ffmpeg needed): `.wav`, `.flac`, `.ogg`
- Via ffmpeg: `.mp3`, `.m4a`, `.wma`, `.aac`, `.opus`, `.amr`, `.webm`, `.aiff`, `.aif`, `.ape`, `.wv`, `.spx`
- Video (audio track extraction via ffmpeg): `.mp4`, `.mkv`, `.avi`, `.mov`, `.wmv`, `.flv`, `.ts`, `.m4v`
- Auto-handles: multi-channel → mono, any sample rate → 16kHz, 8/16/24/32-bit PCM

## Setup

```bash
pip install onnxruntime numpy pyyaml soundfile websockets cryptography
python <skill_dir>/scripts/download_models.py
```

Or use the install script: `bash <skill_dir>/scripts/install.sh`

Requires a valid `license.json` in the skill directory (see `references/MODELS.md`).

## Configuration

Copy `assets/asr_config.yaml` to your working directory or let scripts use the default.

Key settings:
- `models.vad` — VAD model directory (models/vad)
- `models.asr` — Paraformer model directory (models/asr)
- `models.punc` — Punctuation model directory (models/punc)
- `vad.threshold` — VAD threshold (0.0-1.0, default 0.5)

## Usage

```bash
# Transcribe a file (text output with punctuation)
python <skill_dir>/scripts/asr_tools.py transcribe /path/to/audio.wav

# JSON output (with timestamps)
python <skill_dir>/scripts/asr_tools.py transcribe /path/to/audio.wav --json

# Batch transcribe
python <skill_dir>/scripts/asr_tools.py batch /path/to/audio_dir --output results.json

# WebSocket server
python <skill_dir>/scripts/asr_tools.py server
```

## Agent Integration

1. Check models exist: `ls <skill_dir>/models/{vad,asr,punc}/*.onnx`
2. If missing, run `python <skill_dir>/scripts/download_models.py`
3. Run `python <skill_dir>/scripts/asr_tools.py transcribe <file>`
4. Return the `text` field from output
5. For batch: `python <skill_dir>/scripts/asr_tools.py batch <dir> --output results.json`
6. Errors are non-fatal in batch mode — failed files get `"error"` field in results

## Error Handling

- Empty/missing files: raises clear error with filename
- Short audio (<0.1s): returns empty text with `"warning"` field
- Multi-channel audio: auto-mixed to mono before processing
- Non-16kHz audio: auto-resampled via interpolation
- VAD failure: falls back to full-audio recognition
- Punctuation failure: returns raw unpunctuated text
- ffmpeg unavailable: falls back to soundfile/wave for natively supported formats
- Corrupt audio: caught per-file in batch mode, does not stop the batch

## Output Format

```json
{
  "text": "带标点的识别文本。",
  "segments": [{"start": 0.32, "end": 2.56, "text": "带标点的识别文本。"}],
  "duration": 3.2,
  "model": "onnx"
}
```

## References

- `references/HANDBOOK.md` — Detailed usage guide and Python API
- `references/MODELS.md` — Model download and file structure
- `references/AUDIO_PROCESSING.md` — Audio format conversion
- `references/PERFORMANCE.md` — Performance tuning and benchmarks

## Model Protection

Models are protected by session-based auth + client-side encryption.

### Download (default: encrypted + machine-bound)

```bash
# Default: session auth → download → in-memory encrypt → write .enc
python <skill_dir>/scripts/download_models.py

# Dev mode: plaintext (not recommended)
python <skill_dir>/scripts/download_models.py --no-encrypt
```

### How it works

- Auth: license key + machine fingerprint → ephemeral session token (30min TTL)
- Download: server provides file manifest with SHA-256 checksums
- Encrypt: model bytes stay in memory, AES-256-GCM encrypted before writing `.onnx.enc`
- Runtime: `.onnx.enc` decrypted to memory, loaded via `ort.InferenceSession(bytes)`
- Key: stored in `models/model.key`, auto-generated on first download
- Machine bind: key derivation mixes hardware fingerprint, model is useless on other machines
- Backward compatible: if no `.enc` files found, loads plain `.onnx` as before
