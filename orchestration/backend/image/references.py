#  Hekate Orchestration - Reference Image Types
#
#  Defines reference types (composition, style, etc.) and their ComfyUI resources.
#  Ported from noz-ai.
#
#  Depends on: image/comfy/blocks/__init__.py (scan_model_dir)
#  Used by:    image/comfy/blocks/__init__.py
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ReferenceType:
    key: str
    name: str
    description: str
    arch: str
    adapter_model: str
    clip_vision_model: str
    adapter_node: str
    default_strength: float


REFERENCE_TYPES: dict[str, ReferenceType] = {
    "composition": ReferenceType(
        key="composition",
        name="Composition Reference",
        description="Match the spatial layout and structure of the reference image",
        arch="sdxl",
        adapter_model="ip-adapter-plus_sdxl_composition.safetensors",
        clip_vision_model="CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors",
        adapter_node="IPAdapterAdvanced",
        default_strength=1.0,
    ),
}


def get_available_reference_types(arch: str | None = None) -> dict[str, ReferenceType]:
    from backend.image.comfy.blocks import scan_model_dir
    ipadapter_files = scan_model_dir("ipadapter")
    clip_vision_files = scan_model_dir("clip_vision")
    result: dict[str, ReferenceType] = {}
    for key, rt in REFERENCE_TYPES.items():
        if arch and rt.arch != "*" and rt.arch != arch:
            continue
        if rt.adapter_model not in ipadapter_files:
            continue
        if rt.clip_vision_model not in clip_vision_files:
            continue
        result[key] = rt
    return result


def get_reference_type_enum() -> list[str]:
    return list(get_available_reference_types().keys())


def get_reference_type_descriptions() -> str:
    available = get_available_reference_types()
    if not available:
        return ""
    lines = []
    for rt in available.values():
        lines.append(f"- {rt.key} ({rt.arch}): {rt.description}")
    return "Available reference types:\n" + "\n".join(lines)
