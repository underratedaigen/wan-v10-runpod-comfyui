import base64
import logging
import mimetypes
import os
import tempfile
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
import runpod
from PIL import Image, ImageOps
from runpod.serverless.utils import rp_upload

from workflow_builder import build_workflow, coerce_seed, resolve_generation_dimensions


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("wan-v10-worker")

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1:8188")
COMFY_HISTORY_TIMEOUT_S = int(os.environ.get("COMFY_HISTORY_TIMEOUT_S", "3600"))
COMFY_POLL_INTERVAL_S = int(os.environ.get("COMFY_POLL_INTERVAL_S", "5"))
COMFY_STARTUP_TIMEOUT_S = int(os.environ.get("COMFY_STARTUP_TIMEOUT_S", "300"))
COMFY_STARTUP_POLL_INTERVAL_S = float(os.environ.get("COMFY_STARTUP_POLL_INTERVAL_S", "2"))
WORKFLOW_TEMPLATE = Path("/workflow_templates/wan_v10_i2v.json")
COMFY_OUTPUT_DIR = Path("/comfyui/output")
COMFY_TEMP_DIR = Path("/comfyui/temp")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def comfy_url(path: str) -> str:
    return f"http://{COMFY_HOST}{path}"


def check_server() -> None:
    response = requests.get(comfy_url("/"), timeout=10)
    response.raise_for_status()


def wait_for_server() -> None:
    start = time.time()
    last_error: Exception | None = None

    while True:
        try:
            check_server()
            elapsed = time.time() - start
            LOGGER.info("ComfyUI is ready after %.1fs", elapsed)
            return
        except requests.RequestException as exc:
            last_error = exc
            elapsed = time.time() - start
            if elapsed >= COMFY_STARTUP_TIMEOUT_S:
                raise TimeoutError(
                    f"Timed out waiting for ComfyUI after {COMFY_STARTUP_TIMEOUT_S}s."
                ) from exc

            LOGGER.info(
                "Waiting for ComfyUI to start on %s... %.1fs elapsed",
                COMFY_HOST,
                elapsed,
            )
            time.sleep(COMFY_STARTUP_POLL_INTERVAL_S)


def strip_data_uri(data: str) -> str:
    if "," in data and data.split(",", 1)[0].startswith("data:"):
        return data.split(",", 1)[1]
    return data


def image_source_to_png_bytes(job_input: dict[str, Any]) -> tuple[bytes, int, int]:
    image_value = job_input.get("image")
    image_base64 = job_input.get("image_base64")
    image_url = job_input.get("image_url")

    raw_bytes: bytes | None = None
    if image_base64:
        raw_bytes = base64.b64decode(strip_data_uri(image_base64))
    elif image_url:
        response = requests.get(image_url, timeout=120)
        response.raise_for_status()
        raw_bytes = response.content
    elif isinstance(image_value, str):
        if image_value.startswith(("http://", "https://")):
            response = requests.get(image_value, timeout=120)
            response.raise_for_status()
            raw_bytes = response.content
        else:
            raw_bytes = base64.b64decode(strip_data_uri(image_value))

    if raw_bytes is None:
        raise ValueError("Provide one of 'image', 'image_base64', or 'image_url'.")

    with Image.open(BytesIO(raw_bytes)) as source:
        source = ImageOps.exif_transpose(source).convert("RGB")
        width, height = source.size
        output = BytesIO()
        source.save(output, format="PNG", optimize=True)
        return output.getvalue(), width, height


def upload_input_image(image_bytes: bytes, filename: str) -> None:
    files = {
        "image": (filename, BytesIO(image_bytes), "image/png"),
        "overwrite": (None, "true"),
    }
    response = requests.post(comfy_url("/upload/image"), files=files, timeout=120)
    response.raise_for_status()


