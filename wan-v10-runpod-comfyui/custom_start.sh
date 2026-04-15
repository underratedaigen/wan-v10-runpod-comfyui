#!/usr/bin/env bash
set -euo pipefail

if [ -n "${PUBLIC_KEY:-}" ]; then
    mkdir -p ~/.ssh
    echo "$PUBLIC_KEY" > ~/.ssh/authorized_keys
    chmod 700 ~/.ssh
    chmod 600 ~/.ssh/authorized_keys

    for key_type in rsa ecdsa ed25519; do
        key_file="/etc/ssh/ssh_host_${key_type}_key"
        if [ ! -f "$key_file" ]; then
            ssh-keygen -t "$key_type" -f "$key_file" -q -N ''
        fi
    done

    service ssh start && echo "wan-v10-worker: SSH server started" || echo "wan-v10-worker: SSH server failed" >&2
fi

TCMALLOC="$(ldconfig -p | grep -Po "libtcmalloc.so.\d" | head -n 1 || true)"
if [ -n "$TCMALLOC" ]; then
    export LD_PRELOAD="${TCMALLOC}"
fi

echo "wan-v10-worker: checking GPU availability"
if ! GPU_CHECK=$(python3 -c "
import torch
try:
    torch.cuda.init()
    print(f'OK: {torch.cuda.get_device_name(0)}')
except Exception as exc:
    print(f'FAIL: {exc}')
    raise
" 2>&1); then
    echo "wan-v10-worker: GPU is not available. PyTorch CUDA init failed:"
    echo "wan-v10-worker: $GPU_CHECK"
    exit 1
fi
echo "wan-v10-worker: GPU available - $GPU_CHECK"

if command -v comfy-manager-set-mode >/dev/null 2>&1; then
    comfy-manager-set-mode offline || echo "wan-v10-worker: could not set ComfyUI-Manager network_mode" >&2
fi

echo "wan-v10-worker: bootstrapping WAN checkpoint"
python3 -u /bootstrap_models.py

COMFY_PID_FILE="/tmp/comfyui.pid"
: "${COMFY_LOG_LEVEL:=INFO}"
: "${COMFY_HOST:=127.0.0.1:8188}"
: "${COMFY_STARTUP_TIMEOUT_S:=300}"
: "${COMFY_STARTUP_POLL_INTERVAL_S:=2}"

echo "wan-v10-worker: starting ComfyUI"
if [ "${SERVE_API_LOCALLY:-false}" = "true" ]; then
    python -u /comfyui/main.py --disable-auto-launch --disable-metadata --listen --verbose "${COMFY_LOG_LEVEL}" --log-stdout &
    echo $! > "$COMFY_PID_FILE"
    echo "wan-v10-worker: waiting for ComfyUI readiness"
    python3 - <<'PY'
import os
import time
import urllib.error
import urllib.request

comfy_host = os.environ.get("COMFY_HOST", "127.0.0.1:8188")
timeout_s = float(os.environ.get("COMFY_STARTUP_TIMEOUT_S", "300"))
poll_interval_s = float(os.environ.get("COMFY_STARTUP_POLL_INTERVAL_S", "2"))
url = f"http://{comfy_host}/"
start = time.time()

while True:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if 200 <= response.status < 500:
                print(f"wan-v10-worker: ComfyUI ready after {time.time() - start:.1f}s")
                break
    except urllib.error.URLError:
        if time.time() - start >= timeout_s:
            raise SystemExit(f"wan-v10-worker: timed out waiting for ComfyUI after {timeout_s:.1f}s")
        time.sleep(poll_interval_s)
PY
    echo "wan-v10-worker: starting Runpod handler in local API mode"
    python -u /handler.py --rp_serve_api --rp_api_host=0.0.0.0
else
    python -u /comfyui/main.py --disable-auto-launch --disable-metadata --verbose "${COMFY_LOG_LEVEL}" --log-stdout &
    echo $! > "$COMFY_PID_FILE"
    echo "wan-v10-worker: waiting for ComfyUI readiness"
    python3 - <<'PY'
import os
import time
import urllib.error
import urllib.request

comfy_host = os.environ.get("COMFY_HOST", "127.0.0.1:8188")
timeout_s = float(os.environ.get("COMFY_STARTUP_TIMEOUT_S", "300"))
poll_interval_s = float(os.environ.get("COMFY_STARTUP_POLL_INTERVAL_S", "2"))
url = f"http://{comfy_host}/"
start = time.time()

while True:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if 200 <= response.status < 500:
                print(f"wan-v10-worker: ComfyUI ready after {time.time() - start:.1f}s")
                break
    except urllib.error.URLError:
        if time.time() - start >= timeout_s:
            raise SystemExit(f"wan-v10-worker: timed out waiting for ComfyUI after {timeout_s:.1f}s")
        time.sleep(poll_interval_s)
PY
    echo "wan-v10-worker: starting Runpod handler"
    python -u /handler.py
fi
