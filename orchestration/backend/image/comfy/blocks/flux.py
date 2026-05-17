"""
Flux2 architecture translator.

Examines request params and picks the appropriate subgraph template.
Currently supports txt2img only. Negative prompts are ignored (not useful for Flux).
"""
from __future__ import annotations

ARCH = "flux"
DEFAULT_CHECKPOINT = "flux-2-klein-4b-fp8.safetensors"
DEFAULT_CLIP_FILE = "qwen_3_4b.safetensors"
DEFAULT_VAE_FILE = "flux2-vae.safetensors"


def translate(params: dict) -> tuple[str, dict]:
    """Pick subgraph and fill params for Flux2 generation."""
    return ("flux_txt2img", {
        "checkpoint": params.get("checkpoint", DEFAULT_CHECKPOINT),
        "clip_file": params.get("clip_file", DEFAULT_CLIP_FILE),
        "vae_file": params.get("vae_file", DEFAULT_VAE_FILE),
        "prompt": params.get("prompt", ""),
        "seed": params["seed"],
        "width": params.get("width", 1024),
        "height": params.get("height", 1024),
        "cfg": params.get("cfg", 1.5),
        "steps": params.get("steps", 8),
        "sampler": params.get("sampler", "euler"),
    })