def queue_workflow(workflow: dict[str, Any]) -> str:
    payload = {"prompt": workflow, "client_id": str(uuid.uuid4())}
    response = requests.post(comfy_url("/prompt"), json=payload, timeout=60)
    if response.status_code == 400:
        raise ValueError(f"ComfyUI validation failed: {response.text}")
    response.raise_for_status()
    data = response.json()
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise ValueError(f"Missing prompt_id in ComfyUI response: {data}")
    return prompt_id


def wait_for_history(prompt_id: str) -> dict[str, Any]:
    start = time.time()
    while True:
        response = requests.get(comfy_url(f"/history/{prompt_id}"), timeout=30)
        response.raise_for_status()
        history = response.json()
        if prompt_id in history:
            return history[prompt_id]

        elapsed = int(time.time() - start)
        if elapsed >= COMFY_HISTORY_TIMEOUT_S:
            raise TimeoutError(
                f"Timed out waiting for prompt {prompt_id} after {COMFY_HISTORY_TIMEOUT_S}s."
            )

        if elapsed % max(COMFY_POLL_INTERVAL_S, 5) == 0:
            LOGGER.info("Waiting for prompt %s... %ss elapsed", prompt_id, elapsed)
        time.sleep(COMFY_POLL_INTERVAL_S)


def resolve_output_path(filename: str, subfolder: str, output_type: str) -> Path:
    base_dir = COMFY_OUTPUT_DIR if output_type == "output" else COMFY_TEMP_DIR
    return base_dir / subfolder / filename if subfolder else base_dir / filename


def fetch_output_bytes(filename: str, subfolder: str, output_type: str) -> bytes:
    path = resolve_output_path(filename, subfolder, output_type)
    if path.exists():
        return path.read_bytes()

    params = {"filename": filename, "subfolder": subfolder, "type": output_type}
    response = requests.get(comfy_url("/view"), params=params, timeout=120)
    response.raise_for_status()
    return response.content


def bucket_upload_enabled(job_input: dict[str, Any]) -> bool:
    if job_input.get("upload_to_bucket") is False:
        return False
    return bool(os.environ.get("BUCKET_ENDPOINT_URL"))


def upload_file_to_bucket(job_id: str, filename: str, payload: bytes) -> str:
    suffix = Path(filename).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
        temp_file.write(payload)
        temp_path = temp_file.name
    try:
        return rp_upload.upload_image(job_id, temp_path)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def serialize_output(job_id: str, filename: str, payload: bytes, to_bucket: bool) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    if to_bucket:
        return {
            "filename": filename,
            "type": "bucket_url",
            "data": upload_file_to_bucket(job_id, filename, payload),
            "mime_type": mime_type,
        }

    return {
        "filename": filename,
        "type": "base64",
        "data": base64.b64encode(payload).decode("utf-8"),
        "mime_type": mime_type,
    }


def parse_outputs(job_id: str, prompt_history: dict[str, Any], upload_to_bucket: bool) -> dict[str, Any]:
    outputs = prompt_history.get("outputs", {})
    videos: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    for node_output in outputs.values():
        for item in node_output.get("images", []):
            filename = item.get("filename")
            if not filename:
                continue
            output_type = item.get("type", "output")
            subfolder = item.get("subfolder", "")
            payload = fetch_output_bytes(filename, subfolder, output_type)
            serialized = serialize_output(job_id, filename, payload, upload_to_bucket)
            ext = Path(filename).suffix.lower()
            if ext in VIDEO_EXTENSIONS:
                videos.append(serialized)
            elif ext in IMAGE_EXTENSIONS:
                images.append(serialized)
            else:
                files.append(serialized)

    return {"videos": videos, "images": images, "files": files}


def get_default(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback).strip()


