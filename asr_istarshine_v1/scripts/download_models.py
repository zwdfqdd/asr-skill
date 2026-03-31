#!/usr/bin/env python3
"""Download models via session-based auth.

Flow:
    1. POST /api/session/start {license_key, machine_fp}
       → {session_token, manifest[{path, size, sha256}]}
    2. GET /files/<path>?session=<token>  for each file
    3. POST /api/session/complete {session_token}

ONNX files are encrypted in memory before writing to disk.
Plaintext model bytes NEVER touch the filesystem.

Usage:
    python download_models.py                          # download + encrypt
    python download_models.py --no-encrypt             # plaintext (dev only)
    python download_models.py --key-file custom.key    # use specific key file
"""

import argparse
import hashlib
import io
import json
import secrets
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
MODELS_DIR = SKILL_DIR / "models"
LICENSE_FILE = SKILL_DIR / "license.json"

# Service endpoint (resolved at runtime from license.json or CLI arg)
_DEFAULT_ENDPOINT = ""  # set via --endpoint or license.json


# ─── Machine fingerprint ───

def get_machine_fingerprint():
    import platform
    parts = [platform.node(), platform.machine()]
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\Microsoft\Cryptography")
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            parts.append(guid)
            winreg.CloseKey(key)
        except Exception:
            pass
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            parts.append(Path(p).read_text().strip())
            break
        except Exception:
            continue
    if sys.platform == "darwin":
        try:
            import subprocess
            r = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.split("\n"):
                if "IOPlatformUUID" in line:
                    parts.append(line.split('"')[-2])
                    break
        except Exception:
            pass
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ─── Inline encryption ───

MAGIC = b"OXEN"
ENC_VERSION = 1
FLAG_MACHINE_BIND = 0x01


def _derive_key(base_key):
    fp = get_machine_fingerprint().encode()
    return hashlib.sha256(base_key + fp).digest()


def encrypt_in_memory(plaintext, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    final_key = _derive_key(key)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(final_key).encrypt(nonce, plaintext, None)
    header = MAGIC + struct.pack("BB", ENC_VERSION, FLAG_MACHINE_BIND)
    return header + nonce + ciphertext


# ─── License ───

def load_license(license_path=None):
    """Load license config. Returns (license_key, endpoint)."""
    path = Path(license_path) if license_path else LICENSE_FILE
    if not path.exists():
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("license_key") or data.get("token")
        endpoint = data.get("endpoint")
        return key, endpoint
    except Exception:
        return None, None


# ─── HTTP helpers ───

def api_post(endpoint, path, payload):
    """POST JSON to API endpoint. Returns parsed response or raises."""
    url = f"{endpoint}{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "ModelDownloader/2.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(error_body)
            raise RuntimeError(f"API error ({e.code}): {err.get('error', error_body)}")
        except json.JSONDecodeError:
            raise RuntimeError(f"API error ({e.code}): {error_body}")


def download_to_memory(url, desc="", max_retries=3, expected_size=0):
    """Download file entirely into memory with retry + resume. Returns bytes."""
    buf = io.BytesIO()
    downloaded = 0

    for attempt in range(1, max_retries + 1):
        label = f" (retry {attempt}/{max_retries})" if attempt > 1 else ""
        if downloaded > 0:
            print(f"  Resuming {desc} from {downloaded/(1024*1024):.1f} MB...{label}", flush=True)
        else:
            print(f"  Downloading {desc}...{label}", flush=True)
        try:
            headers = {"User-Agent": "ModelDownloader/2.0"}
            if downloaded > 0:
                headers["Range"] = f"bytes={downloaded}-"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=300) as resp:
                # If server returns 200 (not 206), it doesn't support Range — restart
                if downloaded > 0 and resp.status == 200:
                    buf = io.BytesIO()
                    downloaded = 0
                total = expected_size or int(resp.headers.get("Content-Length", 0)) + downloaded
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    buf.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded * 100 // total
                        print(f"\r  [{pct:3d}%] {downloaded/(1024*1024):.1f}/{total/(1024*1024):.1f} MB",
                              end="", flush=True)
                if total > 0:
                    print()
            return buf.getvalue()
        except Exception as e:
            print(f"\n  Download error: {e}")
            if attempt < max_retries:
                import time
                wait = attempt * 3
                print(f"  Retrying in {wait}s...", flush=True)
                time.sleep(wait)
    return None


