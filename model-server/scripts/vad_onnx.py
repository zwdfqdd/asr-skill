#!/usr/bin/env python3
"""VADs — ONNX inference for voice activity detection."""

import numpy as np
import onnxruntime as ort
from pathlib import Path


class SileroVAD:
    """VAD ONNX wrapper. Detects speech segments in 16kHz mono audio."""

    def __init__(self, model_path: str, threshold: float = 0.5,
                 min_speech_ms: int = 250, min_silence_ms: int = 100,
                 speech_pad_ms: int = 30, window_size: int = 512,
                 encryption_key: bytes = None):
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self.min_silence_ms = min_silence_ms
        self.speech_pad_ms = speech_pad_ms
        self.window_size = window_size
        self.sample_rate = 16000

        # Resolve .onnx or .onnx.enc from directory
        p = Path(model_path)
        if p.is_dir():
            # Prefer encrypted
            candidates = list(p.glob("*.onnx.enc"))
            if not candidates:
                candidates = list(p.glob("*.onnx"))
            if not candidates:
                raise FileNotFoundError(f"No .onnx or .onnx.enc in {p}")
            model_path = str(candidates[0])

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3

        if str(model_path).endswith(".enc") and encryption_key:
            from model_crypto import decrypt_model_to_memory
            model_bytes = decrypt_model_to_memory(Path(model_path), encryption_key)
            self.session = ort.InferenceSession(model_bytes, sess_options=opts)
        else:
            self.session = ort.InferenceSession(model_path, sess_options=opts)
        self._reset_state()

    def _reset_state(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._sr = np.array([self.sample_rate], dtype=np.int64)

    def _predict_chunk(self, chunk: np.ndarray) -> float:
        if len(chunk) < self.window_size:
            chunk = np.pad(chunk, (0, self.window_size - len(chunk)))
        input_data = chunk[np.newaxis, :].astype(np.float32)
        inputs = {"input": input_data, "state": self._state, "sr": self._sr}
        try:
            output, new_state = self.session.run(None, inputs)
            self._state = new_state
            return float(output[0][0])
        except Exception:
            input_names = [inp.name for inp in self.session.get_inputs()]
            feed = {}
            for name in input_names:
                nl = name.lower()
                if "state" in nl:
                    feed[name] = self._state
                elif "sr" in nl:
                    feed[name] = self._sr
                else:
                    feed[name] = input_data
            outputs = self.session.run(None, feed)
            if len(outputs) >= 2:
                self._state = outputs[1]
            return float(outputs[0].flatten()[0])

    def detect(self, audio: np.ndarray) -> list:
        """Detect speech segments. Returns list of (start_sample, end_sample)."""
        self._reset_state()
        if audio is None or len(audio) == 0:
            return []
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        peak = max(abs(audio.max()), abs(audio.min()))
        if peak > 1.0:
            audio = audio / peak
        elif peak == 0.0:
            return []  # silent audio

        probs = []
        for i in range(0, len(audio), self.window_size):
            chunk = audio[i:i + self.window_size]
            if len(chunk) < self.window_size // 2:
                break
            probs.append((i, self._predict_chunk(chunk)))

        if not probs:
            return []

        min_speech = int(self.min_speech_ms * self.sample_rate / 1000)
        min_silence = int(self.min_silence_ms * self.sample_rate / 1000)
        speech_pad = int(self.speech_pad_ms * self.sample_rate / 1000)

        segments = []
        in_speech = False
        speech_start = silence_start = 0

        for pos, prob in probs:
            if prob >= self.threshold:
                if not in_speech:
                    speech_start = pos
                    in_speech = True
                silence_start = 0
            else:
                if in_speech:
                    if silence_start == 0:
                        silence_start = pos
                    elif pos - silence_start >= min_silence:
                        speech_end = silence_start + self.window_size
                        if speech_end - speech_start >= min_speech:
                            segments.append((max(0, speech_start - speech_pad),
                                             min(len(audio), speech_end + speech_pad)))
                        in_speech = False
                        silence_start = 0

        if in_speech:
            speech_end = probs[-1][0] + self.window_size
            if speech_end - speech_start >= min_speech:
                segments.append((max(0, speech_start - speech_pad),
                                 min(len(audio), speech_end + speech_pad)))

        # Merge overlapping
        if segments:
            merged = [segments[0]]
            for s, e in segments[1:]:
                if s <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))
            segments = merged

        return segments
