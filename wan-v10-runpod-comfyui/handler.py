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
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
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
DEFAULT_FRAMING_MODE = os.environ.get("WAN_DEFAULT_FRAMING_MODE", "off").strip().lower()
DEFAULT_CAMERA_MOTION_MODE = os.environ.get("WAN_DEFAULT_CAMERA_MOTION_MODE", "off").strip().lower()
DEFAULT_SUBJECT_SCALE = float(os.environ.get("WAN_DEFAULT_SUBJECT_SCALE", "1.0"))
DEFAULT_VERTICAL_BIAS = float(os.environ.get("WAN_DEFAULT_VERTICAL_BIAS", "0.0"))
DEFAULT_BG_BLUR_RADIUS = float(os.environ.get("WAN_DEFAULT_BG_BLUR_RADIUS", "10"))
DEFAULT_BG_DARKEN = float(os.environ.get("WAN_DEFAULT_BG_DARKEN", "0.96"))
DEFAULT_FOREGROUND_SHARPNESS = float(os.environ.get("WAN_DEFAULT_FOREGROUND_SHARPNESS", "1.15"))

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
FRAMING_POSITIVE_SUFFIX = (
    " Keep the subject fully in frame, with the head and hair fully visible at all times,"
    " stable portrait composition, ample headroom, gentle camera movement, and no aggressive push-in."
)
FRAMING_NEGATIVE_SUFFIX = (
    " cropped head, cut off forehead, cut off chin, face out of frame, hair out of frame,"
    " extreme close-up, sudden zoom-in, unstable framing, off-center face, cropped portrait"
)
LOCKED_CAMERA_POSITIVE_SUFFIX = (
    " Use a locked camera with fixed framing and consistent subject size in every frame."
    " No push-in, no zoom, no dolly-in, no crash zoom, and no creeping tighter composition."
)
LOCKED_CAMERA_NEGATIVE_SUFFIX = (
    " zoom in, push-in, dolly-in, crash zoom, camera creep, tighter framing over time,"
    " changing focal length, progressive close-up, camera moves toward subject"
)
GENTLE_CAMERA_POSITIVE_SUFFIX = (
    " Keep camera motion minimal and preserve nearly fixed framing,"
    " with no unrequested zoom or push-in."
)
GENTLE_CAMERA_NEGATIVE_SUFFIX = (
    " unrequested zoom in, unrequested push-in, sudden dolly-in, crash zoom"
)
LOCKED_HARD_CAMERA_POSITIVE_SUFFIX = (
    " Maintain exactly the same camera distance and portrait framing across the whole clip."
    " The subject must stay the same size from start to finish with no zoom, no push-in,"
    " no dolly-in, no gradual tightening, and no camera drift toward the face."
)
LOCKED_HARD_CAMERA_NEGATIVE_SUFFIX = (
    " zoom in, push-in, dolly-in, crash zoom, creeping zoom, tighter framing over time,"
    " camera drift toward face, face getting larger, progressive close-up, changing subject scale,"
    " tightening portrait crop, moving camera closer, lens breathing"
)
EXPLICIT_ZOOM_TERMS = (
    "zoom",
    "push-in",
    "push in",
    "dolly-in",
    "dolly in",
    "close-up",
    "close up",
    "extreme close-up",
    "extreme close up",
    "crash zoom",
    "camera moves toward",
    "camera move toward",
    "moves toward the camera",
    "toward the camera",
)
RESAMPLING_LANCZOS = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS


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


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_float(value: Any, default: float) -> float:
    if value is None:
        return default
    return float(value)


def framing_mode_for(job_input: dict[str, Any]) -> str:
    framing_mode = str(job_input.get("framing_mode", DEFAULT_FRAMING_MODE)).strip().lower()
    if framing_mode in {"", "default"}:
        return DEFAULT_FRAMING_MODE
    return framing_mode


def camera_motion_mode_for(job_input: dict[str, Any]) -> str:
    camera_motion_mode = str(
        job_input.get("camera_motion_mode", DEFAULT_CAMERA_MOTION_MODE)
    ).strip().lower()
    if camera_motion_mode in {"", "default"}:
        return DEFAULT_CAMERA_MOTION_MODE
    return camera_motion_mode


def prompt_requests_zoom(prompt: str) -> bool:
    prompt_lower = prompt.lower()
    return any(term in prompt_lower for term in EXPLICIT_ZOOM_TERMS)