def validate_input(job_input: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job_input, dict):
        raise ValueError("Job input must be an object.")

    prompt = str(job_input.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("Missing required 'prompt'.")

    has_image_source = any(job_input.get(key) for key in ("image", "image_base64", "image_url"))
    if not has_image_source:
        raise ValueError("Provide an input image using 'image', 'image_base64', or 'image_url'.")

    return job_input


def handle_job(job: dict[str, Any]) -> dict[str, Any]:
    job_input = validate_input(job.get("input", {}))
    job_id = job.get("id", str(uuid.uuid4()))

    wait_for_server()

    image_bytes, image_width, image_height = image_source_to_png_bytes(job_input)
    input_filename = f"wan_input_{job_id}.png"
    upload_input_image(image_bytes, input_filename)

    width, height = resolve_generation_dimensions(
        original_width=image_width,
        original_height=image_height,
        width=job_input.get("width"),
        height=job_input.get("height"),
        resolution_preset=str(
            job_input.get(
                "resolution_preset",
                get_default("WAN_DEFAULT_RESOLUTION_PRESET", "720p"),
            )
        ).strip().lower(),
    )

    workflow = build_workflow(
        template_path=WORKFLOW_TEMPLATE,
        input_image_name=input_filename,
        checkpoint_name=str(
            job_input.get(
                "checkpoint_name",
                get_default("WAN_CHECKPOINT_NAME", "wan2.2-i2v-rapid-aio-v10-nsfw.safetensors"),
            )
        ),
        prompt=str(job_input["prompt"]),
        negative_prompt=str(job_input.get("negative_prompt", "")),
        width=width,
        height=height,
        num_frames=job_input.get("num_frames", get_default("WAN_DEFAULT_NUM_FRAMES", "81")),
        fps=job_input.get("fps", get_default("WAN_DEFAULT_FPS", "16")),
        steps=job_input.get("steps", get_default("WAN_DEFAULT_STEPS", "4")),
        cfg=job_input.get("cfg", get_default("WAN_DEFAULT_CFG", "1.0")),
        sampler_name=str(
            job_input.get(
                "sampler_name",
                get_default("WAN_DEFAULT_SAMPLER", "euler_ancestral"),
            )
        ),
        scheduler=str(
            job_input.get(
                "scheduler",
                get_default("WAN_DEFAULT_SCHEDULER", "beta"),
            )
        ),
        denoise=job_input.get("denoise", get_default("WAN_DEFAULT_DENOISE", "1.0")),
        shift=job_input.get("shift", get_default("WAN_DEFAULT_SHIFT", "5.0")),
        seed=coerce_seed(job_input.get("seed")),
        filename_prefix=str(
            job_input.get(
                "filename_prefix",
                get_default("WAN_DEFAULT_FILENAME_PREFIX", "wan-v10/wan_i2v"),
            )
        ),
    )

    prompt_id = queue_workflow(workflow)
    LOGGER.info("Queued prompt %s", prompt_id)
    prompt_history = wait_for_history(prompt_id)
    parsed_outputs = parse_outputs(job_id, prompt_history, bucket_upload_enabled(job_input))

    if not parsed_outputs["videos"] and not parsed_outputs["images"] and not parsed_outputs["files"]:
        raise ValueError(f"No outputs found for prompt {prompt_id}.")

    return {
        "prompt_id": prompt_id,
        "model": {"checkpoint_name": workflow["2"]["inputs"]["ckpt_name"]},
        "input_image": {"width": image_width, "height": image_height},
        "generation": {
            "width": width,
            "height": height,
            "num_frames": workflow["6"]["inputs"]["length"],
            "fps": workflow["9"]["inputs"]["fps"],
            "steps": workflow["7"]["inputs"]["steps"],
            "cfg": workflow["7"]["inputs"]["cfg"],
            "sampler_name": workflow["7"]["inputs"]["sampler_name"],
            "scheduler": workflow["7"]["inputs"]["scheduler"],
            "shift": workflow["3"]["inputs"]["shift"],
            "seed": workflow["7"]["inputs"]["seed"]
        },
        "videos": parsed_outputs["videos"],
        "images": parsed_outputs["images"],
        "files": parsed_outputs["files"]
    }


runpod.serverless.start({"handler": handle_job})
