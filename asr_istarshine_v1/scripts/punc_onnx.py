#!/usr/bin/env python3
"""ONNX inference for punctuation restoration."""

import json
from pathlib import Path

import numpy as np
import onnxruntime as ort


class PunctuationONNX:
    """ONNX wrapper. Adds punctuation to raw ASR text."""

    DEFAULT_PUNCS = {0: "", 1: "", 2: "，", 3: "。", 4: "？", 5: "、"}

    def __init__(self, model_dir: str, encryption_key: bytes = None):
        model_dir = Path(model_dir)
        onnx_path = self._find_onnx(model_dir)
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 2
        opts.log_severity_level = 3

        if str(onnx_path).endswith(".enc") and encryption_key:
            from model_crypto import decrypt_model_to_memory
            model_bytes = decrypt_model_to_memory(onnx_path, encryption_key)
            self.session = ort.InferenceSession(model_bytes, sess_options=opts)
        else:
            self.session = ort.InferenceSession(str(onnx_path), sess_options=opts)

        self.vocab = self._load_vocab(model_dir)
        if isinstance(self.vocab, list):
            self.token2id = {tok: i for i, tok in enumerate(self.vocab)}
        else:
            self.token2id = self.vocab if self.vocab else {}
        self.punc_labels = self._load_punc_labels(model_dir)
        self.input_names = [inp.name for inp in self.session.get_inputs()]
        self.output_names = [out.name for out in self.session.get_outputs()]
        self.unk_id = self.token2id.get("<unk>", 1)
        self.max_len = 512

    @staticmethod
    def _find_onnx(model_dir: Path) -> Path:
        for suffix in ("*.onnx.enc", "*.onnx"):
            candidates = list(model_dir.glob(suffix))
            if candidates:
                for c in candidates:
                    stem = c.name.replace(".enc", "").replace(".onnx", "")
                    if stem in ("model", "punc"):
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
    def _load_punc_labels(model_dir: Path) -> dict:
        # Try config.yaml first (FunASR CT-Transformer format)
        config_path = model_dir / "config.yaml"
        if config_path.exists():
            try:
                import yaml
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                punc_list = cfg.get("model_conf", {}).get("punc_list", [])
                if punc_list:
                    labels = {}
                    for i, p in enumerate(punc_list):
                        if p in ("<unk>", "_", "O"):
                            labels[i] = ""
                        else:
                            labels[i] = p
                    return labels
            except Exception:
                pass

        for name in ("punc_labels.json", "labels.json"):
            path = model_dir / name
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return {i: p for i, p in enumerate(data)}
                return {int(k): v for k, v in data.items()}
        for name in ("punc_labels.txt", "labels.txt"):
            path = model_dir / name
            if path.exists():
                labels = {}
                with open(path, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        labels[i] = line.strip()
                return labels
        return PunctuationONNX.DEFAULT_PUNCS

    def _tokenize(self, text: str) -> list:
        ids = []
        for ch in text:
            if ch in self.token2id:
                ids.append(self.token2id[ch])
            elif ch.lower() in self.token2id:
                ids.append(self.token2id[ch.lower()])
            else:
                ids.append(self.unk_id)
        return ids

    def punctuate(self, text: str) -> str:
        """Add punctuation to raw ASR text. Returns original text on failure."""
        if not text or not text.strip():
            return text
        chars = list(text.replace(" ", ""))
        if not chars:
            return text
        try:
            token_ids = self._tokenize("".join(chars))
            all_punc_ids = []
            for i in range(0, len(token_ids), self.max_len):
                chunk = token_ids[i:i + self.max_len]
                all_punc_ids.extend(self._predict_chunk(chunk))
            result = []
            for i, ch in enumerate(chars):
                result.append(ch)
                if i < len(all_punc_ids):
                    punc = self.punc_labels.get(int(all_punc_ids[i]), "")
                    if punc:
                        result.append(punc)
            return "".join(result)
        except Exception as e:
            print(f"Warning: punctuation failed, returning raw text: {e}", flush=True)
            return text

    def _predict_chunk(self, token_ids: list) -> list:
        seq_len = len(token_ids)
        input_ids = np.array([token_ids], dtype=np.int32)
        text_lengths = np.array([seq_len], dtype=np.int32)
        feed = {}
        for name in self.input_names:
            if "length" in name.lower() or "len" in name.lower():
                feed[name] = text_lengths
            else:
                feed[name] = input_ids
        outputs = self.session.run(self.output_names, feed)
        if outputs:
            logits = outputs[0]
            if logits.ndim == 3:
                return np.argmax(logits, axis=-1)[0][:seq_len].tolist()
            elif logits.ndim == 2:
                return logits[0][:seq_len].astype(int).tolist()
            else:
                return logits.flatten()[:seq_len].astype(int).tolist()
        return [0] * seq_len
