#  Hekate - animatediff_img2vid Translator
#
#  AnimateDiff Image-to-Video workflow translator for the Hekate image service.
#  SDXL + AnimateDiff — idle animation loop from a source character image.
#
#  Depends on: comfy/blocks/animatediff_img2vid.json
#  Used by:    image service block auto-discovery
"""
AnimateDiff Image-to-Video architecture translator.

Routes to the animatediff_img2vid subgraph for generating looping character
animations from a source image using SDXL + AnimateDiff.

Node-to-field mapping (for the animatediff_img2vid.json workflow):
  "20" → CLIPTextEncode (positive)  — prompt text
  "21" → CLIPTextEncode (negative)  — negative text
  "30" → KSampler                   — seed, steps, cfg, denoise
  "3"  → ADE_ApplyAnimateDiffModelSimple — motion_scale
  "11" → ImageScale                 — width, height
  "50" → VHS_VideoCombine           — frame_rate, pingpong, filename_prefix
  "10" → LoadImage (upload slot)    — source_image filename

Depends on: animatediff_img2vid.json
Used by:    image service block auto-discovery
"""
from __future__ import annotations
import random as _random

ARCH = "animatediff_img2vid"

_DEFAULT_NEGATIVE = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, "
    "extra digit, fewer digits, cropped, worst quality, low quality, "
    "jpeg artifacts, signature, watermark, blurry, deformed, ugly, "
    "morphing face, changing identity, glitch"
)

# Output type and generation timeout (seconds)
OUTPUT_TYPE = "video"
TIMEOUT = 300

# Parameter definitions — passed downstream for node injection
# Each entry maps a caller-facing name to the node ID + field in the workflow JSON.
PARAMS = {
    "prompt":          {"node": "20", "field": "text",            "required": True,
                        "description": "Animation description (what the character does)"},
    "negative":        {"node": "21", "field": "text",            "default": _DEFAULT_NEGATIVE},
    "seed":            {"node": "30", "field": "seed",            "type": "int",   "default": -1},
    "steps":           {"node": "30", "field": "steps",           "type": "int",   "default": 20},
    "cfg":             {"node": "30", "field": "cfg",             "type": "float", "default": 7.0},
    "denoise":         {"node": "30", "field": "denoise",         "type": "float", "default": 0.40,
                        "description": "Lower = more faithful to source image (0.3–0.5 typical)"},
    "motion_scale":    {"node": "3",  "field": "motion_scale",    "type": "float", "default": 1.1,
                        "description": "Motion intensity (0.5 = subtle, 1.5 = strong)"},
    "width":           {"node": "11", "field": "width",           "type": "int",   "default": 768},
    "height":          {"node": "11", "field": "height",          "type": "int",   "default": 1024},
    "frame_rate":      {"node": "50", "field": "frame_rate",      "type": "int",   "default": 8},
    "pingpong":        {"node": "50", "field": "pingpong",        "type": "bool",  "default": True,
                        "description": "Play forward then backward for seamless loop"},
    "filename_prefix": {"node": "50", "field": "filename_prefix",                  "default": "animated"},
}

# Upload slot — caller must supply a source image
UPLOADS = {
    "source_image": {
        "node": "10",
        "field": "image",
        "description": "Character image to animate",
    },
}


def translate(params: dict) -> tuple[str, dict]:
    """Pick subgraph and fill params for AnimateDiff image-to-video generation."""
    return ("animatediff_img2vid", {
        "prompt":          params.get("prompt", ""),
        "negative":        params.get("negative", _DEFAULT_NEGATIVE),
        "seed":            params.get("seed", _random.randint(0, 2**31 - 1)),
        "steps":           params.get("steps", 20),
        "cfg":             params.get("cfg", 7.0),
        "denoise":         params.get("denoise", 0.40),
        "motion_scale":    params.get("motion_scale", 1.1),
        "width":           params.get("width", 768),
        "height":          params.get("height", 1024),
        "frame_rate":      params.get("frame_rate", 8),
        "pingpong":        params.get("pingpong", True),
        "filename_prefix": params.get("filename_prefix", "animated"),
        "source_image":    params.get("source_image"),
    })
