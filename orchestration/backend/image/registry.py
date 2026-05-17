#  Hekate Orchestration - Image Backend Registry
#
#  Aggregates models from all active backends and routes requests.
#
#  Depends on: image/base.py
#  Used by:    tools/comfyui.py
from __future__ import annotations

import logging

from backend.image.base import ImageBackend

logger = logging.getLogger(__name__)

_backends: list[ImageBackend] = []


def get_all_models() -> list[str]:
    models = []
    for b in _backends:
        models.extend(b.get_models())
    return models


def get_all_models_info() -> list[dict]:
    models = []
    for b in _backends:
        models.extend(b.get_models_info())
    return models


def get_all_model_hints() -> dict[str, str]:
    hints = {}
    for b in _backends:
        hints.update(b.get_model_hints())
    return hints


def get_backend_for_model(model: str) -> ImageBackend:
    for b in _backends:
        if model in b.get_models():
            return b
    raise ValueError(f"No image backend for model: {model}")


def get_rmbg_backend() -> ImageBackend:
    for b in _backends:
        if b.can_remove_background:
            return b
    raise ValueError("No image backend supports remove_background")


async def start_all(
    comfyui_url: str = "http://localhost:8188",
    comfyui_dir: str = "",
    models_dir: str = "",
    gemini_api_key: str = "",
    gemini_model: str = "",
) -> None:
    from backend.image.comfy.backend import ComfyImageBackend

    comfy = ComfyImageBackend(comfyui_url=comfyui_url, comfyui_dir=comfyui_dir, models_dir=models_dir)
    await comfy.start()
    _backends.append(comfy)
    logger.info("Image backend started: %s (models: %s)", comfy.name, comfy.get_models())

    # Gemini — only if API key configured
    if gemini_api_key:
        try:
            from backend.image.gemini import GeminiImageBackend, configure as configure_gemini
            configure_gemini(api_key=gemini_api_key, model=gemini_model)
            gemini = GeminiImageBackend()
            await gemini.start()
            _backends.append(gemini)
            logger.info("Image backend started: %s", gemini.name)
        except ImportError:
            logger.warning("Gemini API key set but google-genai not installed, skipping")


async def stop_all() -> None:
    for b in _backends:
        logger.info("Stopping image backend: %s", b.name)
        await b.stop()
    _backends.clear()