def download_to_file(url, dest, desc="", max_retries=3):
    """Download file directly to disk with retry + resume (non-ONNX files)."""
    downloaded = 0
    if dest.exists():
        downloaded = dest.stat().st_size

    for attempt in range(1, max_retries + 1):
        label = f" (retry {attempt}/{max_retries})" if attempt > 1 else ""
        if downloaded > 0 and attempt > 1:
            print(f"  Resuming {desc} from {downloaded/(1024*1024):.1f} MB...{label}", flush=True)
        else:
            print(f"  Downloading {desc}...{label}", flush=True)
            downloaded = 0  # fresh start on first attempt
        try:
            headers = {"User-Agent": "ModelDownloader/2.0"}
            if downloaded > 0:
                headers["Range"] = f"bytes={downloaded}-"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=300) as resp:
                if downloaded > 0 and resp.status == 200:
                    downloaded = 0  # server doesn't support Range
                total = int(resp.headers.get("Content-Length", 0)) + downloaded
                dest.parent.mkdir(parents=True, exist_ok=True)
                mode = "ab" if downloaded > 0 else "wb"
                with open(dest, mode) as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = downloaded * 100 // total
                            print(f"\r  [{pct:3d}%] {downloaded/(1024*1024):.1f}/{total/(1024*1024):.1f} MB",
                                  end="", flush=True)
                if total > 0:
                    print()
            return True
        except Exception as e:
            print(f"\n  Download error: {e}")
            if attempt < max_retries:
                import time
                wait = attempt * 3
                print(f"  Retrying in {wait}s...", flush=True)
                time.sleep(wait)
    # Clean up on total failure
    if dest.exists():
        dest.unlink()
    return False


# ─── Local manifest (for incremental updates) ───

_LOCAL_MANIFEST_FILE = MODELS_DIR / "manifest.json"


def _load_local_manifest():
    """Load local manifest {path: sha256} for incremental update checks."""
    if _LOCAL_MANIFEST_FILE.exists():
        try:
            with open(_LOCAL_MANIFEST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_local_manifest(manifest_dict):
    """Save local manifest after successful downloads."""
    _LOCAL_MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCAL_MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest_dict, f, indent=2)


# ─── Key management ───

