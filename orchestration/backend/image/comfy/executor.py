#  Hekate Orchestration - ComfyUI Workflow Executor
#
#  Executes ComfyGraph workflows against a ComfyUI server.
#  Handles image upload, prompt submission, polling, and result collection.
#  Simplified from noz-ai's executor + client.
#
#  Depends on: image/comfy/graph.py, image/comfy/blocks/__init__.py, image/base.py
#  Used by:    image/comfy/backend.py
from __future__ import annotations

import asyncio
import base64
import io
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
from PIL import Image

from backend.image.base import ImageResult, JobProgress
from backend.image.comfy.graph import ComfyGraph

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0
TIMEOUT = 300


class ComfyExecutor:
    """Executes ComfyUI workflows via REST API."""

    def __init__(self, base_url: str = "http://localhost:8188"):
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.base_url}/system_stats")
            return resp.status_code == 200
        except Exception:
            return False

    async def upload_image(self, image: Image.Image, name: str) -> str:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        buf.seek(0)

        client = await self._get_client()
        resp = await client.post(
            f"{self.base_url}/upload/image",
            files={"image": (name, buf, "image/png")},
            data={"overwrite": "true"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("name", name)

    async def execute(
        self,
        graph: ComfyGraph,
        progress: JobProgress | None = None,
        job_id: str = "",
    ) -> ImageResult:
        """Execute a ComfyGraph: upload images, submit prompt, poll, collect result."""
        client = await self._get_client()

        # Upload input images
        for upload in graph.uploads:
            uploaded_name = await self.upload_image(upload.image, upload.name)
            logger.debug("Uploaded: %s -> %s", upload.name, uploaded_name)

        # Submit prompt
        client_id = uuid.uuid4().hex[:8]
        resp = await client.post(
            f"{self.base_url}/prompt",
            json={"prompt": graph.prompt, "client_id": client_id},
        )
        resp.raise_for_status()
        data = resp.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            return ImageResult(error="ComfyUI did not return a prompt ID")

        # Poll for completion
        elapsed = 0.0
        while elapsed < TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            try:
                hist_resp = await client.get(f"{self.base_url}/history/{prompt_id}")
                hist_resp.raise_for_status()
                history = hist_resp.json()
            except Exception as e:
                logger.debug("Poll error: %s", e)
                continue

            if prompt_id not in history:
                # Update progress from queue if available
                if progress:
                    try:
                        q_resp = await client.get(f"{self.base_url}/queue")
                        q_data = q_resp.json()
                        running = q_data.get("queue_running", [])
                        for item in running:
                            if len(item) >= 3 and item[1] == prompt_id:
                                # Item is running
                                pass
                    except Exception:
                        pass
                continue

            # Completed — extract images
            outputs = history[prompt_id].get("outputs", {})
            images: list[tuple[str, Image.Image]] = []

            for node_id, node_out in outputs.items():
                for img_info in node_out.get("images", []):
                    filename = img_info.get("filename", "")
                    subfolder = img_info.get("subfolder", "")
                    img_type = img_info.get("type", "output")

                    try:
                        params = {"filename": filename, "type": img_type}
                        if subfolder:
                            params["subfolder"] = subfolder
                        img_resp = await client.get(f"{self.base_url}/view", params=params)
                        img_resp.raise_for_status()
                        img = Image.open(io.BytesIO(img_resp.content))
                        images.append((filename, img))
                    except Exception as e:
                        logger.warning("Failed to fetch image %s: %s", filename, e)

            if not images:
                return ImageResult(error="ComfyUI returned no output images")

            # Take last image as output
            _, output_image = images[-1]
            w, h = output_image.size
            buf = io.BytesIO()
            output_image.save(buf, format="PNG")
            encoded = base64.b64encode(buf.getvalue()).decode("utf-8")

            return ImageResult(image=encoded, width=w, height=h)

        return ImageResult(error=f"ComfyUI timed out after {TIMEOUT}s")
