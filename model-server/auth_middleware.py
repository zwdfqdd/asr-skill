#!/usr/bin/env python3
"""Model download auth middleware.

Lightweight token + session verification service for nginx auth_request.
Runs on port 8901, validates download tokens against a local database.

Architecture:
    license_key (long-lived)  →  proves identity
    session_token (ephemeral) →  authorizes one download session

Flow:
    1. Client POST /api/session/start {license_key, machine_fp}
       → Server validates license, binds machine, returns {session_token, manifest}
    2. Client GET /files/<path>  Header: X-Session: <session_token>
       → Server validates session, serves file
    3. Client POST /api/session/complete {session_token}
       → Server revokes session
    4. Timeout/disconnect → session auto-expires

Features:
- License key CRUD via admin API
- Per-license download limits (count + bandwidth)
- License expiration
- Ephemeral session tokens (TTL 30min, single-use)
- Machine fingerprint binding
- File manifest with SHA-256 checksums
- Download audit log
- SQLite storage (zero dependencies beyond stdlib)

Usage:
    python auth_middleware.py                    # start server
    python auth_middleware.py create-token       # generate a new license key
    python auth_middleware.py list-tokens        # list all license keys
    python auth_middleware.py revoke <key>       # revoke a license key
"""

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# ─── Config ───
HOST = "127.0.0.1"
PORT = 8901
DB_PATH = Path(__file__).parent / "auth.db"
ADMIN_KEY = os.environ.get("MODEL_ADMIN_KEY", "")
LOG_DIR = Path(__file__).parent / "logs"
MODELS_ROOT = Path(os.environ.get("MODEL_FILES_ROOT", "/home/zhxg/zw/data/models"))

# Default limits per license key
DEFAULT_MAX_DOWNLOADS = 100       # max download sessions (not individual files)
DEFAULT_MAX_BANDWIDTH = 10 * 1024 * 1024 * 1024  # 10 GB
DEFAULT_EXPIRE_DAYS = 365

# Session config
SESSION_TTL = 1800                # 30 minutes
SESSION_CLEANUP_INTERVAL = 300    # clean expired sessions every 5 min


# ─── Database ───

_db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()

    # License keys (formerly "tokens")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS license_keys (
            key_hash      TEXT PRIMARY KEY,
            key_prefix    TEXT NOT NULL,
            label         TEXT DEFAULT '',
            created_at    REAL NOT NULL,
            expires_at    REAL,
            max_downloads INTEGER DEFAULT 100,
            max_bandwidth INTEGER DEFAULT 10737418240,
            downloads     INTEGER DEFAULT 0,
            bandwidth     INTEGER DEFAULT 0,
            machine_fp    TEXT DEFAULT '',
            active        INTEGER DEFAULT 1
        )
    """)

    # Ephemeral download sessions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_token TEXT PRIMARY KEY,
            key_hash      TEXT NOT NULL,
            machine_fp    TEXT NOT NULL,
            ip            TEXT DEFAULT '',
            created_at    REAL NOT NULL,
            expires_at    REAL NOT NULL,
            files_total   INTEGER DEFAULT 0,
            files_done    INTEGER DEFAULT 0,
            bytes_done    INTEGER DEFAULT 0,
            status        TEXT DEFAULT 'active',
            FOREIGN KEY (key_hash) REFERENCES license_keys(key_hash)
        )
    """)

    # Per-file download tracking within a session
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_files (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_token TEXT NOT NULL,
            file_path     TEXT NOT NULL,
            file_size     INTEGER DEFAULT 0,
            downloaded_at REAL,
            status        TEXT DEFAULT 'pending',
            FOREIGN KEY (session_token) REFERENCES sessions(session_token)
        )
    """)

    # Audit log
    conn.execute("""
        CREATE TABLE IF NOT EXISTS download_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash      TEXT NOT NULL,
            session_token TEXT,
            timestamp     REAL NOT NULL,
            ip            TEXT,
            uri           TEXT,
            file_size     INTEGER DEFAULT 0,
            status        TEXT DEFAULT 'ok'
        )
    """)

    conn.commit()
    conn.close()


def hash_key(key):
    """SHA-256 hash for storage (never store plaintext)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ─── Manifest ───

