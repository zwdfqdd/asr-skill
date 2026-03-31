#!/usr/bin/env python3
"""ASR iStarShine V1 — Pure ONNX speech recognition CLI.

Pipeline: VAD → ASR → Punc.
Dependencies: onnxruntime, numpy, soundfile, pyyaml
"""

import io
import sys

# Fix Windows console encoding for Chinese output
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import yaml

SCRIPT_DIR = Path(__file__).parent
SKILL_DIR = SCRIPT_DIR.parent
CONFIG_PATH = SKILL_DIR / "assets" / "asr_config.yaml"


def load_config(config_path=None):
    p = Path(config_path) if config_path else CONFIG_PATH
    if not p.exists():
        print(f"Error: config not found at {p}", file=sys.stderr)
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_model_path(config_path: str) -> Path:
    p = Path(config_path)
    if p.is_absolute():
        return p
    return SKILL_DIR / p


def load_audio(audio_path: str, sample_rate: int = 16000) -> np.ndarray:
    """Load audio from file, convert to mono float32 at target sample rate.

    Supports:
      Audio: .wav, .mp3, .flac, .ogg, .m4a, .wma, .aac, .opus, .amr, .webm, .aiff, .ape, .wv, .spx
      Video (audio track): .mp4, .mkv, .avi, .mov, .wmv, .flv, .ts, .m4v
    Falls back to ffmpeg conversion for non-WAV formats or when soundfile fails.
    """
    audio_path = str(audio_path)
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    file_size = Path(audio_path).stat().st_size
    if file_size == 0:
        raise ValueError(f"Audio file is empty: {audio_path}")

    ext = Path(audio_path).suffix.lower()
    converted_path = None

    # Non-WAV formats (including video): convert via ffmpeg first
    if ext not in (".wav",):
        converted_path = convert_to_wav(audio_path, sample_rate)
        if converted_path != audio_path and Path(converted_path).exists():
            audio_path = converted_path
        else:
            # ffmpeg failed, try soundfile directly (it handles flac/ogg natively)
            converted_path = None

    # Try soundfile (handles wav, flac, ogg natively)
    try:
        import soundfile as sf
        data, sr = sf.read(audio_path, dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sr != sample_rate:
            data = _resample(data, sr, sample_rate)
        _cleanup_converted(converted_path, audio_path)
        return _validate_audio(data)
    except ImportError:
        pass
    except Exception as sf_err:
        # soundfile failed on this WAV, try wave module or ffmpeg fallback
        if ext == ".wav":
            try:
                data = _load_wav_stdlib(audio_path, sample_rate)
                _cleanup_converted(converted_path, audio_path)
                return _validate_audio(data)
            except Exception:
                pass
        # Last resort: ffmpeg convert and retry
        if converted_path is None:
            converted_path = convert_to_wav(audio_path, sample_rate)
            if converted_path != audio_path and Path(converted_path).exists():
                try:
                    import soundfile as sf
                    data, sr = sf.read(converted_path, dtype="float32")
                    if data.ndim > 1:
                        data = data.mean(axis=1)
                    if sr != sample_rate:
                        data = _resample(data, sr, sample_rate)
                    _cleanup_converted(converted_path, audio_path)
                    return _validate_audio(data)
                except Exception:
                    pass
        raise RuntimeError(f"Cannot load audio: {audio_path} (soundfile error: {sf_err})")

    # Fallback: stdlib wave module (WAV only)
    try:
        data = _load_wav_stdlib(audio_path, sample_rate)
        _cleanup_converted(converted_path, audio_path)
        return _validate_audio(data)
    except Exception as wave_err:
        _cleanup_converted(converted_path, audio_path)
        raise RuntimeError(f"Cannot load audio: {audio_path} (wave error: {wave_err})")


def _load_wav_stdlib(audio_path: str, sample_rate: int) -> np.ndarray:
    """Load WAV using Python's built-in wave module."""
    with wave.open(audio_path, "rb") as wf:
        sr = wf.getframerate()
        n_ch = wf.getnchannels()
        sw = wf.getsampwidth()
        n_frames = wf.getnframes()
        if n_frames == 0:
            raise ValueError("WAV file has 0 frames")
        raw = wf.readframes(n_frames)
    if sw == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 3:
        # 24-bit audio
        raw_bytes = np.frombuffer(raw, dtype=np.uint8)
        samples = np.zeros(len(raw_bytes) // 3, dtype=np.int32)
        samples = (raw_bytes[2::3].astype(np.int32) << 24 |
                   raw_bytes[1::3].astype(np.int32) << 16 |
                   raw_bytes[0::3].astype(np.int32) << 8) >> 8
        data = samples.astype(np.float32) / 8388608.0
    elif sw == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0
    else:
        raise ValueError(f"Unsupported sample width: {sw}")
    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1)
    if sr != sample_rate:
        data = _resample(data, sr, sample_rate)
    return data


def _validate_audio(data: np.ndarray) -> np.ndarray:
    """Ensure audio is valid mono float32."""
    if data is None or len(data) == 0:
        raise ValueError("Audio data is empty after loading")
    if not np.isfinite(data).all():
        data = np.nan_to_num(data, nan=0.0, posinf=1.0, neginf=-1.0)
    # Normalize if clipping
    peak = np.abs(data).max()
    if peak > 1.0:
        data = data / peak
    return data.astype(np.float32)


def _cleanup_converted(converted_path, original_path):
    """Remove temporary converted file if it exists."""
    if converted_path and converted_path != str(original_path):
        try:
            p = Path(converted_path)
            if p.exists():
                p.unlink()
        except OSError:
            pass


def _resample(data, orig_sr, target_sr):
    """Resample 1D audio array using linear interpolation."""
    if orig_sr == target_sr:
        return data
    if len(data) == 0:
        return data
    ratio = target_sr / orig_sr
    new_len = max(1, int(len(data) * ratio))
    return np.interp(
        np.linspace(0, len(data) - 1, new_len),
        np.arange(len(data)),
        data
    ).astype(np.float32)


def convert_to_wav(input_path, sample_rate=16000):
    """Convert any audio format to 16kHz mono WAV via ffmpeg.

    Returns the converted file path, or the original path if ffmpeg is unavailable.
    """
    output_path = str(Path(input_path).with_suffix(".converted.wav"))
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(input_path),
             "-ar", str(sample_rate), "-ac", "1",
             "-sample_fmt", "s16", "-f", "wav", output_path],
            capture_output=True, timeout=120
        )
        if result.returncode == 0 and Path(output_path).exists() and Path(output_path).stat().st_size > 0:
            return output_path
        # ffmpeg ran but failed
        stderr = result.stderr.decode("utf-8", errors="replace")[-200:]
        print(f"Warning: ffmpeg conversion failed: {stderr}", file=sys.stderr)
        return str(input_path)
    except FileNotFoundError:
        print("Warning: ffmpeg not found, cannot convert non-WAV audio", file=sys.stderr)
        return str(input_path)
    except subprocess.TimeoutExpired:
        print("Warning: ffmpeg conversion timed out", file=sys.stderr)
        return str(input_path)
    except Exception as e:
        print(f"Warning: ffmpeg conversion error: {e}", file=sys.stderr)
        return str(input_path)


