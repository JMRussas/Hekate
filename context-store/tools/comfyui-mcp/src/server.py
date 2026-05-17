#!/usr/bin/env python3
#  ComfyUI MCP Server — FastAPI edition
#
#  FastAPI application wrapping the ComfyUI REST API for image generation,
#  workflow management, queue monitoring, and video generation. Also exposes
#  tools via MCP.
#
#  Endpoints: GET /health, GET /workflows, GET /queue
#  MCP Tools: generate_image, generate_video, upload_image, download_image,
#             get_result, list_workflows, queue_status
#
#  Depends on: fastapi, uvicorn, mcp, httpx
#  Used by:    Claude Code (registered via .mcp.json)

import asyncio
import base64
import copy
import json
import logging
import mimetypes
import os
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("comfyui-mcp")
logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

# --- App constants ---

VERSION = "0.1.0"
START_TIME = datetime.now(timezone.utc)

# --- Configuration ---

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://192.168.1.164:8188")
WORKFLOWS_DIR = os.environ.get(
    "COMFYUI_WORKFLOWS_DIR",
    str(Path(__file__).parent / "workflows"),
)

# --- FastAPI app ---

app = FastAPI(
    title="ComfyUI MCP Server",
    description="Image generation and workflow management via ComfyUI",
    version="0.1.0",
)

# --- MCP server (mounted on FastAPI) ---

mcp = FastMCP(
    "comfyui",
    instructions="ComfyUI image generation and workflow management",
)


# --- Video workflow registry ---
#
# Each video workflow uses node_id+field injection (more precise than
# class_type scanning) so we can target specific knobs in known graph layouts.

VIDEO_WORKFLOWS: dict[str, dict] = {
    "wan_i2v": {
        "file": "wan_i2v.json",
        "description": "Wan2.1 Image-to-Video — high-quality character idle animation (14B model)",
        "timeout": 600,
        "params": {
            "prompt":          {"node": "6",  "field": "text",            "required": True},
            "negative":        {"node": "7",  "field": "text",            "default": "static, still, frozen, blurry, worst quality, low quality, watermark, text, deformed"},
            "seed":            {"node": "3",  "field": "seed",            "type": "int",   "default": -1},
            "steps":           {"node": "3",  "field": "steps",           "type": "int",   "default": 20},
            "cfg":             {"node": "3",  "field": "cfg",             "type": "float", "default": 6.0},
            "width":           {"node": "50", "field": "width",           "type": "int",   "default": 512},
            "height":          {"node": "50", "field": "height",          "type": "int",   "default": 768},
            "length":          {"node": "50", "field": "length",          "type": "int",   "default": 49},
            "frame_rate":      {"node": "60", "field": "frame_rate",      "type": "int",   "default": 16},
            "pingpong":        {"node": "60", "field": "pingpong",        "type": "bool",  "default": True},
            "filename_prefix": {"node": "60", "field": "filename_prefix", "default": "wan_i2v"},
        },
        "source_image_slot": {"node": "52", "field": "image"},
    },
    "animatediff_img2vid": {
        "file": "animatediff_img2vid.json",
        "description": "AnimateDiff idle animation loop from character image",
        "timeout": 300,
        "params": {
            "prompt":          {"node": "20", "field": "text",            "required": True},
            "negative":        {"node": "21", "field": "text",            "default": "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, fewer digits, cropped, worst quality, low quality, jpeg artifacts, signature, watermark, blurry, deformed, ugly"},
            "seed":            {"node": "30", "field": "seed",            "type": "int",   "default": -1},
            "steps":           {"node": "30", "field": "steps",           "type": "int",   "default": 20},
            "cfg":             {"node": "30", "field": "cfg",             "type": "float", "default": 7.0},
            "denoise":         {"node": "30", "field": "denoise",         "type": "float", "default": 0.40},
            "motion_scale":    {"node": "3",  "field": "motion_scale",    "type": "float", "default": 1.1},
            "width":           {"node": "11", "field": "width",           "type": "int",   "default": 768},
            "height":          {"node": "11", "field": "height",          "type": "int",   "default": 1024},
            "frame_rate":      {"node": "50", "field": "frame_rate",      "type": "int",   "default": 8},
            "pingpong":        {"node": "50", "field": "pingpong",        "type": "bool",  "default": True},
            "filename_prefix": {"node": "50", "field": "filename_prefix", "default": "animated"},
        },
        "source_image_slot": {"node": "10", "field": "image"},
    },
}