def build_manifest():
    """Scan MODELS_ROOT and build file manifest with SHA-256 checksums."""
    manifest = []
    if not MODELS_ROOT.exists():
        return manifest

    for f in sorted(MODELS_ROOT.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(MODELS_ROOT).as_posix()
        size = f.stat().st_size
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        manifest.append({
            "path": rel,
            "size": size,
            "sha256": sha,
        })
    return manifest


# Cache manifest (rebuild on session start if stale)
_manifest_cache = {"data": None, "mtime": 0}
_MANIFEST_TTL = 60  # re-scan at most every 60s


def get_manifest():
    now = time.time()
    if _manifest_cache["data"] is None or now - _manifest_cache["mtime"] > _MANIFEST_TTL:
        _manifest_cache["data"] = build_manifest()
        _manifest_cache["mtime"] = now
    return _manifest_cache["data"]


# ─── License Key Management ───

def create_license_key(label="", max_downloads=DEFAULT_MAX_DOWNLOADS,
                       max_bandwidth=DEFAULT_MAX_BANDWIDTH,
                       expire_days=DEFAULT_EXPIRE_DAYS):
    """Create a new license key. Returns the plaintext key (show once)."""
    key = secrets.token_hex(32)
    key_h = hash_key(key)
    now = time.time()
    expires = now + expire_days * 86400 if expire_days > 0 else None

    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO license_keys (key_hash, key_prefix, label, created_at, "
            "expires_at, max_downloads, max_bandwidth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key_h, key[:8], label, now, expires, max_downloads, max_bandwidth)
        )
        conn.commit()
        conn.close()
    return key


def validate_license_key(key):
    """Validate a license key (without consuming quota). Returns (valid, reason, key_hash)."""
    if not key:
        return False, "no key", ""

    key_h = hash_key(key)
    conn = get_db()
    row = conn.execute(
        "SELECT active, expires_at, max_downloads, downloads, "
        "max_bandwidth, bandwidth, machine_fp FROM license_keys WHERE key_hash = ?",
        (key_h,)
    ).fetchone()
    conn.close()

    if not row:
        return False, "invalid key", ""

    active, expires_at, max_dl, dl_count, max_bw, bw_used, bound_fp = row

    if not active:
        return False, "key revoked", ""
    if expires_at and time.time() > expires_at:
        return False, "key expired", ""
    if max_dl > 0 and dl_count >= max_dl:
        return False, "download limit reached", ""
    if max_bw > 0 and bw_used >= max_bw:
        return False, "bandwidth limit reached", ""

    return True, "ok", key_h


def bind_machine(key_h, machine_fp):
    """Bind or verify machine fingerprint for a license key.
    First call binds; subsequent calls must match."""
    conn = get_db()
    row = conn.execute(
        "SELECT machine_fp FROM license_keys WHERE key_hash = ?", (key_h,)
    ).fetchone()

    if not row:
        conn.close()
        return False, "key not found"

    bound_fp = row[0]

    if not bound_fp:
        # First use — bind this machine
        with _db_lock:
            conn.execute(
                "UPDATE license_keys SET machine_fp = ? WHERE key_hash = ?",
                (machine_fp, key_h)
            )
            conn.commit()
        conn.close()
        return True, "bound"
    elif bound_fp == machine_fp:
        conn.close()
        return True, "matched"
    else:
        conn.close()
        return False, "machine mismatch"


def revoke_license_key(key):
    key_h = hash_key(key)
    with _db_lock:
        conn = get_db()
        conn.execute("UPDATE license_keys SET active = 0 WHERE key_hash = ?", (key_h,))
        conn.commit()
        conn.close()


def list_license_keys():
    conn = get_db()
    rows = conn.execute(
        "SELECT key_prefix, label, created_at, expires_at, "
        "max_downloads, downloads, max_bandwidth, bandwidth, active, machine_fp "
        "FROM license_keys ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return rows


# ─── Session Management ───

def create_session(key_h, machine_fp, ip, manifest):
    """Create an ephemeral download session. Returns session_token."""
    session_token = secrets.token_hex(24)
    now = time.time()
    expires = now + SESSION_TTL

    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO sessions (session_token, key_hash, machine_fp, ip, "
            "created_at, expires_at, files_total, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
            (session_token, key_h, machine_fp, ip, now, expires, len(manifest))
        )
        # Pre-register all files for this session
        for f in manifest:
            conn.execute(
                "INSERT INTO session_files (session_token, file_path, file_size, status) "
                "VALUES (?, ?, ?, 'pending')",
                (session_token, f["path"], f["size"])
            )
        # Consume one download quota from the license key
        conn.execute(
            "UPDATE license_keys SET downloads = downloads + 1 WHERE key_hash = ?",
            (key_h,)
        )
        conn.commit()
        conn.close()

    return session_token


def verify_session(session_token, ip="", uri=""):
    """Verify a session token for file download. Returns (valid, reason)."""
    if not session_token:
        return False, "no session"

    conn = get_db()
    row = conn.execute(
        "SELECT key_hash, machine_fp, expires_at, status FROM sessions "
        "WHERE session_token = ?",
        (session_token,)
    ).fetchone()

    if not row:
        conn.close()
        return False, "invalid session"

    key_h, machine_fp, expires_at, status = row

    if status != "active":
        conn.close()
        return False, f"session {status}"

    if time.time() > expires_at:
        # Auto-expire
        with _db_lock:
            conn.execute(
                "UPDATE sessions SET status = 'expired' WHERE session_token = ?",
                (session_token,)
            )
            conn.commit()
        conn.close()
        return False, "session expired"

    # Extract relative file path from URI (strip /files/ prefix and query)
    file_path = ""
    if uri:
        parsed = urlparse(uri)
        p = parsed.path
        if p.startswith("/files/"):
            file_path = p[len("/files/"):]

    # Mark file as downloaded + update counters
    if file_path:
        with _db_lock:
            sf = conn.execute(
                "SELECT id, file_size, status FROM session_files "
                "WHERE session_token = ? AND file_path = ?",
                (session_token, file_path)
            ).fetchone()

            if sf:
                sf_id, file_size, sf_status = sf
                if sf_status == "pending":
                    conn.execute(
                        "UPDATE session_files SET status = 'done', downloaded_at = ? "
                        "WHERE id = ?",
                        (time.time(), sf_id)
                    )
                    conn.execute(
                        "UPDATE sessions SET files_done = files_done + 1, "
                        "bytes_done = bytes_done + ? WHERE session_token = ?",
                        (file_size, session_token)
                    )
                    # Update license bandwidth
                    conn.execute(
                        "UPDATE license_keys SET bandwidth = bandwidth + ? "
                        "WHERE key_hash = ?",
                        (file_size, key_h)
                    )

            # Audit log
            conn.execute(
                "INSERT INTO download_log (key_hash, session_token, timestamp, ip, uri, status) "
                "VALUES (?, ?, ?, ?, ?, 'ok')",
                (key_h, session_token, time.time(), ip, uri)
            )
            conn.commit()

    conn.close()
    return True, "ok"


def complete_session(session_token):
    """Mark session as completed and revoke it."""
    with _db_lock:
        conn = get_db()
        conn.execute(
            "UPDATE sessions SET status = 'completed' WHERE session_token = ? AND status = 'active'",
            (session_token,)
        )
        conn.commit()
        conn.close()


def cleanup_expired_sessions():
    """Clean up expired sessions."""
    now = time.time()
    with _db_lock:
        conn = get_db()
        conn.execute(
            "UPDATE sessions SET status = 'expired' "
            "WHERE status = 'active' AND expires_at < ?",
            (now,)
        )
        conn.commit()
        conn.close()


def _cleanup_loop():
    """Background thread to periodically clean expired sessions."""
    while True:
        time.sleep(SESSION_CLEANUP_INTERVAL)
        try:
            cleanup_expired_sessions()
        except Exception:
            pass


# ─── HTTP Handler ───

class AuthHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log_file = LOG_DIR / f"auth-{datetime.now().strftime('%Y-%m-%d')}.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {args[0]}\n")

    def _read_json_body(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _check_admin(self):
        if not ADMIN_KEY:
            self._json_response(500, {"error": "MODEL_ADMIN_KEY not set"})
            return False
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {ADMIN_KEY}":
            self._json_response(403, {"error": "invalid admin key"})
            return False
        return True

    # ─── GET ───

    def do_GET(self):
        parsed = urlparse(self.path)

        # nginx auth_request: /verify (now session-based)
        if parsed.path == "/verify":
            session_token = self.headers.get("X-Session", "")
            ip = self.headers.get("X-Real-IP", self.client_address[0])
            uri = self.headers.get("X-Original-URI", "")

            valid, reason = verify_session(session_token, ip, uri)
            if valid:
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": reason}).encode())
            return

        # Admin API
        if parsed.path.startswith("/admin/"):
            if not self._check_admin():
                return
            self._handle_admin_get(parsed)
            return

        # File download: /files/<path>?session=<token>
        if parsed.path.startswith("/files/"):
            from urllib.parse import parse_qs
            qs = parse_qs(parsed.query)
            session_token = qs.get("session", [""])[0]
            ip = self.client_address[0]
            file_rel = parsed.path[len("/files/"):]

            valid, reason = verify_session(session_token, ip, f"/files/{file_rel}")
            if not valid:
                self._json_response(403, {"error": reason})
                return

            file_path = MODELS_ROOT / file_rel
            if not file_path.exists() or not file_path.is_file():
                self._json_response(404, {"error": "file not found"})
                return

            # Prevent path traversal
            try:
                file_path.resolve().relative_to(MODELS_ROOT.resolve())
            except ValueError:
                self._json_response(403, {"error": "access denied"})
                return

            file_size = file_path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
            self.end_headers()

            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        self.send_response(404)
        self.end_headers()

    # ─── POST ───

    def do_POST(self):
        parsed = urlparse(self.path)

        # ─── Public API: session management ───
        if parsed.path == "/api/session/start":
            self._handle_session_start()
            return

        if parsed.path == "/api/session/complete":
            self._handle_session_complete()
            return

        # ─── Admin API ───
        if parsed.path.startswith("/admin/"):
            if not self._check_admin():
                return
            data = self._read_json_body()
            self._handle_admin_post(parsed, data)
            return

        self.send_response(404)
        self.end_headers()

    # ─── Session Handlers ───

    def _handle_session_start(self):
        data = self._read_json_body()
        license_key = data.get("license_key", "")
        machine_fp = data.get("machine_fp", "")
        ip = self.headers.get("X-Real-IP", self.client_address[0])

        if not license_key:
            self._json_response(400, {"error": "license_key required"})
            return

        if not machine_fp:
            self._json_response(400, {"error": "machine_fp required"})
            return

        # Validate license key
        valid, reason, key_h = validate_license_key(license_key)
        if not valid:
            self._json_response(403, {"error": reason})
            return

        # Bind / verify machine
        bound, bind_reason = bind_machine(key_h, machine_fp)
        if not bound:
            self._json_response(403, {"error": bind_reason})
            return

        # Build manifest
        manifest = get_manifest()
        if not manifest:
            self._json_response(500, {"error": "no model files available"})
            return

        # Create session
        session_token = create_session(key_h, machine_fp, ip, manifest)

        self._json_response(200, {
            "session_token": session_token,
            "expires_in": SESSION_TTL,
            "manifest": manifest,
        })

    def _handle_session_complete(self):
        data = self._read_json_body()
        session_token = data.get("session_token", "")

        if not session_token:
            self._json_response(400, {"error": "session_token required"})
            return

        complete_session(session_token)
        self._json_response(200, {"status": "completed"})

    # ─── Admin Handlers ───

    def _handle_admin_get(self, parsed):
        if parsed.path == "/admin/tokens":
            rows = list_license_keys()
            keys = []
            for r in rows:
                keys.append({
                    "prefix": r[0] + "...",
                    "label": r[1],
                    "created": datetime.fromtimestamp(r[2]).isoformat(),
                    "expires": datetime.fromtimestamp(r[3]).isoformat() if r[3] else None,
                    "downloads": f"{r[5]}/{r[4]}",
                    "bandwidth_mb": f"{r[7]/(1024*1024):.0f}/{r[6]/(1024*1024):.0f}",
                    "active": bool(r[8]),
                    "machine_bound": bool(r[9]),
                })
            self._json_response(200, {"tokens": keys})

        elif parsed.path == "/admin/stats":
            conn = get_db()
            total = conn.execute(
                "SELECT COUNT(*) FROM license_keys WHERE active=1"
            ).fetchone()[0]
            active_sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE status='active'"
            ).fetchone()[0]
            today = conn.execute(
                "SELECT COUNT(*) FROM download_log WHERE timestamp > ?",
                (time.time() - 86400,)
            ).fetchone()[0]
            conn.close()
            self._json_response(200, {
                "active_keys": total,
                "active_sessions": active_sessions,
                "downloads_24h": today,
            })

        elif parsed.path == "/admin/sessions":
            conn = get_db()
            rows = conn.execute(
                "SELECT session_token, key_hash, machine_fp, ip, "
                "created_at, expires_at, files_total, files_done, bytes_done, status "
                "FROM sessions ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
            conn.close()
            sessions = []
            for r in rows:
                sessions.append({
                    "token_prefix": r[0][:8] + "...",
                    "key_prefix": r[1][:8] + "...",
                    "ip": r[3],
                    "created": datetime.fromtimestamp(r[4]).isoformat(),
                    "expires": datetime.fromtimestamp(r[5]).isoformat(),
                    "progress": f"{r[7]}/{r[6]}",
                    "bytes_mb": f"{r[8]/(1024*1024):.1f}",
                    "status": r[9],
                })
            self._json_response(200, {"sessions": sessions})

        elif parsed.path == "/admin/manifest":
            self._json_response(200, {"manifest": get_manifest()})

        else:
            self._json_response(404, {"error": "not found"})

    def _handle_admin_post(self, parsed, data):
        if parsed.path == "/admin/tokens/create":
            key = create_license_key(
                label=data.get("label", ""),
                max_downloads=data.get("max_downloads", DEFAULT_MAX_DOWNLOADS),
                max_bandwidth=data.get("max_bandwidth", DEFAULT_MAX_BANDWIDTH),
                expire_days=data.get("expire_days", DEFAULT_EXPIRE_DAYS),
            )
            self._json_response(200, {
                "license_key": key,
                "message": "Save this key - it cannot be retrieved later",
            })

        elif parsed.path == "/admin/tokens/revoke":
            key = data.get("token", "") or data.get("license_key", "")
            if key:
                revoke_license_key(key)
                self._json_response(200, {"message": "key revoked"})
            else:
                self._json_response(400, {"error": "key required"})

        else:
            self._json_response(404, {"error": "not found"})


# ─── CLI ───

def cmd_serve():
    global ADMIN_KEY
    init_db()
    if not ADMIN_KEY:
        generated = secrets.token_hex(16)
        print(f"WARNING: MODEL_ADMIN_KEY not set. Generated temporary key:")
        print(f"  export MODEL_ADMIN_KEY={generated}")
        ADMIN_KEY = generated

    # Start session cleanup thread
    t = threading.Thread(target=_cleanup_loop, daemon=True)
    t.start()

    print(f"\nAuth middleware listening on {HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    print(f"Models root: {MODELS_ROOT}")
    print(f"Session TTL: {SESSION_TTL}s")
    print(f"Logs: {LOG_DIR}")
    server = HTTPServer((HOST, PORT), AuthHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


def cmd_create_token():
    init_db()
    key = create_license_key(
        label=input("Label (optional): ").strip() or "default",
        expire_days=int(input(f"Expire days [{DEFAULT_EXPIRE_DAYS}]: ").strip() or DEFAULT_EXPIRE_DAYS),
        max_downloads=int(input(f"Max download sessions [{DEFAULT_MAX_DOWNLOADS}]: ").strip() or DEFAULT_MAX_DOWNLOADS),
    )
    print(f"\nLicense key created (save it now, cannot retrieve later):")
    print(f"  {key}")
    print(f"\nFor license.json:")
    print(json.dumps({"license_key": key}, indent=2))


def cmd_list_tokens():
    init_db()
    rows = list_license_keys()
    if not rows:
        print("No license keys found.")
        return
    print(f"{'Prefix':<12} {'Label':<15} {'Sessions':<12} {'Active':<8} {'Machine':<10} {'Expires'}")
    print("-" * 80)
    for r in rows:
        exp = datetime.fromtimestamp(r[3]).strftime("%Y-%m-%d") if r[3] else "never"
        fp = "bound" if r[9] else "any"
        print(f"{r[0]+'...':<12} {r[1]:<15} {r[5]}/{r[4]:<10} {'yes' if r[8] else 'NO':<8} {fp:<10} {exp}")


def cmd_revoke(key):
    init_db()
    revoke_license_key(key)
    print(f"License key revoked: {key[:8]}...")


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "serve":
        cmd_serve()
    elif sys.argv[1] == "create-token":
        cmd_create_token()
    elif sys.argv[1] == "list-tokens":
        cmd_list_tokens()
    elif sys.argv[1] == "revoke" and len(sys.argv) > 2:
        cmd_revoke(sys.argv[2])
    else:
        print("Usage:")
        print("  python auth_middleware.py [serve]        Start auth server")
        print("  python auth_middleware.py create-token   Create license key")
        print("  python auth_middleware.py list-tokens    List all license keys")
        print("  python auth_middleware.py revoke <key>   Revoke a license key")


if __name__ == "__main__":
    main()
