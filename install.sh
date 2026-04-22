#!/usr/bin/env bash
# AetherLynk Pi Agent installer / updater
# Usage: curl -sSL https://raw.githubusercontent.com/ArcReactorKC/aetherlynk-pi-agent/main/install.sh | sudo bash

set -euo pipefail

RAW_BASE="https://raw.githubusercontent.com/ArcReactorKC/aetherlynk-pi-agent/main"
INSTALL_DIR="/opt/aetherlynk"
VENV_DIR="${INSTALL_DIR}/venv"
SERVICE_NAME="aetherlynk"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LOG_FILE="/var/log/aetherlynk/agent.log"

# --- Root check ---
if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: This script must be run as root (use sudo)." >&2
    exit 1
fi

echo "==> Installing AetherLynk Pi Agent..."

# --- System dependencies ---
echo "==> Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3-pip python3-venv curl

# --- Download latest files ---
echo "==> Downloading latest agent files from GitHub..."
mkdir -p "${INSTALL_DIR}"
curl -sSfL "${RAW_BASE}/aetherlynk_agent.py"   -o "${INSTALL_DIR}/aetherlynk_agent.py"
curl -sSfL "${RAW_BASE}/requirements.txt"       -o "${INSTALL_DIR}/requirements.txt"
curl -sSfL "${RAW_BASE}/aetherlynk.service"     -o "${SERVICE_FILE}"

# --- Virtual environment ---
echo "==> Creating Python virtual environment..."
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

# --- Directories ---
echo "==> Creating runtime directories..."
mkdir -p /etc/aetherlynk
mkdir -p /var/log/aetherlynk

# --- Systemd ---
echo "==> Enabling and starting service..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

if systemctl is-active --quiet "${SERVICE_NAME}"; then
    systemctl restart "${SERVICE_NAME}"
    echo "==> Service restarted."
else
    systemctl start "${SERVICE_NAME}"
    echo "==> Service started."
fi

# --- Tail log so installer sees the device key ---
echo ""
echo "==> Installation complete. Tailing log for 15 seconds..."
echo "    (The Device Key will appear below — write it on the device label)"
echo ""
sleep 2
timeout 15 tail -n 40 -f "${LOG_FILE}" || true

echo ""
echo "==> Done. If you missed the Device Key, run:"
echo "    sudo journalctl -u ${SERVICE_NAME} --no-pager | grep 'Device Key'"
