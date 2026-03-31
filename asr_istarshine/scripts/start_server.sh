#!/usr/bin/env bash
set -e
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "Starting ASR iStarShine V1 Server..."
exec python3 "$SKILL_DIR/scripts/asr_server.py" "$@"