# --- Helpers ---


async def validate_workflow_models(workflow: dict) -> list[str]:
    """Check that loader nodes in the workflow have their model files present on ComfyUI.
    Returns a list of error strings (empty = all clear)."""
    loaders = {
        "CheckpointLoaderSimple": "ckpt_name",
        "UNETLoader": "unet_name",
        "CLIPLoader": "clip_name",
        "VAELoader": "vae_name",
        "CLIPVisionLoader": "clip_name",
    }
    errors = []
    cache: dict[str, list] = {}
    async with httpx.AsyncClient(timeout=10) as client:
        for node_id, node in workflow.items():
            cls = node.get("class_type", "")
            if cls not in loaders:
                continue
            field = loaders[cls]
            model_name = node.get("inputs", {}).get(field)
            if not model_name:
                continue
            if cls not in cache:
                try:
                    resp = await client.get(f"{COMFYUI_URL}/object_info/{cls}")
                    info = resp.json()
                    required = info.get(cls, {}).get("input", {}).get("required", {})
                    for _, v in required.items():
                        if isinstance(v, list) and len(v) > 0 and isinstance(v[0], list):
                            cache[cls] = v[0]
                            break
                    else:
                        cache[cls] = []
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    log.debug("object_info fetch failed for %s: %s", cls, exc)
                    cache[cls] = []
            available = cache.get(cls, [])
            if available and model_name not in available:
                errors.append(f"Node {node_id} ({cls}): '{model_name}' not found")
    return errors


# --- FastAPI routes ---


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "comfyui-mcp"}


@app.get("/workflows")
async def get_workflows():
    """List available workflow templates."""
    return await list_workflows()


@app.get("/queue")
async def get_queue():
    """Get current ComfyUI queue status."""
    return await queue_status()


# --- MCP Tools ---


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
    workflow_path = Path(WORKFLOWS_DIR) / f"{workflow_name}.json"
    if not workflow_path.exists():
        return {"error": f"Workflow not found: {workflow_path}"}

    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"Failed to load workflow: {e}"}

    if seed < 0:
        seed = random.randint(0, 2**32 - 1)

    positive_set = False
    for _node_id, node in workflow.items():
        cls = node.get("class_type", "")
        inputs = node.get("inputs", {})

        if cls == "CLIPTextEncode" and not positive_set:
            txt = inputs.get("text", "")
            if txt in ("", "PROMPT", "positive prompt", "{{prompt}}"):
                inputs["text"] = prompt
                positive_set = True

        if cls in ("EmptyLatentImage", "EmptySD3LatentImage"):
            inputs["width"] = width
            inputs["height"] = height

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


@mcp.tool()
async def upload_image(
    base64_data: str,
    filename: str = "upload.png",
) -> dict:
    """Upload a base64-encoded image to ComfyUI's input directory.

    Use the returned filename in generate_video as source_image_filename.
    Accepts raw base64 or a data URI (data:image/png;base64,...).

    Args:
        base64_data: Base64-encoded image bytes, with or without data URI prefix.
        filename: Filename to store under in ComfyUI's input folder.
    """
    if "," in base64_data:
        base64_data = base64_data.split(",", 1)[1]

    try:
        image_bytes = base64.b64decode(base64_data)
    except ValueError as e:
        return {"error": f"Failed to decode base64: {e}"}

    content_type = mimetypes.guess_type(filename)[0] or "image/png"
    boundary = "----ComfyUIUpload"

    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode()
    body += f"Content-Type: {content_type}\r\n\r\n".encode()
    body += image_bytes
    body += b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="overwrite"\r\n\r\n'
    body += b"true\r\n"
    body += f"--{boundary}--\r\n".encode()

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{COMFYUI_URL}/upload/image",
                content=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            resp.raise_for_status()
            data = resp.json()
        return {
            "name": data.get("name", filename),
            "subfolder": data.get("subfolder", ""),
            "type": data.get("type", "input"),
        }
    except httpx.HTTPError as e:
        return {"error": f"Upload failed: {e}"}


