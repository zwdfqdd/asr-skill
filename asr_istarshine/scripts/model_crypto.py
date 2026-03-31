#!/usr/bin/env python3
"""Model encryption/decryption for ONNX models.

Encrypts .onnx files with AES-256-GCM, optionally bound to machine fingerprint.
Decrypts to memory only — never writes plaintext model to disk.

Usage:
    # Encrypt all models (generates key if not exists)
    python model_crypto.py encrypt --models-dir <dir> [--key-file key.bin] [--machine-bind]

    # Decrypt and verify (test only)
    python model_crypto.py verify --models-dir <dir> [--key-file key.bin]

    # Generate machine fingerprint
    python model_crypto.py fingerprint
"""

import argparse
import hashlib
import os
import platform
import secrets
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Machine fingerprint — ties key to this specific machine
# ---------------------------------------------------------------------------

def get_machine_fingerprint() -> str:
    """Generate a stable machine-specific fingerprint."""
    parts = []

    # OS + architecture
    parts.append(platform.node())
    parts.append(platform.machine())

    # Windows: use machine GUID from registry
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography"
            )
            machine_guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            parts.append(machine_guid)
            winreg.CloseKey(key)
        except Exception:
            pass

    # Linux: use machine-id
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            parts.append(Path(p).read_text().strip())
            break
        except Exception:
            continue

    # macOS: use hardware UUID
    if sys.platform == "darwin":
        try:
            import subprocess
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                if "IOPlatformUUID" in line:
                    uuid = line.split('"')[-2]
                    parts.append(uuid)
                    break
        except Exception:
            pass

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def derive_key(base_key: bytes) -> bytes:
    """Derive final AES-256 key mixed with machine fingerprint."""
    fp = get_machine_fingerprint().encode("utf-8")
    return hashlib.sha256(base_key + fp).digest()


# ---------------------------------------------------------------------------
# AES-256-GCM encrypt / decrypt
# ---------------------------------------------------------------------------

# File format: MAGIC(4) + VERSION(1) + FLAGS(1) + NONCE(12) + TAG(16) + CIPHERTEXT
MAGIC = b"OXEN"  # ONNX Encrypted
VERSION = 1
FLAG_MACHINE_BIND = 0x01


