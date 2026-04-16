#!/bin/bash
# install_worker.sh — einmaliges Setup auf jedem Mac Studio
#
# Installiert Miniforge, erstellt conda-Env, lädt Molmo-Modell,
# legt einen macOS LaunchAgent an (autostart bei Login).
#
# Usage:
#   bash install_worker.sh --coordinator http://10.0.0.1:5000 [--worker-name mac-studio-1]
#
# Oder per SSH von einem anderen Rechner aus:
#   ssh administrator@10.0.0.x "bash <(curl -fsSL http://10.0.0.1:5000/install_worker.sh) \
#       --coordinator http://10.0.0.1:5000 --worker-name mac-studio-1"

set -euo pipefail

# ── Argumente ─────────────────────────────────────────────────────────────────

COORDINATOR=""
WORKER_NAME="$(hostname -s)"
WORKER_DIR="$HOME/caption-worker"
CONDA_ENV="caption-worker"
PLIST_LABEL="com.pd.caption-worker"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --coordinator)  COORDINATOR="$2";  shift 2 ;;
        --worker-name)  WORKER_NAME="$2";  shift 2 ;;
        --worker-dir)   WORKER_DIR="$2";   shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [[ -z "$COORDINATOR" ]]; then
    echo "Error: --coordinator required (e.g. http://10.0.0.1:5000)"
    exit 1
fi

echo "=== Distributed Caption Worker Setup ==="
echo "Coordinator : $COORDINATOR"
echo "Worker name : $WORKER_NAME"
echo "Worker dir  : $WORKER_DIR"
echo "========================================"

mkdir -p "$WORKER_DIR"

# ── 1. Miniforge ──────────────────────────────────────────────────────────────

MINIFORGE_PATH="$HOME/miniforge3"

if [[ ! -d "$MINIFORGE_PATH" ]]; then
    echo "Installing Miniforge3 …"
    ARCH="$(uname -m)"
    if [[ "$ARCH" == "arm64" ]]; then
        INSTALLER="Miniforge3-MacOSX-arm64.sh"
    else
        INSTALLER="Miniforge3-MacOSX-x86_64.sh"
    fi
    curl -fsSL "https://github.com/conda-forge/miniforge/releases/latest/download/$INSTALLER" \
        -o "/tmp/$INSTALLER"
    bash "/tmp/$INSTALLER" -b -p "$MINIFORGE_PATH"
    rm "/tmp/$INSTALLER"
else
    echo "Miniforge3 already installed."
fi

CONDA="$MINIFORGE_PATH/bin/conda"
PYTHON="$MINIFORGE_PATH/envs/$CONDA_ENV/bin/python"

# ── 2. Conda environment ──────────────────────────────────────────────────────

if ! "$CONDA" env list | grep -q "^$CONDA_ENV "; then
    echo "Creating conda env '$CONDA_ENV' …"
    "$CONDA" create -n "$CONDA_ENV" python=3.11 -y
else
    echo "Conda env '$CONDA_ENV' already exists."
fi

# Install dependencies
echo "Installing Python dependencies …"
"$MINIFORGE_PATH/envs/$CONDA_ENV/bin/pip" install --quiet \
    torch torchvision \
    transformers>=4.40.0 \
    accelerate \
    einops \
    requests \
    tqdm \
    pyarrow \
    pillow \
    rumps

echo "Dependencies installed."

# ── 3. Copy worker scripts ────────────────────────────────────────────────────

cp "$(dirname "$0")/worker.py"      "$WORKER_DIR/worker.py"
cp "$(dirname "$0")/menubar_app.py" "$WORKER_DIR/menubar_app.py"
echo "Worker scripts copied to $WORKER_DIR/"

# ── 4. Pre-download Molmo model (optional but recommended) ───────────────────

echo "Pre-downloading Molmo-7B-D-0924 model weights (~14 GB) …"
echo "(This may take a while on first run. Skip with Ctrl+C — worker will download on first task.)"

"$PYTHON" - <<'EOF'
import os
from transformers import AutoProcessor, AutoModelForCausalLM
MODEL_ID = "allenai/Molmo-7B-D-0924"
print("Downloading processor …")
AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
print("Downloading model weights …")
AutoModelForCausalLM.from_pretrained(MODEL_ID, trust_remote_code=True,
                                     torch_dtype="auto")
print("Model cached.")
EOF

# ── 5. LaunchAgent (autostart on login) ──────────────────────────────────────

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST_LABEL.plist"
LOG_DIR="$WORKER_DIR/logs"

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$WORKER_DIR/worker.py</string>
        <string>--coordinator</string>
        <string>$COORDINATOR</string>
        <string>--worker-name</string>
        <string>$WORKER_NAME</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$WORKER_DIR</string>

    <!-- Restart automatically if worker crashes -->
    <key>KeepAlive</key>
    <true/>

    <!-- Start when user logs in -->
    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/worker.log</string>

    <key>StandardErrorPath</key>
    <string>$LOG_DIR/worker.log</string>

    <!-- Throttle restarts to avoid tight crash loops -->
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
PLIST

# Load (or reload) the worker LaunchAgent
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load   "$PLIST_PATH"

# ── 6. Menu Bar App LaunchAgent ───────────────────────────────────────────────

MENUBAR_PLIST_LABEL="com.pd.caption-menubar"
MENUBAR_PLIST_PATH="$LAUNCH_AGENTS_DIR/$MENUBAR_PLIST_LABEL.plist"

cat > "$MENUBAR_PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$MENUBAR_PLIST_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$WORKER_DIR/menubar_app.py</string>
        <string>--coordinator</string>
        <string>$COORDINATOR</string>
        <string>--worker-name</string>
        <string>$WORKER_NAME</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$WORKER_DIR</string>

    <key>KeepAlive</key>
    <true/>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/menubar.log</string>

    <key>StandardErrorPath</key>
    <string>$LOG_DIR/menubar.log</string>

    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
PLIST

launchctl unload "$MENUBAR_PLIST_PATH" 2>/dev/null || true
launchctl load   "$MENUBAR_PLIST_PATH"

echo ""
echo "=== Setup complete ==="
echo "Worker '$WORKER_NAME' will start automatically on login."
echo "Menu bar icon appears in the top-right corner of the screen."
echo "Coordinator: $COORDINATOR"
echo "Logs: $LOG_DIR/worker.log"
echo ""
echo "To check status:  tail -f $LOG_DIR/worker.log"
echo "To stop worker:   launchctl unload $PLIST_PATH"
echo "To start worker:  launchctl load   $PLIST_PATH"
