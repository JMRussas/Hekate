#  Orchestration Engine - ComfyUI Tool
#
#  Image/asset generation via ComfyUI REST API.
#
#  Depends on: backend/config.py, tools/base.py
#  Used by:    services/executor.py (via tool registry)

import asyncio
import uuid

import httpx

from backend.config import COMFYUI_DEFAULT_CHECKPOINT, COMFYUI_HOSTS, cfg
from backend.tools.base import Tool

POLL_INTERVAL = cfg("comfyui.poll_interval", 2.0)
COMFY_TIMEOUT = cfg("comfyui.timeout", 300)


class GenerateImageTool(Tool):
    name = "generate_image"
    description = (
        "Generate an image using ComfyUI. Provide a text prompt and optional "
        "parameters. The image will be saved to the project workspace."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Text prompt for image generation",
            },
            "negative_prompt": {
                "type": "string",
                "default": "",
                "description": "Negative prompt (things to avoid)",
            },
            "width": {"type": "integer", "default": 1024, "description": "Image width"},
            "height": {"type": "integer", "default": 1024, "description": "Image height"},
            "host": {
                "type": "string",
                "enum": list(COMFYUI_HOSTS.keys()),
                "default": "local",
                "description": "Which ComfyUI host to use",
            },
        },
        "required": ["prompt"],
    }

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._http = http_client

    async def execute(self, params: dict) -> str:
        prompt_text = params["prompt"]
        negative = params.get("negative_prompt", "")
        width = params.get("width", 1024)
        height = params.get("height", 1024)
        host_key = params.get("host", "local")

        host_url = COMFYUI_HOSTS.get(host_key, COMFYUI_HOSTS.get("local", "http://localhost:8188"))
        client_id = uuid.uuid4().hex[:8]

        # Simple text-to-image workflow (SDXL/Flux compatible)
        workflow = _build_txt2img_workflow(prompt_text, negative, width, height)

        try:
            # Use shared client for initial prompt submission
            if self._http:
                resp = await self._http.post(
                    f"{host_url}/prompt",
                    json={"prompt": workflow, "client_id": client_id},
                    timeout=30.0,
                )
            else:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{host_url}/prompt",
                        json={"prompt": workflow, "client_id": client_id},
                    )
            resp.raise_for_status()
            data = resp.json()
            prompt_id = data.get("prompt_id")
            if not prompt_id:
                return "Error: ComfyUI did not return a prompt ID"

            # Poll for completion using a dedicated client (long-lived polling)
            async with httpx.AsyncClient(timeout=COMFY_TIMEOUT) as poll_client:
                elapsed = 0.0
                while elapsed < COMFY_TIMEOUT:
                    await asyncio.sleep(POLL_INTERVAL)
                    elapsed += POLL_INTERVAL

                    hist_resp = await poll_client.get(f"{host_url}/history/{prompt_id}")
                    hist_resp.raise_for_status()
                    history = hist_resp.json()

                    if prompt_id in history:
                        outputs = history[prompt_id].get("outputs", {})
                        # Find image outputs
                        images = []
                        for node_id, node_out in outputs.items():
                            for img in node_out.get("images", []):
                                filename = img.get("filename", "")
                                images.append(f"{host_url}/view?filename={filename}")
                        if images:
                            return "Image generated successfully.\nURLs:\n" + "\n".join(images)
                        return "Workflow completed but no images found in output."

            return f"Error: ComfyUI timed out after {COMFY_TIMEOUT}s"

        except httpx.ConnectError:
            return f"Error: ComfyUI not reachable at {host_url}"
        except Exception as e:
            return f"Error: ComfyUI request failed: {e}"


def _build_txt2img_workflow(prompt: str, negative: str, width: int, height: int) -> dict:
    """Build a minimal SDXL/Flux txt2img workflow."""
    # This is a minimal workflow structure. Users can extend with custom workflows.
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": -1,
                "steps": 20,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": COMFYUI_DEFAULT_CHECKPOINT},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative or "bad quality, blurry", "clip": ["4", 1]},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "orchestration", "images": ["8", 0]},
        },
    }
