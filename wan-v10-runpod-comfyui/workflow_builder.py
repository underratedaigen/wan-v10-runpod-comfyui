import json
import math
import random
from copy import deepcopy
from pathlib import Path


PRESET_PIXELS = {
    "480p": 832 * 480,
    "720p": 1280 * 720,
}


def round_to_multiple(value: int, multiple: int = 16) -> int:
    value = max(multiple, int(value))
    return max(multiple, int(round(value / multiple) * multiple))


def normalize_frame_count(value: int | str) -> int:
    frames = max(1, int(value))
    remainder = (frames - 1) % 4
    if remainder == 0:
        return frames
    return frames + (4 - remainder)


def coerce_seed(seed: int | str | None) -> int:
    if seed is None:
        return random.randint(0, 2**63 - 1)
    seed_value = int(seed)
    if seed_value < 0:
        return random.randint(0, 2**63 - 1)
    return seed_value


def preset_dimensions(original_width: int, original_height: int, preset: str) -> tuple[int, int]:
    preset_key = preset.lower()
    if preset_key not in PRESET_PIXELS:
        raise ValueError(f"Unsupported resolution_preset '{preset}'. Use one of: {', '.join(PRESET_PIXELS)}")

    target_pixels = PRESET_PIXELS[preset_key]
    aspect = original_width / original_height
    width = math.sqrt(target_pixels * aspect)
    height = width / aspect
    return round_to_multiple(int(width)), round_to_multiple(int(height))


def resolve_generation_dimensions(
    *,
    original_width: int,
    original_height: int,
    width: int | str | None,
    height: int | str | None,
    resolution_preset: str,
) -> tuple[int, int]:
    if width is None and height is None:
        return preset_dimensions(original_width, original_height, resolution_preset)

    if width is not None and height is not None:
        return round_to_multiple(int(width)), round_to_multiple(int(height))

    aspect = original_width / original_height
    if width is not None:
        final_width = round_to_multiple(int(width))
        final_height = round_to_multiple(int(final_width / aspect))
        return final_width, final_height

    final_height = round_to_multiple(int(height))
    final_width = round_to_multiple(int(final_height * aspect))
    return final_width, final_height


def load_template(template_path: Path) -> dict:
    return json.loads(template_path.read_text(encoding="utf-8"))


def build_workflow(
    *,
    template_path: Path,
    input_image_name: str,
    checkpoint_name: str,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    num_frames: int | str,
    fps: int | float | str,
    steps: int | str,
    cfg: float | str,
    sampler_name: str,
    scheduler: str,
    denoise: float | str,
    shift: float | str,
    seed: int,
    filename_prefix: str,
) -> dict:
    workflow = deepcopy(load_template(template_path))

    workflow["1"]["inputs"]["image"] = input_image_name
    workflow["2"]["inputs"]["ckpt_name"] = checkpoint_name
    workflow["3"]["inputs"]["shift"] = float(shift)
    workflow["4"]["inputs"]["text"] = prompt
    workflow["5"]["inputs"]["text"] = negative_prompt
    workflow["6"]["inputs"]["width"] = int(width)
    workflow["6"]["inputs"]["height"] = int(height)
    workflow["6"]["inputs"]["length"] = normalize_frame_count(num_frames)
    workflow["7"]["inputs"]["seed"] = int(seed)
    workflow["7"]["inputs"]["steps"] = int(steps)
    workflow["7"]["inputs"]["cfg"] = float(cfg)
    workflow["7"]["inputs"]["sampler_name"] = sampler_name
    workflow["7"]["inputs"]["scheduler"] = scheduler
    workflow["7"]["inputs"]["denoise"] = float(denoise)
    workflow["9"]["inputs"]["fps"] = float(fps)
    workflow["10"]["inputs"]["filename_prefix"] = filename_prefix

    return workflow
