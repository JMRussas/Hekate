#  Hekate Orchestration - ComfyUI Graph Infrastructure
#
#  ComfyGraph data class, Builder for constructing workflows,
#  and image decode utility.
#  Ported from noz-ai.
#
#  Depends on: PIL
#  Used by:    image/comfy/blocks/__init__.py, image/comfy/executor.py
from __future__ import annotations

import base64
import io
import logging
import random
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

_MAX_SEED = 2**64 - 1


def _sanitize_seed(raw: int | None) -> int:
    if raw is None or raw == 0:
        return random.randint(0, _MAX_SEED)
    return raw % (_MAX_SEED + 1)


@dataclass
class ImageUpload:
    image: Image.Image
    name: str


@dataclass
class ComfyGraph:
    prompt: dict[str, dict]
    uploads: list[ImageUpload]
    output_node: str


class Builder:
    def __init__(self):
        self._counter = 0
        self.prompt: dict[str, dict] = {}
        self.uploads: list[ImageUpload] = []

    def add(self, class_type: str, inputs: dict[str, Any], group: str = "") -> str:
        node_id = str(self._counter)
        self._counter += 1
        self.prompt[node_id] = {
            "class_type": class_type,
            "inputs": inputs,
            "_meta": {"title": class_type, "group": group},
        }
        return node_id

    def ref(self, node_id, output_index: int = 0) -> list:
        if isinstance(node_id, list):
            return node_id
        return [str(node_id), output_index]

    def add_image(self, image: Image.Image, name: str, group: str = "") -> str:
        self.uploads.append(ImageUpload(image=image, name=name))
        return self.add("LoadImage", {"image": name}, group=group)


def decode_image(value: Any) -> Image.Image:
    if isinstance(value, Image.Image):
        return value
    if isinstance(value, str):
        if value.startswith("data:"):
            value = value.split(",", 1)[1]
        img_bytes = base64.b64decode(value)
        return Image.open(io.BytesIO(img_bytes))
    raise ValueError(f"Expected image (PIL or base64 string), got {type(value).__name__}")