@mcp.tool()
async def generate_video(
    prompt: str,
    workflow_name: str = "wan_i2v",
    source_image_filename: str = "",
    width: int = 512,
    height: int = 768,
    seed: int = -1,
    steps: int = 20,
) -> dict:
    """Generate a video using a ComfyUI video workflow.

    For image-to-video workflows (wan_i2v, animatediff_img2vid), first call
    upload_image to get a source_image_filename, then pass it here.

    Validates model availability before queueing to fail fast with a clear error.
    Returns a prompt_id for tracking with get_result (use poll=True, timeout=600).

    Args:
        prompt: Text prompt describing the motion/animation.
        workflow_name: "wan_i2v" or "animatediff_img2vid".
        source_image_filename: ComfyUI input filename from upload_image (required for i2v).
        width: Frame width in pixels.
        height: Frame height in pixels.
        seed: Random seed (-1 for random).
        steps: Diffusion steps.
    """
    wf_def = VIDEO_WORKFLOWS.get(workflow_name)
    if not wf_def:
        available = ", ".join(VIDEO_WORKFLOWS.keys())
        return {"error": f"Unknown video workflow '{workflow_name}'. Available: {available}"}

    if wf_def.get("source_image_slot") and not source_image_filename:
        return {
            "error": f"Workflow '{workflow_name}' is image-to-video and requires source_image_filename. "
                     "Call upload_image first, then pass the returned name here.",
        }

    workflow_path = Path(WORKFLOWS_DIR) / wf_def["file"]
    if not workflow_path.exists():
        return {"error": f"Workflow file not found: {workflow_path}"}

    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"Failed to load workflow: {e}"}

    workflow = copy.deepcopy(workflow)

    if seed < 0:
        seed = random.randint(0, 2**32 - 1)

    param_overrides = {
        "prompt": prompt,
        "seed": seed,
        "steps": steps,
        "width": width,
        "height": height,
    }
    for param_name, param_def in wf_def["params"].items():
        value = param_overrides.get(param_name, param_def.get("default"))
        if value is None:
            if param_def.get("required"):
                return {"error": f"Required param '{param_name}' not provided"}
            continue
        node_id = param_def["node"]
        field = param_def["field"]
        if node_id in workflow:
            workflow[node_id]["inputs"][field] = value

    if source_image_filename:
        slot = wf_def.get("source_image_slot")
        if slot:
            node_id, field = slot["node"], slot["field"]
            if node_id in workflow:
                workflow[node_id]["inputs"][field] = source_image_filename

    model_errors = await validate_workflow_models(workflow)
    if model_errors:
        return {
            "error": "Missing models on ComfyUI server",
            "missing_models": model_errors,
            "hint": "Ensure required model files are installed in ComfyUI before generating.",
        }

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
            "estimated_timeout": wf_def["timeout"],
        }
    except httpx.HTTPError as e:
        return {"error": f"Failed to submit video prompt: {e}"}


@mcp.tool()
async def download_image(
    filename: str,
    subfolder: str = "",
    image_type: str = "output",
) -> dict:
    """Download a generated image (or video frame) from ComfyUI and return it as base64.

    Use the filename returned by get_result to fetch the actual bytes.
    Returns a base64-encoded payload and an HTML img tag for embedding.

    Args:
        filename: Filename from get_result (e.g. 'tile_00001.png').
        subfolder: Subfolder within ComfyUI output (usually empty).
        image_type: ComfyUI image type — 'output', 'temp', or 'input'.
    """
    params = urlencode({"filename": filename, "subfolder": subfolder, "type": image_type})
    url = f"{COMFYUI_URL}/view?{params}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data_b64 = base64.b64encode(resp.content).decode("utf-8")
            content_type = resp.headers.get("content-type", "image/png")

        return {
            "filename": filename,
            "url": url,
            "content_type": content_type,
            "size_bytes": len(resp.content),
            "base64": data_b64,
            "html": f'<img src="data:{content_type};base64,{data_b64}" style="max-width:100%;image-rendering:pixelated" />',
        }
    except httpx.HTTPError as e:
        return {"error": f"Failed to download image: {e}", "url": url}


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
    uvicorn.run(app, host="0.0.0.0", port=8000)
