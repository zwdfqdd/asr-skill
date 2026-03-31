#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== ASR iStarShine V1 Installer ==="
echo "Pure ONNX — no PyTorch, no FunASR"
echo ""

command -v python3 &>/dev/null || { echo "Error: python3 not found"; exit 1; }
echo "Python: $(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

echo ""
echo "Installing dependencies..."
pip install onnxruntime numpy pyyaml soundfile websockets cryptography

echo ""
if command -v ffmpeg &>/dev/null; then
    echo "ffmpeg: $(ffmpeg -version 2>&1 | head -1)"
else
    echo "Warning: ffmpeg not found (optional, for non-WAV formats)"
fi

# Check license.json
LICENSE_FILE="$SKILL_DIR/license.json"
if [ ! -f "$LICENSE_FILE" ]; then
    echo ""
    echo "=== License Setup ==="
    echo "A license key is required to download models."
    read -rp "License key: " LICENSE_KEY
    if [ -n "$LICENSE_KEY" ]; then
        read -rp "Server endpoint: " ENDPOINT
        if [ -z "$ENDPOINT" ]; then
            echo "Error: endpoint is required."
            exit 1
        fi
        cat > "$LICENSE_FILE" <<EOF
{
    "license_key": "$LICENSE_KEY",
    "endpoint": "$ENDPOINT"
}
EOF
        echo "Saved to $LICENSE_FILE"
    else
        echo "Skipped. Create license.json manually before downloading models."
    fi
fi

echo ""
echo "=== Download Models ==="
echo "  1) Download now (encrypted + machine-bound)"
echo "  2) Skip"
read -rp "Choice [1]: " c
if [ "${c:-1}" = "1" ]; then
    python3 "$SKILL_DIR/scripts/download_models.py"
else
    echo "Run later: python3 $SKILL_DIR/scripts/download_models.py"
fi

echo ""
echo "=== Done ==="
echo "Test:  python3 $SKILL_DIR/scripts/test_asr.py"
echo "Usage: python3 $SKILL_DIR/scripts/asr_tools.py transcribe <audio.wav>"
