"""
SDXL architecture translator.

Examines request params and picks the appropriate subgraph template.
Currently supports txt2img only; img2img, LoRA, ControlNet are future additions.
"""
from __future__ import annotations

ARCH = "sdxl"
IS_DEFAULT = True
DEFAULT_CHECKPOINT = "dreamshaperXL_lightningDPMSDE.safetensors"


def translate(params: dict) -> tuple[str, dict]:
    """Pick subgraph and fill params for SDXL generation."""
    return ("sdxl_txt2img", {
        "checkpoint": DEFAULT_CHECKPOINT,
        "prompt": params.get("prompt", ""),
        "negative_prompt": params.get("negative_prompt", ""),
        "seed": params["seed"],
        "width": params.get("width", 1024),
        "height": params.get("height", 1024),
    })
