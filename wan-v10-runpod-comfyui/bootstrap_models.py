import os
from pathlib import Path

import requests


DEFAULT_CHECKPOINT_NAME = "wan2.2-i2v-rapid-aio-v10-nsfw.safetensors"
DEFAULT_CHECKPOINT_URL = (
    "https://huggingface.co/Phr00t/WAN2.2-14B-Rapid-AllInOne/resolve/main/"
    "v10/wan2.2-i2v-rapid-aio-v10-nsfw.safetensors"
)
DEFAULT_CHECKPOINT_SIZE = 23387046339
NETWORK_VOLUME_DIR = Path("/runpod-volume/models/checkpoints")
LOCAL_MODEL_DIR = Path("/comfyui/models/checkpoints")
CHUNK_SIZE = 16 * 1024 * 1024


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def choose_model_dir() -> Path:
    configured = os.environ.get("WAN_MODEL_DIR", "").strip()
    if configured:
        return Path(configured)
    if Path("/runpod-volume").exists():
        return NETWORK_VOLUME_DIR
    return LOCAL_MODEL_DIR


def checkpoint_is_ready(path: Path, expected_size: int | None) -> bool:
    if not path.exists():
        return False
    if expected_size and path.stat().st_size != expected_size:
        print(
            f"[bootstrap] existing checkpoint size mismatch for {path.name}: "
            f"{path.stat().st_size} != {expected_size}",
            flush=True,
        )
        return False
    return True


def download_checkpoint(url: str, destination: Path, token: str | None) -> None:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    if partial.exists():
        partial.unlink()

    print(f"[bootstrap] downloading checkpoint to {destination}", flush=True)
    with requests.get(url, headers=headers, stream=True, timeout=120) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length", "0") or "0")
        downloaded = 0
        last_reported_gb = -1

        with open(partial, "wb") as handle:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)

                current_gb = downloaded // (1024**3)
                if current_gb != last_reported_gb:
                    last_reported_gb = current_gb
                    if total > 0:
                        pct = downloaded * 100 / total
                        print(
                            f"[bootstrap] downloaded {downloaded / (1024**3):.1f} GB / "
                            f"{total / (1024**3):.1f} GB ({pct:.1f}%)",
                            flush=True,
                        )
                    else:
                        print(
                            f"[bootstrap] downloaded {downloaded / (1024**3):.1f} GB",
                            flush=True,
                        )

    partial.replace(destination)
    print(f"[bootstrap] checkpoint ready: {destination}", flush=True)


def main() -> int:
    if env_flag("WAN_SKIP_MODEL_DOWNLOAD", default=False):
        print("[bootstrap] WAN_SKIP_MODEL_DOWNLOAD=true, skipping checkpoint bootstrap", flush=True)
        return 0

    checkpoint_name = os.environ.get("WAN_CHECKPOINT_NAME", DEFAULT_CHECKPOINT_NAME).strip()
    checkpoint_url = os.environ.get("WAN_CHECKPOINT_URL", DEFAULT_CHECKPOINT_URL).strip()
    expected_size_raw = os.environ.get("WAN_CHECKPOINT_SIZE", str(DEFAULT_CHECKPOINT_SIZE)).strip()
    expected_size = int(expected_size_raw) if expected_size_raw else None
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")

    model_dir = choose_model_dir()
    checkpoint_path = model_dir / checkpoint_name

    if checkpoint_is_ready(checkpoint_path, expected_size):
        print(f"[bootstrap] checkpoint already present: {checkpoint_path}", flush=True)
        return 0

    if checkpoint_path.exists():
        checkpoint_path.unlink()

    try:
        download_checkpoint(checkpoint_url, checkpoint_path, token)
    except Exception as exc:
        print(f"[bootstrap] failed to download checkpoint: {exc}", flush=True)
        return 1

    if not checkpoint_is_ready(checkpoint_path, expected_size):
        print("[bootstrap] checkpoint download completed but validation failed", flush=True)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
