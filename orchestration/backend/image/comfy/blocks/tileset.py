"""
Tileset architecture translator.

Wraps SDXL with SeamlessTile for seamlessly-tiling game surface textures.
Injects a tile-optimized prompt prefix and sensible negative defaults so
callers only need to describe the surface material (e.g. "stone floor, dungeon").

Depends on: sdxl_tile.json
Used by:    app/comfy/blocks/__init__.py (translator registry)
"""
from __future__ import annotations

ARCH = "tileset"

DEFAULT_CHECKPOINT = "dreamshaperXL_alpha2Xl10.safetensors"

_PROMPT_PREFIX = (
    "seamlessly tileable texture, game tile, top-down view, "
    "consistent flat ambient lighting, uniform value range, "
)

_DEFAULT_NEGATIVE = (
    "seams, visible borders, edge artifacts, vignette, gradient, "
    "watermark, text, signature, perspective distortion, 3d render, photograph"
)


def translate(params: dict) -> tuple[str, dict]:
    """Pick subgraph and fill params for seamless tile generation."""
    raw_prompt = params.get("prompt", "")
    prompt = f"{_PROMPT_PREFIX}{raw_prompt}" if raw_prompt else _PROMPT_PREFIX.rstrip(", ")
    negative = params.get("negative_prompt", _DEFAULT_NEGATIVE)

    return ("sdxl_tile", {
        "checkpoint": params.get("checkpoint", DEFAULT_CHECKPOINT),
        "prompt": prompt,
        "negative_prompt": negative,
        "seed": params["seed"],
        "width": params.get("width", 1024),
        "height": params.get("height", 1024),
        "steps": params.get("steps", 20),
        "cfg": params.get("cfg", 7.0),
        "tiling": params.get("tiling", "enable"),
    })