def load_or_create_key(key_path):
    if key_path.exists():
        key = key_path.read_bytes()
        if len(key) >= 16:
            print(f"Using existing key: {key_path}")
            return key
    key = secrets.token_bytes(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    print(f"Generated new key: {key_path}")
    return key


# ─── Main download flow ───

def download_with_session(endpoint, license_key, encrypt=True, key=None):
    """Session-based download flow."""
    machine_fp = get_machine_fingerprint()

    # Step 1: Start session
    print("Starting download session...", flush=True)
    try:
        resp = api_post(endpoint, "/api/session/start", {
            "license_key": license_key,
            "machine_fp": machine_fp,
        })
    except RuntimeError as e:
        print(f"  Failed: {e}")
        return False

    session_token = resp["session_token"]
    manifest = resp["manifest"]
    expires_in = resp.get("expires_in", 1800)

    print(f"  Session started (expires in {expires_in}s)")
    print(f"  Files to download: {len(manifest)}")
    total_size = sum(f["size"] for f in manifest)
    print(f"  Total size: {total_size/(1024*1024):.1f} MB")
    print()

    # Step 2: Download each file
    all_ok = True
    skipped = 0
    local_manifest = _load_local_manifest()

    for i, finfo in enumerate(manifest, 1):
        fpath = finfo["path"]
        fsize = finfo["size"]
        fsha = finfo["sha256"]
        is_onnx = fpath.endswith(".onnx")

        dest = MODELS_DIR / fpath
        enc_dest = MODELS_DIR / (fpath + ".enc") if is_onnx else None

        # Skip if already exists AND sha256 matches (incremental update)
        local_sha = local_manifest.get(fpath)
        if local_sha == fsha:
            target = enc_dest if (is_onnx and encrypt) else dest
            if target and target.exists():
                skipped += 1
                print(f"[{i}/{len(manifest)}] {fpath} — up to date")
                continue

        print(f"[{i}/{len(manifest)}] {fpath} ({fsize/(1024*1024):.1f} MB)"
              + (" [update]" if local_sha and local_sha != fsha else ""))

        file_url = f"{endpoint}/files/{fpath}?session={session_token}"

        if is_onnx and encrypt and key:
            # Download to memory → verify → encrypt → write
            data = download_to_memory(file_url, fpath)
            if data is None:
                all_ok = False
                continue

            # Verify SHA-256
            actual_sha = hashlib.sha256(data).hexdigest()
            if actual_sha != fsha:
                print(f"  CHECKSUM MISMATCH: expected {fsha[:16]}... got {actual_sha[:16]}...")
                del data
                all_ok = False
                continue

            # Encrypt in memory
            print(f"  Encrypting...", flush=True)
            try:
                encrypted = encrypt_in_memory(data, key)
                del data  # clear plaintext

                enc_dest.parent.mkdir(parents=True, exist_ok=True)
                enc_dest.write_bytes(encrypted)
                del encrypted
                print(f"  Saved: {enc_dest.relative_to(MODELS_DIR)}")
            except ImportError:
                print("  Error: 'cryptography' package required. pip install cryptography")
                del data
                all_ok = False
                break
            except Exception as e:
                print(f"  Encryption error: {e}")
                all_ok = False
                continue
        else:
            # Non-ONNX or plaintext mode: download to file, then verify
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not download_to_file(file_url, dest, fpath):
                all_ok = False
                continue

            # Verify SHA-256
            actual_sha = hashlib.sha256(dest.read_bytes()).hexdigest()
            if actual_sha != fsha:
                print(f"  CHECKSUM MISMATCH: expected {fsha[:16]}... got {actual_sha[:16]}...")
                dest.unlink()
                all_ok = False
                continue

    # Step 3: Complete session
    print("\nCompleting session...", flush=True)
    try:
        api_post(endpoint, "/api/session/complete", {
            "session_token": session_token,
        })
        print("  Session closed.")
    except Exception as e:
        print(f"  Warning: failed to close session (will auto-expire): {e}")

    # Save local manifest for incremental updates
    if all_ok:
        for finfo in manifest:
            local_manifest[finfo["path"]] = finfo["sha256"]
        _save_local_manifest(local_manifest)

    if skipped:
        print(f"  ({skipped} files already up to date)")

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Model Downloader (session-based)")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="Download plaintext models (dev only)")
    parser.add_argument("--license-key", default=None,
                        help="License key (or set in license.json)")
    parser.add_argument("--license-file", default=None,
                        help="Path to license.json")
    parser.add_argument("--endpoint", default=None,
                        help="Server endpoint URL")
    parser.add_argument("--key-file", default=None,
                        help="Encryption key file path")
    args = parser.parse_args()

    # Load license
    file_key, file_endpoint = load_license(args.license_file)
    license_key = args.license_key or file_key
    endpoint = args.endpoint or file_endpoint or _DEFAULT_ENDPOINT

    if not license_key:
        print("Error: No license key provided.")
        print("  Use --license-key <key> or create license.json:")
        print('  {"license_key": "your_key_here", "endpoint": "https://..."}')
        sys.exit(1)

    if not endpoint:
        print("Error: No endpoint provided.")
        print("  Use --endpoint <url> or set in license.json:")
        print('  {"license_key": "...", "endpoint": "https://..."}')
        sys.exit(1)

    encrypt = not args.no_encrypt
    enc_key = None

    print("Model Downloader v2.0 (session-based)")
    print(f"Target: {MODELS_DIR}")
    print(f"Mode: {'ENCRYPTED + MACHINE-BOUND' if encrypt else 'PLAINTEXT (dev)'}")
    print(f"Endpoint: {endpoint}")
    print()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    if encrypt:
        key_path = Path(args.key_file) if args.key_file else MODELS_DIR / "model.key"
        enc_key = load_or_create_key(key_path)

    ok = download_with_session(endpoint, license_key, encrypt=encrypt, key=enc_key)

    print(f"\n=== {'All models downloaded' if ok else 'Some downloads failed'} ===")
    if encrypt and ok:
        print("Models are encrypted and bound to this machine.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
