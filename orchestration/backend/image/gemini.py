#  Hekate Orchestration - Gemini Image Backend
#
#  Image generation via Google Gemini API (google-genai package).
#  Optional — only active when GEMINI_API_KEY is set.
#  Ported from noz-ai.
#
#  Depends on: image/base.py, image/seed.py, google-genai
#  Used by:    image/registry.py
from __future__ import annotations

import asyncio
import base64
import io
import logging

from PIL import Image

from backend.image.base import ImageBackend, ImageResult, JobProgress

logger = logging.getLogger(__name__)

# Configurable
_api_key: str = ""
_model: str = "gemini-2.0-flash-preview-image-generation"


def configure(api_key: str, model: str = "") -> None:
    global _api_key, _model
    _api_key = api_key
    if model:
        _model = model


class GeminiImageBackend(ImageBackend):
    name = "gemini"

    def __init__(self):
        self._client = None

    async def start(self) -> None:
        from google import genai
        self._client = genai.Client(api_key=_api_key)
        logger.info("Gemini image backend ready (model: %s)", _model)

    async def stop(self) -> None:
        self._client = None

    def get_models(self) -> list[str]:
        return ["gemini"]

    def get_model_hints(self) -> dict[str, str]:
        return {"gemini": "does not support negative prompts"}

    @property
    def can_generate(self) -> bool:
        return True

    @property
    def can_remove_background(self) -> bool:
        return False

    async def generate(
        self,
        model: str,
        params: dict,
        job_id: str = "",
        progress: JobProgress | None = None,
    ) -> ImageResult:
        from backend.image.seed import hash_seed, random_seed

        if self._client is None:
            return ImageResult(error="Gemini backend not started")

        prompt = params.get("prompt", "")
        text_seed = params.get("seed") or random_seed()
        seed_int = hash_seed(str(text_seed)) % (2**31) or None

        try:
            from google.genai import types

            config_kwargs: dict = {"response_modalities": ["IMAGE"]}
            if seed_int is not None:
                config_kwargs["seed"] = seed_int

            # Check for reference images
            references = params.get("references", [])
            source_images = []
            for ref in references:
                if "data" in ref:
                    from backend.image.comfy.graph import decode_image
                    source_images.append(decode_image(ref["data"]))

            if source_images:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=_model,
                    contents=[prompt, *source_images],
                    config=types.GenerateContentConfig(**config_kwargs),
                )
            else:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_kwargs),
                )

            image = _extract_image(response)
            w, h = image.size
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            encoded = base64.b64encode(buf.getvalue()).decode("utf-8")

            return ImageResult(image=encoded, seed=str(text_seed), width=w, height=h, model="gemini")

        except Exception as e:
            logger.exception("Gemini generation failed")
            return ImageResult(error=str(e))


def _extract_image(response) -> Image.Image:
    if response.parts:
        for part in response.parts:
            if part.inline_data is not None:
                return Image.open(io.BytesIO(part.inline_data.data))

    text = ""
    if hasattr(response, "text") and response.text:
        text = f" Text response: {response.text}"
    raise RuntimeError(f"Gemini returned no image.{text}")
