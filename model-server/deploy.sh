#!/bin/bash
# deploy.sh — Setup model download auth server on 192.168.223.5
# Target: /home/zhxg/zw/data/models
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Supports two modes:
#   - With nginx (recommended): auth + rate limit + static file serving
#   - Without nginx (standalone): auth_middleware.py serves files directly

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="model-auth"
INSTALL_DIR="/opt/model-auth"
MODELS_DIR="/home/zhxg/zw/data/models"
SERVER_IP="192.168.223.5"
HTTP_PORT=8080

echo "=== Model Download Auth Server Setup ==="
echo "  Server:     ${SERVER_IP}"
echo "  Models:     ${MODELS_DIR}"
echo "  Install to: ${INSTALL_DIR}"
echo ""

# 0. Check Python
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install it first."
    exit 1
fi
echo "Python: $(python3 --version)"

# 1. Create directories and copy files
echo ""
echo "[1/6] Installing files to ${INSTALL_DIR}..."
sudo mkdir -p ${INSTALL_DIR}/logs
sudo mkdir -p ${MODELS_DIR}
sudo cp "${SCRIPT_DIR}/auth_middleware.py" ${INSTALL_DIR}/
sudo chmod 644 ${INSTALL_DIR}/auth_middleware.py

# 2. Generate admin key
ADMIN_KEY=$(openssl rand -hex 16)
echo ""
echo "[2/6] Generated admin key: ${ADMIN_KEY}"
echo "  ⚠️  Save this! You need it to manage license keys."

# 3. Create systemd service
echo ""
echo "[3/6] Creating systemd service..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Model Download Auth Middleware
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=MODEL_ADMIN_KEY=${ADMIN_KEY}
Environment=MODEL_FILES_ROOT=${MODELS_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/auth_middleware.py serve
Restart=always
RestartSec=5

# Security hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl start ${SERVICE_NAME}
echo "  Service started: systemctl status ${SERVICE_NAME}"

# 4. Check and configure nginx
echo ""
echo "[4/6] Configuring nginx..."
HAS_NGINX=false
if command -v nginx &>/dev/null; then
    HAS_NGINX=true
    sudo cp "${SCRIPT_DIR}/nginx.conf" /etc/nginx/conf.d/model-server.conf
    
    # Test and reload
    if sudo nginx -t 2>/dev/null; then
        sudo systemctl reload nginx
        echo "  nginx configured on port ${HTTP_PORT}"
        echo "  Endpoint: http://${SERVER_IP}:${HTTP_PORT}"
    else
        echo "  ⚠️  nginx config test failed. Check: sudo nginx -t"
        echo "  Falling back to standalone mode (port 8901)"
        HAS_NGINX=false
    fi
else
    echo "  nginx not found. Using standalone mode."
    echo "  Install nginx for rate limiting and static file serving:"
    echo "    sudo apt install nginx"
    echo ""
    echo "  Standalone endpoint: http://${SERVER_IP}:8901"
fi

# 5. Verify models directory
echo ""
echo "[5/6] Checking models directory..."
if [ -d "${MODELS_DIR}" ]; then
    MODEL_COUNT=$(find ${MODELS_DIR} -name "*.onnx" -o -name "*.json" -o -name "*.mvn" -o -name "*.txt" 2>/dev/null | wc -l)
    if [ "$MODEL_COUNT" -gt 0 ]; then
        echo "  Found ${MODEL_COUNT} model files in ${MODELS_DIR}"
        echo "  Structure:"
        find ${MODELS_DIR} -type f | head -20 | sed 's/^/    /'
    else
        echo "  ⚠️  Models directory exists but is empty!"
        echo "  Place model files in:"
        echo "    ${MODELS_DIR}/vad/   (VAD model)"
        echo "    ${MODELS_DIR}/asr/   (ASR model)"
        echo "    ${MODELS_DIR}/punc/  (Punctuation model)"
    fi
else
    echo "  ⚠️  Models directory not found. Creating..."
    sudo mkdir -p ${MODELS_DIR}/{vad,asr,punc}
    echo "  Place model files in ${MODELS_DIR}/{vad,asr,punc}/"
fi

# 6. Create first license key
echo ""
echo "[6/6] Creating first license key..."
LICENSE_KEY=$(MODEL_ADMIN_KEY=${ADMIN_KEY} MODEL_FILES_ROOT=${MODELS_DIR} python3 -c "
import sys; sys.path.insert(0, '${INSTALL_DIR}')
from auth_middleware import create_license_key, init_db
init_db()
k = create_license_key(label='initial', expire_days=365, max_downloads=100)
print(k)
")

# Determine endpoint
if [ "$HAS_NGINX" = true ]; then
    ENDPOINT="http://${SERVER_IP}:${HTTP_PORT}"
else
    ENDPOINT="http://${SERVER_IP}:8901"
fi

echo ""
echo "============================================"
echo "  ✅ Setup complete!"
echo ""
echo "  Admin key:     ${ADMIN_KEY}"
echo "  License key:   ${LICENSE_KEY}"
echo "  Models dir:    ${MODELS_DIR}"
echo "  Endpoint:      ${ENDPOINT}"
echo ""
echo "  Service management:"
echo "    sudo systemctl status ${SERVICE_NAME}"
echo "    sudo systemctl restart ${SERVICE_NAME}"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "  Client license.json:"
echo '  {'
echo "    \"license_key\": \"${LICENSE_KEY}\","
echo "    \"endpoint\": \"${ENDPOINT}\""
echo '  }'
echo ""
echo "  Admin API:"
echo "    curl -H 'Authorization: Bearer ${ADMIN_KEY}' http://127.0.0.1:8901/admin/tokens"
echo "    curl -H 'Authorization: Bearer ${ADMIN_KEY}' http://127.0.0.1:8901/admin/stats"
echo "============================================"
