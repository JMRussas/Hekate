"""
Lumina Image 2.0 architecture translator.

Examines request params and picks the appropriate subgraph template.
Uses CheckpointLoaderSimple (all-in-one) + ModelSamplingAuraFlow + KSampler.
"""
from __future__ import annotations

ARCH = "lumina2"
DEFAULT_CHECKPOINT = "lumina_2.safetensors"


def translate(params: dict) -> tuple[str, dict]:
    """Pick subgraph and fill params for Lumina 2 generation."""
    return ("lumina2_txt2img", {
        "checkpoint": params.get("checkpoint", DEFAULT_CHECKPOINT),
        "prompt": params.get("prompt", ""),
        "negative_prompt": params.get("negative_prompt", ""),
        "seed": params["seed"],
        "width": params.get("width", 1024),
        "height": params.get("height", 1024),
        "cfg": params.get("cfg", 5.0),
        "steps": params.get("steps", 20),
    })