def _find_onnx(path: Path):
    """Find .onnx or .onnx.enc model file in path."""
    if not path.exists():
        return None
    if path.is_file() and (path.suffix == ".onnx" or path.name.endswith(".onnx.enc")):
        return path
    if path.is_dir():
        # Prefer encrypted
        for pattern in ("*.onnx.enc", "*.onnx"):
            candidates = list(path.glob(pattern))
            if candidates:
                for c in candidates:
                    stem = c.name.replace(".enc", "").replace(".onnx", "")
                    if stem in ("model", "vad", "punc"):
                        return c
                return max(candidates, key=lambda p: p.stat().st_size)
    return None


class ASRPipeline:
    """Complete ASR pipeline: VAD -> ASR -> Punc."""

    def __init__(self, config: dict, encryption_key: bytes = None):
        sys.path.insert(0, str(SCRIPT_DIR))
        from vad_onnx import SileroVAD
        from paraformer_onnx import ParaformerONNX
        from punc_onnx import PunctuationONNX

        models = config.get("models", {})
        vad_cfg = config.get("vad", {})
        feat_cfg = config.get("features", {})

        vad_path = resolve_model_path(models.get("vad", ""))
        asr_path = resolve_model_path(models.get("asr", ""))
        punc_path = resolve_model_path(models.get("punc", ""))

        # Auto-load key from models dir if not provided
        if encryption_key is None:
            encryption_key = self._auto_load_key(config)

        # VAD
        vad_onnx = _find_onnx(vad_path)
        if vad_onnx:
            print("Loading VAD...", file=sys.stderr)
            self.vad = SileroVAD(str(vad_onnx),
                                 threshold=vad_cfg.get("threshold", 0.5),
                                 min_speech_ms=vad_cfg.get("min_speech_ms", 250),
                                 min_silence_ms=vad_cfg.get("min_silence_ms", 100),
                                 speech_pad_ms=vad_cfg.get("speech_pad_ms", 30),
                                 window_size=vad_cfg.get("window_size", 512),
                                 encryption_key=encryption_key)
        else:
            print(f"Warning: VAD not found at {vad_path}", file=sys.stderr)
            self.vad = None

        # ASR
        if asr_path.exists():
            print("Loading ASR...", file=sys.stderr)
            self.asr = ParaformerONNX(str(asr_path),
                                      n_mels=feat_cfg.get("n_mels", 80),
                                      frame_length_ms=feat_cfg.get("frame_length", 25),
                                      frame_shift_ms=feat_cfg.get("frame_shift", 10),
                                      encryption_key=encryption_key)
        else:
            print(f"Error: ASR model not found at {asr_path}", file=sys.stderr)
            sys.exit(1)

        # Punctuation
        if punc_path.exists():
            print("Loading Punc...", file=sys.stderr)
            self.punc = PunctuationONNX(str(punc_path), encryption_key=encryption_key)
        else:
            print(f"Warning: Punc not found at {punc_path}", file=sys.stderr)
            self.punc = None

        self.sample_rate = config.get("audio", {}).get("sample_rate", 16000)
        print("Pipeline ready.", file=sys.stderr)

    @staticmethod
    def _auto_load_key(config: dict):
        """Try to load encryption key from model.key in skill dir or config."""
        key_path = config.get("encryption", {}).get("key_file")
        if key_path:
            p = Path(key_path)
            if not p.is_absolute():
                p = SKILL_DIR / p
            if p.exists():
                return p.read_bytes()

        # Check common locations
        for candidate in (SKILL_DIR / "model.key", SKILL_DIR / "models" / "model.key"):
            if candidate.exists():
                return candidate.read_bytes()
        return None

    def transcribe(self, audio_path: str) -> dict:
        """Transcribe a single audio file. Returns dict with text, segments, duration, model, file."""
        audio_path = str(audio_path)
        try:
            audio = load_audio(audio_path, self.sample_rate)
        except Exception as e:
            raise RuntimeError(f"Failed to load audio '{Path(audio_path).name}': {e}")

        duration = round(len(audio) / self.sample_rate, 2)
        if duration < 0.1:
            return {"text": "", "segments": [], "duration": duration,
                    "model": "ASR", "file": audio_path,
                    "warning": "Audio too short (<0.1s)"}

        try:
            segments = self.vad.detect(audio) if self.vad else []
        except Exception as e:
            print(f"Warning: VAD failed, using full audio: {e}", file=sys.stderr)
            segments = []

        if not segments:
            segments = [(0, len(audio))]

        seg_results = self.asr.recognize_segments(audio, segments)
        raw_text = "".join(r["text"] for r in seg_results)

        try:
            text = self.punc.punctuate(raw_text) if self.punc and raw_text else raw_text
        except Exception as e:
            print(f"Warning: Punctuation failed, using raw text: {e}", file=sys.stderr)
            text = raw_text

        # Cleanup any temp converted file
        converted = Path(audio_path).with_suffix(".converted.wav")
        if converted.exists() and str(converted) != str(audio_path):
            try:
                converted.unlink()
            except OSError:
                pass

        return {"text": text, "segments": seg_results, "duration": duration,
                "model": "ASR", "file": str(audio_path)}