def should_apply_framing(job_input: dict[str, Any]) -> bool:
    framing_mode = framing_mode_for(job_input)
    if framing_mode in {"off", "none", "disabled"}:
        return False
    return parse_bool(job_input.get("keep_head_in_frame"), True)


def augment_prompt_for_framing(prompt: str, negative_prompt: str, job_input: dict[str, Any]) -> tuple[str, str]:
    updated_prompt = prompt.rstrip()
    updated_negative = negative_prompt.strip()
    explicit_zoom_requested = prompt_requests_zoom(updated_prompt)

    if should_apply_framing(job_input):
        if FRAMING_POSITIVE_SUFFIX.strip() not in updated_prompt:
            updated_prompt += FRAMING_POSITIVE_SUFFIX
        if FRAMING_NEGATIVE_SUFFIX not in updated_negative:
            updated_negative = (
                f"{updated_negative}, {FRAMING_NEGATIVE_SUFFIX}"
                if updated_negative
                else FRAMING_NEGATIVE_SUFFIX
            )

    camera_motion_mode = camera_motion_mode_for(job_input)
    if explicit_zoom_requested and camera_motion_mode in {"locked", "locked_hard"}:
        camera_motion_mode = "gentle"

    if camera_motion_mode == "locked_hard":
        if LOCKED_HARD_CAMERA_POSITIVE_SUFFIX.strip() not in updated_prompt:
            updated_prompt += LOCKED_HARD_CAMERA_POSITIVE_SUFFIX
        if LOCKED_HARD_CAMERA_NEGATIVE_SUFFIX not in updated_negative:
            updated_negative = (
                f"{updated_negative}, {LOCKED_HARD_CAMERA_NEGATIVE_SUFFIX}"
                if updated_negative
                else LOCKED_HARD_CAMERA_NEGATIVE_SUFFIX
            )
    elif camera_motion_mode == "locked":
        if LOCKED_CAMERA_POSITIVE_SUFFIX.strip() not in updated_prompt:
            updated_prompt += LOCKED_CAMERA_POSITIVE_SUFFIX
        if LOCKED_CAMERA_NEGATIVE_SUFFIX not in updated_negative:
            updated_negative = (
                f"{updated_negative}, {LOCKED_CAMERA_NEGATIVE_SUFFIX}"
                if updated_negative
                else LOCKED_CAMERA_NEGATIVE_SUFFIX
            )
    elif camera_motion_mode in {"gentle", "balanced"}:
        if GENTLE_CAMERA_POSITIVE_SUFFIX.strip() not in updated_prompt:
            updated_prompt += GENTLE_CAMERA_POSITIVE_SUFFIX
        if GENTLE_CAMERA_NEGATIVE_SUFFIX not in updated_negative:
            updated_negative = (
                f"{updated_negative}, {GENTLE_CAMERA_NEGATIVE_SUFFIX}"
                if updated_negative
                else GENTLE_CAMERA_NEGATIVE_SUFFIX
            )

    return updated_prompt.strip(), updated_negative.strip()


def load_source_image(job_input: dict[str, Any]) -> Image.Image:
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
        return ImageOps.exif_transpose(source).convert("RGB")


def feather_mask(size: tuple[int, int], blur_radius: float) -> Image.Image:
    mask = Image.new("L", size, color=255)
    if blur_radius <= 0:
        return mask
    return mask.filter(ImageFilter.GaussianBlur(radius=max(1.0, blur_radius)))


