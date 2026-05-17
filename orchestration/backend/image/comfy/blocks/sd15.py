"""
SD 1.5 architecture translator.

Examines request params and picks the appropriate subgraph template.
Currently supports txt2img only.
"""
from __future__ import annotations

ARCH = "sd15"
DEFAULT_CHECKPOINT = "dreamshaper_8.safetensors"


def translate(params: dict) -> tuple[str, dict]:
    """Pick subgraph and fill params for SD 1.5 generation."""
    return ("sd15_txt2img", {
        "checkpoint": DEFAULT_CHECKPOINT,
        "prompt": params.get("prompt", ""),
        "negative_prompt": params.get("negative_prompt", ""),
        "seed": params["seed"],
        "width": params.get("width", 512),
        "height": params.get("height", 512),
    })
