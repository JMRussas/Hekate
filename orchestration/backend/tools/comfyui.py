#  Orchestration Engine - Image Generation Tools
#
#  Full-featured image generation via the composable subgraph system.
#  Replaces the original minimal SDXL-only implementation.
#  Supports all architectures (flux, sdxl, sd15, lumina2, zimage),
#  LoRA styles, reference images, batch generation, and background removal.
#
#  Depends on: tools/base.py, image/registry.py, image/base.py
#  Used by:    services/executor.py (via tool registry)
from __future__ import annotations

import logging

from backend.image import registry
from backend.image.base import ImageResult
from backend.tools.base import Tool

logger = logging.getLogger(__name__)


class GenerateImageTool(Tool):
    name = "generate_image"
    description = (
        "Generate an image from a text prompt. Supports multiple architectures "
        "(flux, sdxl, sd15, lumina2, zimage), LoRA styles, and reference images. "
        "Returns a base64-encoded PNG."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Text prompt describing the image to generate",
            },
            "negative_prompt": {
                "type": "string",
                "default": "",
                "description": "Things to avoid in the image",
            },
            "model": {
                "type": "string",
                "description": "Architecture: flux, sdxl, sd15, lumina2, zimage. Default: sdxl",
            },
            "lora": {
                "type": "string",
                "description": "LoRA style key (use 'none' for base model)",
            },
            "seed": {
                "type": "string",
                "description": "Text seed for reproducibility (e.g. 'golden-falcon')",
            },
            "width": {"type": "integer", "default": 1024, "description": "Image width"},
            "height": {"type": "integer", "default": 1024, "description": "Image height"},
        },
        "required": ["prompt"],
    }

    async def execute(self, params: dict) -> str:
        model = params.pop("model", "sdxl")

        try:
            backend = registry.get_backend_for_model(model)
        except ValueError:
            available = registry.get_all_models()
            return f"Error: Unknown model '{model}'. Available: {', '.join(available)}"

        try:
            result = await backend.generate(model=model, params=params)
            if result.error:
                return f"Error: {result.error}"
            return (
                f"Image generated: {result.width}x{result.height} "
                f"(model={result.model}, seed={result.seed})\n"
                f"[base64:{len(result.image)} chars]"
            )
        except Exception as e:
            logger.exception("Image generation failed")
            return f"Error: {e}"


class BatchGenerateImageTool(Tool):
    name = "batch_generate_images"
    description = (
        "Generate 2-4 images in a single batch. Each image can have different "
        "prompts, models, and styles. Returns results for each."
    )
    parameters = {
        "type": "object",
        "properties": {
            "images": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "negative_prompt": {"type": "string", "default": ""},
                        "model": {"type": "string", "default": "sdxl"},
                        "lora": {"type": "string", "default": "none"},
                        "seed": {"type": "string"},
                    },
                    "required": ["prompt"],
                },
                "minItems": 2,
                "maxItems": 4,
                "description": "Array of image generation requests (2-4)",
            },
        },
        "required": ["images"],
    }

    async def execute(self, params: dict) -> str:
        images = params.get("images", [])
        if len(images) < 2 or len(images) > 4:
            return "Error: batch_generate requires 2-4 images"

        results = []
        for i, img_params in enumerate(images):
            model = img_params.pop("model", "sdxl")
            try:
                backend = registry.get_backend_for_model(model)
                result = await backend.generate(model=model, params=img_params)
                if result.error:
                    results.append(f"Image {i+1}: Error - {result.error}")
                else:
                    results.append(f"Image {i+1}: {result.width}x{result.height} (seed={result.seed})")
            except Exception as e:
                results.append(f"Image {i+1}: Error - {e}")

        return "\n".join(results)


class RemoveBackgroundTool(Tool):
    name = "remove_background"
    description = "Remove the background from an image. Returns a base64 PNG with transparent background."
    parameters = {
        "type": "object",
        "properties": {
            "image": {
                "type": "string",
                "description": "Base64-encoded PNG image",
            },
        },
        "required": ["image"],
    }

    async def execute(self, params: dict) -> str:
        image_b64 = params.get("image", "")
        if not image_b64:
            return "Error: No image provided"

        try:
            backend = registry.get_rmbg_backend()
            result = await backend.remove_background(image_b64)
            if result.error:
                return f"Error: {result.error}"
            return f"Background removed: {result.width}x{result.height}\n[base64:{len(result.image)} chars]"
        except Exception as e:
            return f"Error: {e}"


class ListModelsTool(Tool):
    name = "list_image_models"
    description = "List available image generation models, their validation status, and LoRA styles."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, params: dict) -> str:
        models = registry.get_all_models_info()
        if not models:
            return "No image models available. Is ComfyUI running?"

        lines = []
        for m in models:
            status = "valid" if m.get("valid") else f"invalid ({', '.join(m.get('errors', []))})"
            loras = m.get("loras", [])
            lora_str = ", ".join(l["key"] for l in loras) if loras else "none"
            lines.append(f"- {m['name']} ({m.get('backend', '?')}) [{status}] LoRAs: {lora_str}")

        return "Available models:\n" + "\n".join(lines)