def apply_input_framing(source: Image.Image, job_input: dict[str, Any]) -> tuple[Image.Image, dict[str, Any]]:
    framing_mode = framing_mode_for(job_input)
    camera_motion_mode = camera_motion_mode_for(job_input)
    explicit_zoom_requested = prompt_requests_zoom(str(job_input.get("prompt", "")))
    if not should_apply_framing(job_input):
        return source, {
            "mode": framing_mode,
            "camera_motion_mode": camera_motion_mode,
            "explicit_zoom_requested": explicit_zoom_requested,
            "enabled": False,
            "subject_scale": 1.0,
            "vertical_bias": 0.0,
        }

    width, height = source.size
    subject_scale = clamp(parse_float(job_input.get("subject_scale"), DEFAULT_SUBJECT_SCALE), 0.72, 0.98)
    vertical_bias = clamp(parse_float(job_input.get("vertical_bias"), DEFAULT_VERTICAL_BIAS), -0.2, 0.2)
    blur_radius = clamp(parse_float(job_input.get("background_blur_radius"), DEFAULT_BG_BLUR_RADIUS), 0.0, 64.0)
    darken = clamp(parse_float(job_input.get("background_darken"), DEFAULT_BG_DARKEN), 0.5, 1.2)
    foreground_sharpness = clamp(
        parse_float(job_input.get("foreground_sharpness"), DEFAULT_FOREGROUND_SHARPNESS),
        0.5,
        2.0,
    )

    if framing_mode == "balanced":
        subject_scale = min(0.93, max(subject_scale, 0.9))
        vertical_bias = max(vertical_bias, 0.03)
    elif framing_mode in {"strict", "keep_head_in_frame"}:
        subject_scale = min(subject_scale, 0.88)
        vertical_bias = max(vertical_bias, 0.06)

    if not explicit_zoom_requested and camera_motion_mode == "locked_hard":
        subject_scale = min(subject_scale, 0.78)
        vertical_bias = max(vertical_bias, 0.10)
        blur_radius = max(blur_radius, 10.0)
    elif not explicit_zoom_requested and camera_motion_mode == "locked":
        subject_scale = min(subject_scale, 0.82)
        vertical_bias = max(vertical_bias, 0.09)
    elif camera_motion_mode in {"gentle", "balanced"}:
        subject_scale = min(subject_scale, 0.86)
        vertical_bias = max(vertical_bias, 0.07)

    fg_width = max(16, int(round(width * subject_scale)))
    fg_height = max(16, int(round(height * subject_scale)))
    foreground = source.resize((fg_width, fg_height), RESAMPLING_LANCZOS)
    if abs(foreground_sharpness - 1.0) > 0.01:
        foreground = ImageEnhance.Sharpness(foreground).enhance(foreground_sharpness)

    background = source.resize((width, height), RESAMPLING_LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    if abs(darken - 1.0) > 0.01:
        background = ImageEnhance.Brightness(background).enhance(darken)

    canvas = background.copy()
    x = int(round((width - fg_width) / 2))
    y = int(round((height - fg_height) / 2 + (vertical_bias * height)))
    x = max(0, min(width - fg_width, x))
    y = max(0, min(height - fg_height, y))
    mask = feather_mask((fg_width, fg_height), blur_radius=max(4.0, min(fg_width, fg_height) * 0.01))
    canvas.paste(foreground, (x, y), mask)

    return canvas, {
        "mode": framing_mode,
        "camera_motion_mode": camera_motion_mode,
        "explicit_zoom_requested": explicit_zoom_requested,
        "enabled": True,
        "subject_scale": round(subject_scale, 4),
        "vertical_bias": round(vertical_bias, 4),
        "background_blur_radius": round(blur_radius, 2),
        "background_darken": round(darken, 3),
        "foreground_sharpness": round(foreground_sharpness, 3),
    }


def image_to_png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


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

    source_image = load_source_image(job_input)
    image_width, image_height = source_image.size
    prepared_image, framing_settings = apply_input_framing(source_image, job_input)
    prepared_width, prepared_height = prepared_image.size
    image_bytes = image_to_png_bytes(prepared_image)
    input_filename = f"wan_input_{job_id}.png"
    upload_input_image(image_bytes, input_filename)

    prompt, negative_prompt = augment_prompt_for_framing(
        prompt=str(job_input["prompt"]),
        negative_prompt=str(job_input.get("negative_prompt", "")),
        job_input=job_input,
    )

    width, height = resolve_generation_dimensions(
        original_width=prepared_width,
        original_height=prepared_height,
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
        prompt=prompt,
        negative_prompt=negative_prompt,
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
        "input_image": {
            "width": image_width,
            "height": image_height,
            "prepared_width": prepared_width,
            "prepared_height": prepared_height,
            "framing": framing_settings,
        },
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
            "seed": workflow["7"]["inputs"]["seed"],
            "prompt": workflow["4"]["inputs"]["text"],
            "negative_prompt": workflow["5"]["inputs"]["text"],
        },
        "videos": parsed_outputs["videos"],
        "images": parsed_outputs["images"],
        "files": parsed_outputs["files"]
    }


runpod.serverless.start({"handler": handle_job})
