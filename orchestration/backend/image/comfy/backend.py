#  Hekate Orchestration - ComfyUI Image Backend
#
#  Full-featured ComfyUI backend using the composable subgraph system.
#  Supports multiple architectures (flux, sdxl, sd15, lumina2, zimage),
#  LoRA injection, reference images, and background removal.
#
#  Depends on: image/base.py, image/comfy/executor.py, image/comfy/blocks/
#  Used by:    image/registry.py
from __future__ import annotations

import logging

from backend.image.base import ImageBackend, ImageResult, JobProgress
from backend.image.comfy.executor import ComfyExecutor

logger = logging.getLogger(__name__)


class ComfyImageBackend(ImageBackend):
    name = "comfy"

    def __init__(self, comfyui_url: str = "http://localhost:8188", comfyui_dir: str = "", models_dir: str = ""):
        self._executor = ComfyExecutor(comfyui_url)
        self._comfyui_dir = comfyui_dir
        self._models_dir = models_dir

    async def start(self) -> None:
        from backend.image.comfy.blocks import configure as configure_blocks
        from backend.image.loras import configure as configure_loras

        configure_blocks(comfyui_dir=self._comfyui_dir, models_dir=self._models_dir)
        configure_loras(self._models_dir)

    async def stop(self) -> None:
        await self._executor.close()

    def get_models(self) -> list[str]:
        from backend.image.comfy.blocks import get_models_info
        return [m["name"] for m in get_models_info() if m["valid"]]

    def get_models_info(self) -> list[dict]:
        from backend.image.comfy.blocks import get_models_info
        info = get_models_info()
        # Add LoRA info per model
        from backend.image.loras import get_loras_for_arch
        for m in info:
            loras = get_loras_for_arch(m["name"])
            m["loras"] = [
                {"key": l.key, "name": l.name, "description": l.description}
                for l in loras.values()
            ]
        return info

    @property
    def can_generate(self) -> bool:
        return True

    @property
    def can_remove_background(self) -> bool:
        return True

    async def generate(
        self,
        model: str,
        params: dict,
        job_id: str = "",
        progress: JobProgress | None = None,
    ) -> ImageResult:
        from backend.image.comfy.blocks import translate_for_chat
        from backend.image.seed import hash_seed

        # Resolve LoRA
        lora_key = params.pop("lora", "")
        if lora_key and lora_key != "none":
            from backend.image.loras import discover_loras
            lora_entry = discover_loras().get(lora_key)
            if lora_entry:
                params["lora_file"] = lora_entry.file
                params["lora_strength"] = lora_entry.strength
                params["lora_prefix"] = lora_entry.prompt_prefix
                params["lora_postfix"] = lora_entry.prompt_postfix

        # Resolve text seed
        seed = params.get("seed", "")
        if isinstance(seed, str) and seed:
            params["seed"] = hash_seed(seed)
        elif not seed:
            from backend.image.seed import random_seed, hash_seed as hs
            seed_text = random_seed()
            params["seed"] = hs(seed_text)
            seed = seed_text

        graph = translate_for_chat(model, params, job_id=job_id)
        logger.info("Built %s graph: %d nodes, %d uploads", model, len(graph.prompt), len(graph.uploads))

        result = await self._executor.execute(graph, progress=progress, job_id=job_id)
        result.seed = str(seed)
        result.model = model
        return result

    async def remove_background(
        self,
        image_b64: str,
        job_id: str = "",
        progress: JobProgress | None = None,
    ) -> ImageResult:
        from backend.image.comfy.blocks import resolve_subgraph
        from backend.image.comfy.graph import decode_image

        graph = resolve_subgraph("rmbg", {"image": decode_image(image_b64)}, job_id=job_id)
        return await self._executor.execute(graph, progress=progress, job_id=job_id)
