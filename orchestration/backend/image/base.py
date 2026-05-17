#  Hekate Orchestration - Image Backend Base
#
#  Abstract base for pluggable image generation backends.
#  Each backend expresses capabilities (generate, remove_background)
#  and what models it offers. The registry routes requests.
#
#  Depends on: (none)
#  Used by:    image/registry.py, image/comfy/backend.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ImageResult:
    """Result from an image generation or processing operation."""
    image: str = ""       # base64-encoded PNG
    seed: str = ""        # seed used
    width: int = 0
    height: int = 0
    model: str = ""
    error: str = ""


@dataclass
class JobProgress:
    """Progress state for a running image job."""
    current_step: int = 0
    total_steps: int = 0
    sampler_name: str = ""

    @property
    def fraction(self) -> float:
        if self.total_steps <= 0:
            return 0.0
        return min(1.0, self.current_step / self.total_steps)


class ImageBackend:
    """Base class for image generation backends."""

    name: str = ""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def get_models(self) -> list[str]:
        return []

    def get_models_info(self) -> list[dict]:
        return [
            {"name": m, "backend": self.name, "valid": True, "errors": []}
            for m in self.get_models()
        ]

    def get_model_hints(self) -> dict[str, str]:
        return {}

    @property
    def can_generate(self) -> bool:
        return False

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
        raise NotImplementedError

    async def remove_background(
        self,
        image_b64: str,
        job_id: str = "",
        progress: JobProgress | None = None,
    ) -> ImageResult:
        raise NotImplementedError
