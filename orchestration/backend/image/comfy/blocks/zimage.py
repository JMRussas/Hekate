"""
Z-Image Turbo architecture translator.

Examines request params and picks the appropriate subgraph template.
Uses UNETLoader + ModelSamplingAuraFlow + CLIPLoader(ltxv) + KSampler.
"""
from __future__ import annotations

ARCH = "zimage"
DEFAULT_CHECKPOINT = "z_image_turbo_bf16.safetensors"
DEFAULT_CLIP_FILE = "qwen_3_4b.safetensors"
DEFAULT_VAE_FILE = "z_image_ae.safetensors"


def translate(params: dict) -> tuple[str, dict]:
    """Pick subgraph and fill params for Z-Image generation."""
    return ("zimage_txt2img", {
        "checkpoint": params.get("checkpoint", DEFAULT_CHECKPOINT),
        "clip_file": params.get("clip_file", DEFAULT_CLIP_FILE),
        "vae_file": params.get("vae_file", DEFAULT_VAE_FILE),
        "prompt": params.get("prompt", ""),
        "negative_prompt": params.get("negative_prompt", ""),
        "seed": params["seed"],
        "width": params.get("width", 1024),
        "height": params.get("height", 1024),
        "cfg": params.get("cfg", 1.0),
        "steps": params.get("steps", 8),
        "sampler": params.get("sampler", "euler"),
        "scheduler": params.get("scheduler", "normal"),
    })
