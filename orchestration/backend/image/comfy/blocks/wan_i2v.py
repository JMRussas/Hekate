#  Hekate - wan_i2v Translator
#
#  Wan2.1 Image-to-Video workflow translator for the Hekate image service.
#  14B model — high-quality character idle animation from a source image.
#
#  Depends on: comfy/blocks/wan_i2v.json
#  Used by:    image service block auto-discovery
"""
Wan2.1 Image-to-Video architecture translator.

Routes to the wan_i2v subgraph for generating short character animation
clips from a source image using the 14B fp8 model.

Node-to-field mapping (for the wan_i2v.json workflow):
  "6"  → CLIPTextEncode (positive)  — prompt text
  "7"  → CLIPTextEncode (negative)  — negative text
  "3"  → KSampler                   — seed, steps, cfg
  "50" → WanImageToVideo            — width, height, length
  "60" → VHS_VideoCombine           — frame_rate, pingpong, filename_prefix
  "52" → LoadImage (upload slot)    — source_image filename

Depends on: wan_i2v.json
Used by:    image service block auto-discovery
"""
from __future__ import annotations
import random as _random

ARCH = "wan_i2v"

DEFAULT_MODEL = "wan2.1_i2v_480p_14B_fp8_scaled.safetensors"

_DEFAULT_NEGATIVE = (
    "static, still, frozen, blurry, worst quality, low quality, "
    "watermark, text, deformed"
)

# Output type and generation timeout (seconds)
OUTPUT_TYPE = "video"
TIMEOUT = 600

# Parameter definitions — passed downstream for node injection
# Each entry maps a caller-facing name to the node ID + field in the workflow JSON.
PARAMS = {
    "prompt":          {"node": "6",  "field": "text",            "required": True},
    "negative":        {"node": "7",  "field": "text",            "default": _DEFAULT_NEGATIVE},
    "seed":            {"node": "3",  "field": "seed",            "type": "int",   "default": -1},
    "steps":           {"node": "3",  "field": "steps",           "type": "int",   "default": 20},
    "cfg":             {"node": "3",  "field": "cfg",             "type": "float", "default": 6.0},
    "width":           {"node": "50", "field": "width",           "type": "int",   "default": 512},
    "height":          {"node": "50", "field": "height",          "type": "int",   "default": 768},
    "length":          {"node": "50", "field": "length",          "type": "int",   "default": 49,
                        "description": "Number of video frames (49 @ 16 fps ≈ 3 s)"},
    "frame_rate":      {"node": "60", "field": "frame_rate",      "type": "int",   "default": 16},
    "pingpong":        {"node": "60", "field": "pingpong",        "type": "bool",  "default": True},
    "filename_prefix": {"node": "60", "field": "filename_prefix",                  "default": "wan_i2v"},
}

# Upload slot — caller must supply a source image
UPLOADS = {
    "source_image": {
        "node": "52",
        "field": "image",
        "description": "Character image to animate",
    },
}


def translate(params: dict) -> tuple[str, dict]:
    """Pick subgraph and fill params for Wan2.1 image-to-video generation."""
    return ("wan_i2v", {
        "prompt":          params.get("prompt", ""),
        "negative":        params.get("negative", _DEFAULT_NEGATIVE),
        "seed":            params.get("seed", _random.randint(0, 2**31 - 1)),
        "steps":           params.get("steps", 20),
        "cfg":             params.get("cfg", 6.0),
        "width":           params.get("width", 512),
        "height":          params.get("height", 768),
        "length":          params.get("length", 49),
        "frame_rate":      params.get("frame_rate", 16),
        "pingpong":        params.get("pingpong", True),
        "filename_prefix": params.get("filename_prefix", "wan_i2v"),
        "source_image":    params.get("source_image"),
    })
