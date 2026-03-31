#!/usr/bin/env python3
"""ONNX inference for speech recognition."""

import json
import math
import re
from pathlib import Path

import numpy as np
import onnxruntime as ort


class ParaformerONNX:
    """Speech-to-text on 16kHz mono audio."""

    def __init__(self, model_dir: str, n_mels: int = 80,
                 frame_length_ms: int = 25, frame_shift_ms: int = 10,
                 encryption_key: bytes = None):
        model_dir = Path(model_dir)
        onnx_path = self._find_onnx(model_dir)
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 2
        opts.intra_op_num_threads = 4
        opts.log_severity_level = 3

        # Support encrypted models (.onnx.enc)
        if str(onnx_path).endswith(".enc") and encryption_key:
            from model_crypto import decrypt_model_to_memory
            model_bytes = decrypt_model_to_memory(onnx_path, encryption_key)
            self.session = ort.InferenceSession(model_bytes, sess_options=opts)
        else:
            self.session = ort.InferenceSession(str(onnx_path), sess_options=opts)

        self.vocab = self._load_vocab(model_dir)
        if isinstance(self.vocab, list):
            self.id2token = {i: tok for i, tok in enumerate(self.vocab)}
            self.vocab = {tok: i for i, tok in enumerate(self.vocab)}
        else:
            self.id2token = {v: k for k, v in self.vocab.items()} if self.vocab else {}

        self.n_mels = n_mels
        self.sample_rate = 16000
        self.frame_length = int(self.sample_rate * frame_length_ms / 1000)
        self.frame_shift = int(self.sample_rate * frame_shift_ms / 1000)
        # Detect LFR (Low Frame Rate) from model input shape
        expected_dim = self.session.get_inputs()[0].shape[-1]
        if isinstance(expected_dim, int) and expected_dim > n_mels:
            self.lfr_m = expected_dim // n_mels  # stack window (e.g. 7)
            self.lfr_n = self.lfr_m - 1          # skip (e.g. 6)
        else:
            self.lfr_m = 1
            self.lfr_n = 1
        self.cmvn_mean, self.cmvn_istd = self._load_cmvn(model_dir)
        self.input_names = [inp.name for inp in self.session.get_inputs()]
        self.output_names = [out.name for out in self.session.get_outputs()]

    @staticmethod
    def _find_onnx(model_dir: Path) -> Path:
        # Prefer encrypted models, fall back to plain
        for suffix in ("*.onnx.enc", "*.onnx"):
            candidates = list(model_dir.glob(suffix))
            if candidates:
                for c in candidates:
                    stem = c.name.replace(".enc", "").replace(".onnx", "")
                    if stem in ("model", "paraformer"):
                        return c
                return max(candidates, key=lambda p: p.stat().st_size)
        raise FileNotFoundError(f"No .onnx or .onnx.enc in {model_dir}")

    @staticmethod
    def _load_vocab(model_dir: Path) -> dict:
        for name in ("tokens.json", "vocab.json"):
            path = model_dir / name
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        for name in ("tokens.txt", "vocab.txt"):
            path = model_dir / name
            if path.exists():
                vocab = {}
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            vocab[parts[0]] = int(parts[1])
                        elif len(parts) == 1:
                            vocab[parts[0]] = len(vocab)
                return vocab
        return {}

    @staticmethod
    def _load_cmvn(model_dir: Path):
        # Try cmvn.json first
        json_path = model_dir / "cmvn.json"
        if json_path.exists():
            try:
                with open(json_path, "r") as f:
                    data = json.load(f)
                mean = np.array(data.get("mean", []), dtype=np.float32)
                istd = np.array(data.get("istd", data.get("inv_stddev", [])), dtype=np.float32)
                return mean if len(mean) else None, istd if len(istd) else None
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                print(f"Warning: failed to parse cmvn.json: {e}", flush=True)

        # Try Kaldi-format am.mvn
        mvn_path = model_dir / "am.mvn"
        if mvn_path.exists():
            try:
                content = mvn_path.read_text(encoding="utf-8")
                blocks = re.findall(r'\[([^\]]+)\]', content)
                vectors = []
                for block in blocks:
                    vals = block.strip().split()
                    try:
                        vectors.append(np.array([float(v) for v in vals], dtype=np.float32))
                    except ValueError:
                        continue
                shifts = [v for v in vectors if len(v) > 10]
                if len(shifts) >= 2:
                    return shifts[0], shifts[1]
                elif len(shifts) == 1:
                    return shifts[0], None
            except Exception as e:
                print(f"Warning: failed to parse am.mvn: {e}", flush=True)
        return None, None

    def _compute_fbank(self, audio: np.ndarray) -> np.ndarray:
        emphasized = np.append(audio[0], audio[1:] - 0.97 * audio[:-1])
        num_frames = max(1, 1 + (len(emphasized) - self.frame_length) // self.frame_shift)
        frames = np.zeros((num_frames, self.frame_length), dtype=np.float32)
        for i in range(num_frames):
            start = i * self.frame_shift
            end = start + self.frame_length
            if end <= len(emphasized):
                frames[i] = emphasized[start:end]
            else:
                frames[i, :len(emphasized) - start] = emphasized[start:]

        window = np.hamming(self.frame_length).astype(np.float32)
        frames *= window
        nfft = 512
        power = np.abs(np.fft.rfft(frames, n=nfft)) ** 2
        mel_filters = self._mel_filterbank(nfft, self.n_mels, self.sample_rate)
        log_mel = np.log(np.maximum(np.dot(power, mel_filters.T), 1e-10))

        # Apply LFR (Low Frame Rate) stacking if needed
        if self.lfr_m > 1:
            log_mel = self._apply_lfr(log_mel, self.lfr_m, self.lfr_n)

        # Apply CMVN (after LFR so dimensions match 560-dim am.mvn)
        feat_dim = log_mel.shape[-1]
        if self.cmvn_mean is not None and len(self.cmvn_mean) == feat_dim:
            log_mel = log_mel + self.cmvn_mean  # am.mvn AddShift is added
        if self.cmvn_istd is not None and len(self.cmvn_istd) == feat_dim:
            log_mel = log_mel * self.cmvn_istd
        return log_mel.astype(np.float32)

    @staticmethod
    def _apply_lfr(feats: np.ndarray, lfr_m: int, lfr_n: int) -> np.ndarray:
        """Stack lfr_m consecutive frames, advance by lfr_n frames."""
        T, D = feats.shape
        T_lfr = math.ceil(T / lfr_n) if lfr_n > 0 else T
        # Pad end so we always have lfr_m frames to stack
        pad_len = max(0, (T_lfr - 1) * lfr_n + lfr_m - T)
        if pad_len > 0:
            feats = np.concatenate([feats, np.tile(feats[-1:], (pad_len, 1))], axis=0)
        lfr_feats = []
        for i in range(T_lfr):
            start = i * lfr_n
            lfr_feats.append(feats[start:start + lfr_m].flatten())
        return np.stack(lfr_feats, axis=0)

    @staticmethod
    def _mel_filterbank(nfft: int, n_mels: int, sample_rate: int) -> np.ndarray:
        def hz2mel(hz): return 2595.0 * math.log10(1.0 + hz / 700.0)
        def mel2hz(mel): return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)
        mel_points = np.linspace(hz2mel(0), hz2mel(sample_rate / 2), n_mels + 2)
        hz_points = np.array([mel2hz(m) for m in mel_points])
        bins = np.floor((nfft + 1) * hz_points / sample_rate).astype(int)
        filters = np.zeros((n_mels, nfft // 2 + 1), dtype=np.float32)
        for i in range(n_mels):
            l, c, r = bins[i], bins[i + 1], bins[i + 2]
            for j in range(l, c):
                if c > l: filters[i, j] = (j - l) / (c - l)
            for j in range(c, r):
                if r > c: filters[i, j] = (r - j) / (r - c)
        return filters

    def recognize(self, audio: np.ndarray) -> str:
        if audio is None or len(audio) == 0:
            return ""
        if len(audio) < self.frame_length:
            # Pad short audio to minimum length
            audio = np.pad(audio, (0, self.frame_length - len(audio)))
        try:
            feats = self._compute_fbank(audio)[np.newaxis, :, :]
            feats_len = np.array([feats.shape[1]], dtype=np.int32)
            feed = {}
            for name in self.input_names:
                if "length" in name.lower() or "len" in name.lower():
                    feed[name] = feats_len
                else:
                    feed[name] = feats
            outputs = self.session.run(self.output_names, feed)
            if outputs:
                logits = outputs[0]
                if logits.ndim == 3:
                    token_ids = np.argmax(logits, axis=-1)[0]
                elif logits.ndim == 2:
                    token_ids = logits[0].astype(int)
                else:
                    token_ids = logits.flatten().astype(int)
                return self._decode_tokens(token_ids)
        except Exception as e:
            print(f"Warning: ASR recognize failed: {e}", flush=True)
        return ""

    def _decode_tokens(self, token_ids: np.ndarray) -> str:
        if not self.id2token:
            return " ".join(str(t) for t in token_ids if t > 0)
        tokens = []
        skip = {"", "<blank>", "<unk>", "<s>", "</s>", "<sos>", "<eos>", "<pad>", "<sos/eos>"}
        for tid in token_ids:
            token = self.id2token.get(int(tid), "")
            if token in skip:
                continue
            token = token.replace("@@", "").replace("\u2581", "")
            if token:
                tokens.append(token)
        return "".join(tokens)

    def recognize_segments(self, audio: np.ndarray, segments: list) -> list:
        results = []
        for start, end in segments:
            if start >= end or start >= len(audio):
                continue
            end = min(end, len(audio))
            try:
                text = self.recognize(audio[start:end])
                if text.strip():
                    results.append({
                        "start": round(start / self.sample_rate, 3),
                        "end": round(end / self.sample_rate, 3),
                        "text": text.strip()
                    })
            except Exception as e:
                print(f"Warning: segment [{start}:{end}] failed: {e}", flush=True)
        return results
