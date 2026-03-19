#!/usr/bin/env python3
#  ComfyUI MCP Server
#
#  FastMCP server wrapping the ComfyUI REST API for image generation,
#  workflow management, and queue monitoring.
#
#  Tools: generate_image, get_result, list_workflows, queue_status
#
#  Depends on: mcp, httpx
#  Used by:    Claude Code (registered via .mcp.json)

import asyncio
import json
import logging
import os
import random
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("comfyui-mcp")
logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

# --- Configuration ---

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://192.168.1.164:8188")
WORKFLOWS_DIR = os.environ.get(
    "COMFYUI_WORKFLOWS_DIR",
    str(Path(__file__).parent / "workflows"),
)

mcp = FastMCP(
    "comfyui",
    instructions="ComfyUI image generation and workflow management",
)


# --- Tools ---


@mcp.tool()
async def list_workflows() -> dict:
    """Scan the workflows directory for available workflow templates (.json files).

    Returns a list of workflow names with metadata (node count, file path).
    """
    workflows_path = Path(WORKFLOWS_DIR)
    if not workflows_path.exists():
        return {
            "workflows": [],
            "count": 0,
            "error": f"Workflows directory not found: {WORKFLOWS_DIR}",
        }

    workflows = []
    for f in sorted(workflows_path.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            workflows.append(
                {
                    "name": f.stem,
                    "file": str(f),
                    "node_count": len(data) if isinstance(data, dict) else 0,
                }
            )
        except (json.JSONDecodeError, OSError):
            workflows.append({"name": f.stem, "file": str(f), "error": "invalid JSON"})

    return {"workflows": workflows, "count": len(workflows)}


@mcp.tool()
async def queue_status() -> dict:
    """Query the ComfyUI API for current queue depth and running jobs.

    Returns pending and running job counts with prompt IDs.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{COMFYUI_URL}/queue")
            resp.raise_for_status()
            data = resp.json()

        pending = data.get("queue_pending", [])
        running = data.get("queue_running", [])

        return {
            "pending_count": len(pending),
            "running_count": len(running),
            "pending": [{"prompt_id": item[1]} for item in pending] if pending else [],
            "running": [{"prompt_id": item[1]} for item in running] if running else [],
        }
    except httpx.HTTPError as e:
        return {
            "error": f"Failed to query ComfyUI queue: {e}",
            "pending_count": 0,
            "running_count": 0,
        }


@mcp.tool()
async def generate_image(
    prompt: str,
    workflow_name: str = "default",
    width: int = 512,
    height: int = 512,
    seed: int = -1,
) -> dict:
    """Generate an image using a ComfyUI workflow template.

    Loads the named workflow JSON, injects the prompt text, dimensions, and seed,
    then posts to the ComfyUI /prompt endpoint. Returns a prompt_id for tracking.

    Args:
        prompt: Text prompt describing the image to generate.
        workflow_name: Name of a workflow template (without .json extension).
        width: Image width in pixels.
        height: Image height in pixels.
        seed: Random seed (-1 for random).
    """
    # Load workflow template
    workflow_path = Path(WORKFLOWS_DIR) / f"{workflow_name}.json"
    if not workflow_path.exists():
        return {"error": f"Workflow not found: {workflow_path}"}

    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"Failed to load workflow: {e}"}

    if seed < 0:
        seed = random.randint(0, 2**32 - 1)

    # Inject parameters into known node types
    positive_set = False
    for _node_id, node in workflow.items():
        cls = node.get("class_type", "")
        inputs = node.get("inputs", {})

        # Positive prompt — first empty CLIPTextEncode or explicit placeholder
        if cls == "CLIPTextEncode" and not positive_set:
            txt = inputs.get("text", "")
            if txt in ("", "PROMPT", "positive prompt", "{{prompt}}"):
                inputs["text"] = prompt
                positive_set = True

        # Dimensions — EmptyLatentImage or EmptySD3LatentImage
        if cls in ("EmptyLatentImage", "EmptySD3LatentImage"):
            inputs["width"] = width
            inputs["height"] = height

        # Seed — KSampler, KSamplerAdvanced, RandomNoise
        if cls in ("KSampler", "KSamplerAdvanced"):
            inputs["seed"] = seed
        if cls == "RandomNoise":
            inputs["noise_seed"] = seed

    client_id = uuid.uuid4().hex
    payload = {"prompt": workflow, "client_id": client_id}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{COMFYUI_URL}/prompt", json=payload)
            resp.raise_for_status()
            data = resp.json()

        prompt_id = data.get("prompt_id", "")
        if "error" in data:
            return {"error": data["error"], "node_errors": data.get("node_errors", {})}
        if not prompt_id:
            return {"error": "ComfyUI returned no prompt_id", "response": data}
        return {
            "prompt_id": prompt_id,
            "client_id": client_id,
            "seed": seed,
            "width": width,
            "height": height,
            "workflow": workflow_name,
            "status": "queued",
        }
    except httpx.HTTPError as e:
        return {"error": f"Failed to submit prompt: {e}"}


@mcp.tool()
async def get_result(
    prompt_id: str,
    poll: bool = False,
    timeout: int = 120,
) -> dict:
    """Retrieve the result of a completed image generation job.

    Checks the ComfyUI /history endpoint for the given prompt_id.
    Returns image filenames and download URLs if complete, or current status.

    Args:
        prompt_id: The prompt_id returned by generate_image.
        poll: If True, keep polling until the job completes or timeout is reached.
        timeout: Maximum seconds to wait when polling (default 120).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    interval = 2

    while True:
        result = await _fetch_result(prompt_id)
        if not poll or result.get("status") in ("completed", "failed", "not_found"):
            return result
        if asyncio.get_event_loop().time() >= deadline:
            result["timeout"] = True
            return result
        await asyncio.sleep(interval)


async def _fetch_result(prompt_id: str) -> dict:
    """Single fetch of job result from ComfyUI history + queue."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        return {"error": f"Failed to fetch history: {e}", "prompt_id": prompt_id}

    if prompt_id not in data:
        # Not in history yet — check if still queued/running
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{COMFYUI_URL}/queue")
                resp.raise_for_status()
                queue = resp.json()

            pending_ids = [item[1] for item in queue.get("queue_pending", [])]
            running_ids = [item[1] for item in queue.get("queue_running", [])]

            if prompt_id in running_ids:
                return {"prompt_id": prompt_id, "status": "running"}
            elif prompt_id in pending_ids:
                return {"prompt_id": prompt_id, "status": "pending"}
            else:
                return {"prompt_id": prompt_id, "status": "not_found"}
        except httpx.HTTPError:
            return {"prompt_id": prompt_id, "status": "unknown"}

    entry = data[prompt_id]
    outputs = entry.get("outputs", {})

    images = []
    for _node_id, node_output in outputs.items():
        for img in node_output.get("images", []):
            filename = img.get("filename", "")
            subfolder = img.get("subfolder", "")
            img_type = img.get("type", "output")
            view_url = (
                f"{COMFYUI_URL}/view?filename={quote(filename)}"
                f"&subfolder={quote(subfolder)}&type={quote(img_type)}"
            )
            images.append({
                "filename": filename,
                "subfolder": subfolder,
                "type": img_type,
                "url": view_url,
            })

    status_info = entry.get("status", {})
    status_msg = status_info.get("status_str", "")
    completed = status_info.get("completed", False)
    result = {
        "prompt_id": prompt_id,
        "status": "completed" if completed else "failed",
        "images": images,
        "image_count": len(images),
    }
    if not completed and status_msg:
        result["status_message"] = status_msg
    if not completed:
        result["messages"] = status_info.get("messages", [])
    return result


# --- Entry point ---

if __name__ == "__main__":
    log.info("ComfyUI URL: %s", COMFYUI_URL)
    log.info("Workflows dir: %s", WORKFLOWS_DIR)
    mcp.run()
