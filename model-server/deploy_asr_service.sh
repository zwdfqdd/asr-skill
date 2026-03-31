#!/bin/bash
# deploy_asr_service.sh — Deploy ASR inference service on 192.168.223.5
# Runs alongside model-server (auth middleware)
#
# Prerequisites:
#   - Python 3.8+ with pip
#   - Model files in /home/zhxg/zw/data/models/{vad,asr,punc}/
#   - model-server deployed (deploy.sh)
#
# Usage:
#   chmod +x deploy_asr_service.sh
#   ./deploy_asr_service.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="asr-inference"
INSTALL_DIR="/opt/asr-inference"
MODELS_DIR="/home/zhxg/zw/data/models"
ASR_HOST="0.0.0.0"
ASR_PORT=2701
SERVER_IP="192.168.223.5"

echo "=== ASR Inference Service Setup ==="
echo "  Server:     ${SERVER_IP}"
echo "  Models:     ${MODELS_DIR}"
echo "  Install to: ${INSTALL_DIR}"
echo "  Listen:     ${ASR_HOST}:${ASR_PORT}"
echo ""

# 0. Check Python
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found"
    exit 1
fi
echo "Python: $(python3 --version)"

# 1. Install Python dependencies
echo ""
echo "[1/5] Installing Python dependencies..."
pip3 install --quiet onnxruntime numpy pyyaml soundfile websockets cryptography
echo "  Done."

# Check ffmpeg
if command -v ffmpeg &>/dev/null; then
    echo "  ffmpeg: $(ffmpeg -version 2>&1 | head -1)"
else
    echo "  ⚠️  ffmpeg not found (optional, for non-WAV formats)"
    echo "  Install: sudo apt install ffmpeg"
fi

# 2. Copy ASR scripts
echo ""
echo "[2/5] Installing ASR scripts to ${INSTALL_DIR}..."
sudo mkdir -p ${INSTALL_DIR}/scripts
sudo mkdir -p ${INSTALL_DIR}/assets

# Copy all Python scripts
for f in asr_tools.py asr_server.py paraformer_onnx.py vad_onnx.py punc_onnx.py model_crypto.py test_asr.py; do
    if [ -f "${SCRIPT_DIR}/scripts/${f}" ]; then
        sudo cp "${SCRIPT_DIR}/scripts/${f}" ${INSTALL_DIR}/scripts/
        echo "  Copied: ${f}"
    else
        echo "  ⚠️  Warning: ${f} not found in ${SCRIPT_DIR}/scripts/"
    fi
done

# 3. Create server config (pointing to actual model paths)
echo ""
echo "[3/5] Creating server config..."
sudo tee ${INSTALL_DIR}/assets/asr_config.yaml > /dev/null <<EOF
# ASR inference config — deployed on ${SERVER_IP}
models:
  vad: "${MODELS_DIR}/vad"
  asr: "${MODELS_DIR}/asr"
  punc: "${MODELS_DIR}/punc"

vad:
  threshold: 0.5
  min_speech_ms: 250
  min_silence_ms: 100
  speech_pad_ms: 30
  window_size: 512

audio:
  sample_rate: 16000
  channels: 1

features:
  n_mels: 80
  frame_length: 25
  frame_shift: 10
  window: "hamming"

server:
  host: "${ASR_HOST}"
  port: ${ASR_PORT}

output:
  include_timestamps: true
  include_punctuation: true
  pretty_json: true
EOF
echo "  Config written to ${INSTALL_DIR}/assets/asr_config.yaml"

# 4. Create systemd service
echo ""
echo "[4/5] Creating systemd service..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=ASR iStarShine V1 Inference Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONPATH=${INSTALL_DIR}/scripts
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/scripts/asr_server.py --host ${ASR_HOST} --port ${ASR_PORT} --http-only
Restart=always
RestartSec=5

# Resource limits
LimitNOFILE=65536
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl start ${SERVICE_NAME}
echo "  Service started."

# 4b. Update nginx config (add ASR proxy routes)
echo ""
echo "[4b] Updating nginx config..."
if command -v nginx &>/dev/null; then
    if [ -f "${SCRIPT_DIR}/nginx.conf" ]; then
        sudo cp "${SCRIPT_DIR}/nginx.conf" /etc/nginx/conf.d/model-server.conf
        if sudo nginx -t 2>/dev/null; then
            sudo systemctl reload nginx
            echo "  nginx updated with ASR proxy routes."
        else
            echo "  ⚠️  nginx config test failed. Check: sudo nginx -t"
        fi
    fi
else
    echo "  nginx not found. ASR service available directly on port ${ASR_PORT}."
fi

# 5. Verify
echo ""
echo "[5/5] Verifying..."
sleep 2
if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo "  ✅ ${SERVICE_NAME} is running"
    if command -v curl &>/dev/null; then
        HEALTH=$(curl -s http://127.0.0.1:${ASR_PORT}/health 2>/dev/null || echo '{"status":"unreachable"}')
        echo "  Health: ${HEALTH}"
    fi
else
    echo "  ❌ ${SERVICE_NAME} failed to start"
    echo "  Check: journalctl -u ${SERVICE_NAME} -n 20"
fi

echo ""
echo "============================================"
echo "  ✅ ASR Inference Service deployed!"
echo ""
echo "  Endpoint: http://${SERVER_IP}:${ASR_PORT}"
echo ""
echo "  API:"
echo "    POST /transcribe          (multipart file upload)"
echo "    POST /transcribe/base64   (JSON base64 audio)"
echo "    GET  /health              (health check)"
echo ""
echo "  Test:"
echo "    curl -X POST -F 'file=@test.wav' http://${SERVER_IP}:${ASR_PORT}/transcribe"
echo ""
echo "  Service management:"
echo "    sudo systemctl status ${SERVICE_NAME}"
echo "    sudo systemctl restart ${SERVICE_NAME}"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo "============================================"
