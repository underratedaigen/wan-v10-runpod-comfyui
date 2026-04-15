# WAN v10 NSFW Runpod Serverless Worker

This folder is a self-contained Runpod Serverless worker for image-to-video generation with:

- ComfyUI on top of `runpod/worker-comfyui:5.8.5-base`
- `Phr00t/WAN2.2-14B-Rapid-AllInOne` v10 NSFW I2V AIO checkpoint
- a custom Runpod handler so you can send a simple request with a prompt plus an image URL or base64 image

It does not require raw ComfyUI workflow JSON in the API request.

## What This Worker Does

- downloads `wan2.2-i2v-rapid-aio-v10-nsfw.safetensors` automatically at worker startup if it is missing
- stores the checkpoint on `/runpod-volume/models/checkpoints` when a network volume is attached
- falls back to `/comfyui/models/checkpoints` if no network volume is mounted
- uploads the input image to ComfyUI
- builds a fixed WAN 2.2 I2V workflow internally
- returns MP4 output as either:
  - base64 in the Runpod response
  - or a bucket URL if Runpod bucket env vars are configured

## Main Files

- `Dockerfile`
- `custom_start.sh`
- `bootstrap_models.py`
- `handler.py`
- `workflow_builder.py`
- `workflow_templates/wan_v10_i2v.json`
- `test_input.json`
- `.env.example`

## Request Format

Minimal request:

```json
{
  "input": {
    "prompt": "A cinematic slow-motion close-up of the subject turning toward the camera, realistic motion, wet skin, subtle pool reflections.",
    "image_url": "https://example.com/your-image.jpg"
  }
}
```

More controlled request:

```json
{
  "input": {
    "prompt": "A cinematic slow-motion close-up of the subject turning toward the camera, realistic motion, wet skin, subtle pool reflections.",
    "negative_prompt": "blurry, low quality, artifacts, subtitles, watermark, static frame",
    "image_url": "https://example.com/your-image.jpg",
    "resolution_preset": "720p",
    "num_frames": 81,
    "fps": 16,
    "steps": 4,
    "cfg": 1.0,
    "sampler_name": "euler_ancestral",
    "scheduler": "beta",
    "shift": 5.0,
    "seed": 42,
    "framing_mode": "off",
    "camera_motion_mode": "off",
    "subject_scale": 1.0,
    "vertical_bias": 0.0
  }
}
```

## Response Format

If bucket upload is configured:

```json
{
  "output": {
    "videos": [
      {
        "filename": "wan-v10/wan_i2v_00001_.mp4",
        "type": "bucket_url",
        "data": "https://your-bucket/.../wan_i2v_00001_.mp4",
        "mime_type": "video/mp4"
      }
    ]
  }
}
```

If bucket upload is not configured, `type` becomes `base64`.

## Runpod Setup

If this folder lives inside a larger repo, use these GitHub deploy fields:

- Build context: `wan-v10-runpod-comfyui`
- Dockerfile path: `Dockerfile`

Recommended endpoint settings:

- Endpoint type: `Queue`
- GPU: `A100 80GB` or better
- Active workers: `0`
- Max workers: `1`
- GPUs per worker: `1`
- Idle timeout: `300-900` seconds
- Execution timeout: `1800-3600` seconds
- Container disk:
  - with network volume: `80 GB` recommended
  - without network volume: `120 GB` safer

The disk guidance above is my sizing recommendation based on the `23.4 GB` checkpoint plus ComfyUI/runtime overhead, not an official minimum.

Strong recommendation: attach a Runpod network volume. The bootstrap script automatically prefers:

```text
/runpod-volume/models/checkpoints
```

when a network volume is present.

## Environment Variables

At minimum:

```text
WAN_CHECKPOINT_NAME=wan2.2-i2v-rapid-aio-v10-nsfw.safetensors
WAN_CHECKPOINT_URL=https://huggingface.co/Phr00t/WAN2.2-14B-Rapid-AllInOne/resolve/main/v10/wan2.2-i2v-rapid-aio-v10-nsfw.safetensors
WAN_CHECKPOINT_SIZE=23387046339
WAN_DEFAULT_RESOLUTION_PRESET=720p
WAN_DEFAULT_NUM_FRAMES=81
WAN_DEFAULT_FPS=16
WAN_DEFAULT_STEPS=4
WAN_DEFAULT_CFG=1.0
WAN_DEFAULT_SAMPLER=euler_ancestral
WAN_DEFAULT_SCHEDULER=beta
WAN_DEFAULT_DENOISE=1.0
WAN_DEFAULT_SHIFT=5.0
WAN_DEFAULT_FILENAME_PREFIX=wan-v10/wan_i2v
WAN_DEFAULT_FRAMING_MODE=off
WAN_DEFAULT_CAMERA_MOTION_MODE=off
WAN_DEFAULT_SUBJECT_SCALE=1.0
WAN_DEFAULT_VERTICAL_BIAS=0.0
WAN_DEFAULT_BG_BLUR_RADIUS=10
WAN_DEFAULT_BG_DARKEN=0.96
WAN_DEFAULT_FOREGROUND_SHARPNESS=1.15
WAN_DEFAULT_TRIM_INSET_FRAME=true
WAN_DEFAULT_PRESERVE_SOURCE_DIMENSIONS=true
COMFY_LOG_LEVEL=INFO
COMFY_HISTORY_TIMEOUT_S=3600
COMFY_POLL_INTERVAL_S=5
COMFY_STARTUP_TIMEOUT_S=300
COMFY_STARTUP_POLL_INTERVAL_S=2
```

Optional:

```text
HF_TOKEN=your_huggingface_token
```

Bucket uploads:

```text
BUCKET_ENDPOINT_URL=...
BUCKET_ACCESS_KEY_ID=...
BUCKET_SECRET_ACCESS_KEY=...
```

## Test Request

```bash
curl -X POST "https://api.runpod.ai/v2/YOUR_ENDPOINT_ID/runsync" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d @test_input.json
```

## Notes

- `720p` and `480p` preserve the input aspect ratio instead of forcing exactly `1280x720` or `832x480`
- width and height are rounded to multiples of `16`
- source dimensions are now preserved by default when you do not explicitly pass `width` or `height`, which helps avoid unnecessary upscale blur on already-clean inputs
- frame counts are rounded up to the nearest valid WAN length (`4n + 1`)
- anti-zoom framing is now off by default so the input image is passed through more directly and the opening frame stays cleaner
- inset-frame trimming is now on by default, so screenshot-style uploads with a sharp center image over a blurred background will be auto-cropped before generation when the border is detected confidently
- if you want the old behavior back for a specific request, turn `framing_mode` and `camera_motion_mode` on explicitly
- the worker uses a fixed internal graph so the public API stays simple

## Sources Used

- [Phr00t WAN 2.2 All-in-One](https://huggingface.co/Phr00t/WAN2.2-14B-Rapid-AllInOne)
- [Phr00t WAN v10 tree](https://huggingface.co/Phr00t/WAN2.2-14B-Rapid-AllInOne/tree/main/v10)
- [Official Wan 2.2 I2V A14B](https://huggingface.co/Wan-AI/Wan2.2-I2V-A14B)
- [Runpod worker-comfyui](https://github.com/runpod-workers/worker-comfyui)
- [Runpod custom worker guide](https://docs.runpod.io/serverless/workers/custom-worker)
- [Runpod GitHub integration](https://docs.runpod.io/serverless/github-integration)