def batch_transcribe(pipeline, audio_dir, output_file=None):
    """Batch transcribe all audio files in a directory."""
    audio_dir = Path(audio_dir)
    if not audio_dir.exists():
        print(f"Error: directory not found: {audio_dir}", file=sys.stderr)
        return []
    if not audio_dir.is_dir():
        print(f"Error: not a directory: {audio_dir}", file=sys.stderr)
        return []

    exts = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".wma", ".aac",
            ".opus", ".amr", ".webm", ".aiff", ".aif", ".ape", ".wv", ".spx",
            ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m4v"}
    files = sorted(f for f in audio_dir.iterdir()
                   if f.suffix.lower() in exts and not f.name.endswith(".converted.wav"))
    if not files:
        print(f"No audio files in {audio_dir}", file=sys.stderr)
        return []

    results = []
    success = 0
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f.name}", file=sys.stderr)
        try:
            result = pipeline.transcribe(str(f))
            results.append(result)
            success += 1
        except Exception as e:
            results.append({"file": str(f), "error": str(e)})
            print(f"  Error: {e}", file=sys.stderr)

    print(f"Done: {success}/{len(files)} succeeded.", file=sys.stderr)

    if output_file:
        try:
            with open(output_file, "w", encoding="utf-8") as out:
                json.dump(results, out, ensure_ascii=False, indent=2)
            print(f"Saved to {output_file}", file=sys.stderr)
        except OSError as e:
            print(f"Error: cannot write output file: {e}", file=sys.stderr)
    return results


def main():
    parser = argparse.ArgumentParser(description="ASR iStarShine V1 - Pure ONNX")
    sub = parser.add_subparsers(dest="command")

    p_t = sub.add_parser("transcribe", help="Transcribe an audio file")
    p_t.add_argument("audio")
    p_t.add_argument("--json", action="store_true")

    p_b = sub.add_parser("batch", help="Batch transcribe a directory")
    p_b.add_argument("directory")
    p_b.add_argument("--output", "-o", default=None)

    p_s = sub.add_parser("server", help="Start WebSocket server")
    p_s.add_argument("--host", default=None)
    p_s.add_argument("--port", type=int, default=None)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    config = load_config()

    if args.command == "transcribe":
        if not Path(args.audio).exists():
            print(f"Error: {args.audio} not found", file=sys.stderr)
            sys.exit(1)
        try:
            result = ASRPipeline(config).transcribe(args.audio)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["text"])
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "batch":
        if not Path(args.directory).is_dir():
            print(f"Error: {args.directory} is not a directory", file=sys.stderr)
            sys.exit(1)
        results = batch_transcribe(ASRPipeline(config), args.directory, args.output)
        if not args.output:
            print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.command == "server":
        from asr_server import start_server
        start_server(config, args.host, args.port)


if __name__ == "__main__":
    main()
