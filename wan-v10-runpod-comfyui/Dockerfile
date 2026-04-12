FROM runpod/worker-comfyui:5.8.5-base

ENV PYTHONUNBUFFERED=1 \
    COMFY_LOG_LEVEL=INFO \
    WAN_CHECKPOINT_NAME=wan2.2-i2v-rapid-aio-v10-nsfw.safetensors \
    WAN_CHECKPOINT_URL=https://huggingface.co/Phr00t/WAN2.2-14B-Rapid-AllInOne/resolve/main/v10/wan2.2-i2v-rapid-aio-v10-nsfw.safetensors \
    WAN_CHECKPOINT_SIZE=23387046339 \
    WAN_DEFAULT_RESOLUTION_PRESET=720p \
    WAN_DEFAULT_NUM_FRAMES=81 \
    WAN_DEFAULT_FPS=16 \
    WAN_DEFAULT_STEPS=4 \
    WAN_DEFAULT_CFG=1.0 \
    WAN_DEFAULT_SAMPLER=euler_ancestral \
    WAN_DEFAULT_SCHEDULER=beta \
    WAN_DEFAULT_DENOISE=1.0 \
    WAN_DEFAULT_SHIFT=5.0 \
    WAN_DEFAULT_FILENAME_PREFIX=wan-v10/wan_i2v \
    WAN_SKIP_MODEL_DOWNLOAD=false \
    COMFY_HISTORY_TIMEOUT_S=3600 \
    COMFY_POLL_INTERVAL_S=5

COPY custom_start.sh /custom_start.sh
COPY bootstrap_models.py /bootstrap_models.py
COPY workflow_builder.py /workflow_builder.py
COPY handler.py /handler.py
COPY workflow_templates /workflow_templates

RUN chmod +x /custom_start.sh

CMD ["/custom_start.sh"]