def encrypt_bytes(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt bytes with AES-256-GCM. Returns nonce + tag + ciphertext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    # AESGCM.encrypt appends 16-byte tag to ciphertext
    return nonce + ciphertext


def decrypt_bytes(data: bytes, key: bytes) -> bytes:
    """Decrypt AES-256-GCM encrypted bytes. Input: nonce(12) + ciphertext+tag."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = data[:12]
    ciphertext_with_tag = data[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext_with_tag, None)


def encrypt_model_file(onnx_path: Path, key: bytes) -> Path:
    """Encrypt an .onnx file → .onnx.enc (always machine-bound)"""
    plaintext = onnx_path.read_bytes()
    final_key = derive_key(key)

    encrypted = encrypt_bytes(plaintext, final_key)

    enc_path = onnx_path.with_suffix(onnx_path.suffix + ".enc")
    with open(enc_path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("BB", VERSION, FLAG_MACHINE_BIND))
        f.write(encrypted)

    return enc_path


def decrypt_model_to_memory(enc_path: Path, key: bytes) -> bytes:
    """Decrypt .onnx.enc → raw model bytes in memory. Never touches disk."""
    data = enc_path.read_bytes()

    if data[:4] != MAGIC:
        raise ValueError(f"Not an encrypted model file: {enc_path}")

    version, flags = struct.unpack("BB", data[4:6])
    if version != VERSION:
        raise ValueError(f"Unsupported encryption version: {version}")

    final_key = derive_key(key)

    try:
        return decrypt_bytes(data[6:], final_key)
    except Exception:
        raise RuntimeError(
            f"Decryption failed for {enc_path.name}. "
            "Model is bound to a different machine or wrong key."
        )


# ---------------------------------------------------------------------------
# ONNX Runtime integration — load encrypted model directly
# ---------------------------------------------------------------------------

def load_encrypted_session(enc_path, key: bytes, sess_options=None):
    """Load an encrypted .onnx.enc file into an ONNX Runtime InferenceSession.

    Model is decrypted in memory only — never written to disk.
    """
    import onnxruntime as ort
    model_bytes = decrypt_model_to_memory(Path(enc_path), key)
    if sess_options is None:
        sess_options = ort.SessionOptions()
        sess_options.log_severity_level = 3
    return ort.InferenceSession(model_bytes, sess_options=sess_options)


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------

def generate_key_file(key_path: Path) -> bytes:
    """Generate a random 256-bit key and save to file."""
    key = secrets.token_bytes(32)
    key_path.write_bytes(key)
    print(f"Generated key: {key_path}")
    return key


def load_key(key_path: Path) -> bytes:
    """Load key from file."""
    if not key_path.exists():
        raise FileNotFoundError(f"Key file not found: {key_path}")
    key = key_path.read_bytes()
    if len(key) < 16:
        raise ValueError("Key file too short (need at least 16 bytes)")
    return key


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_encrypt(args):
    models_dir = Path(args.models_dir)
    key_path = Path(args.key_file)

    if key_path.exists():
        key = load_key(key_path)
        print(f"Using existing key: {key_path}")
    else:
        key = generate_key_file(key_path)

    onnx_files = list(models_dir.rglob("*.onnx"))
    onnx_files = [f for f in onnx_files if not f.name.endswith(".enc")]

    if not onnx_files:
        print("No .onnx files found to encrypt.")
        return

    for f in onnx_files:
        enc_path = encrypt_model_file(f, key)
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  Encrypted: {f.name} ({size_mb:.1f} MB) -> {enc_path.name}")

    if args.remove_originals:
        for f in onnx_files:
            f.unlink()
            print(f"  Removed original: {f.name}")

    print(f"\nDone. {len(onnx_files)} models encrypted (machine-bound).")
    print(f"Key: {key_path.absolute()}")
    print("WARNING: Keep the key file safe! Without it, models cannot be decrypted.")


def cmd_verify(args):
    models_dir = Path(args.models_dir)
    key_path = Path(args.key_file)
    key = load_key(key_path)

    enc_files = list(models_dir.rglob("*.onnx.enc"))
    if not enc_files:
        print("No .onnx.enc files found.")
        return

    import onnxruntime as ort
    for f in enc_files:
        try:
            model_bytes = decrypt_model_to_memory(f, key)
            sess = ort.InferenceSession(model_bytes)
            inputs = [i.name for i in sess.get_inputs()]
            print(f"  OK: {f.name} (inputs: {inputs})")
        except Exception as e:
            print(f"  FAILED: {f.name} - {e}")


def cmd_fingerprint(args):
    fp = get_machine_fingerprint()
    print(f"Machine fingerprint: {fp}")


def main():
    parser = argparse.ArgumentParser(description="ONNX Model Encryption Tool")
    sub = parser.add_subparsers(dest="command")

    p_enc = sub.add_parser("encrypt", help="Encrypt .onnx models")
    p_enc.add_argument("--models-dir", required=True, help="Directory containing .onnx files")
    p_enc.add_argument("--key-file", default="model.key", help="Key file path (default: model.key)")
    p_enc.add_argument("--remove-originals", action="store_true", help="Delete original .onnx after encryption")

    p_ver = sub.add_parser("verify", help="Verify encrypted models can be loaded")
    p_ver.add_argument("--models-dir", required=True, help="Directory containing .onnx.enc files")
    p_ver.add_argument("--key-file", default="model.key", help="Key file path")

    p_fp = sub.add_parser("fingerprint", help="Show machine fingerprint")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {"encrypt": cmd_encrypt, "verify": cmd_verify, "fingerprint": cmd_fingerprint}[args.command](args)


if __name__ == "__main__":
    main()
