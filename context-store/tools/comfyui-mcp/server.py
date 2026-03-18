import os
import json
from pathlib import Path
import requests
import uvicorn
from fastapi import FastAPI

# Read ComfyUI URL from environment variable, with a default
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://localhost:8188")
WORKFLOWS_DIR = os.environ.get("COMFYUI_WORKFLOWS_DIR", os.path.join(os.path.dirname(__file__), "workflows"))

# --- FastMCP Server Setup ---
app = FastAPI(
    title="ComfyUI MCP Server",
    description="A FastMCP server to interact with a ComfyUI instance.",
)

@app.on_event("startup")
async def startup_event():
    """
    On startup, perform a health check on the configured ComfyUI instance.
    If the instance is not available, the server will fail to start.
    """
    print("--- ComfyUI MCP Server Startup ---")
    print(f"Attempting to connect to ComfyUI at: {COMFYUI_URL}")
    
    # A simple GET to the root of the ComfyUI instance should suffice as a health check.
    health_check_url = f"{COMFYUI_URL}/"
    
    try:
        # Using a timeout to prevent hanging indefinitely
        response = requests.get(health_check_url, timeout=10)
        response.raise_for_status()  # Raises HTTPError for bad responses (4xx or 5xx)
        print("✅ ComfyUI instance is healthy and reachable.")
    except requests.exceptions.RequestException as e:
        print(f"❌ CRITICAL: Could not connect to ComfyUI at {health_check_url}.")
        print(f"   Please ensure the ComfyUI instance is running and accessible at that URL.")
        print(f"   You can configure the URL using the COMFYUI_URL environment variable.")
        print(f"   Error details: {e}")
        # Raising an exception here will stop the FastAPI server from starting
        raise RuntimeError(f"Failed to connect to ComfyUI: {e}") from e
    print("--- Startup complete. Server is running. ---")


@app.get("/")
async def get_root():
    """
    Root endpoint to confirm the server is running.
    """
    return {"status": "ok", "message": "ComfyUI MCP Server is running."}


@app.get("/tools/list_workflows")
async def list_workflows():
    """
    Scan the workflows directory for available workflow templates (.json files).
    Returns a list of workflow names and their file paths.
    """
    workflows_path = Path(WORKFLOWS_DIR)
    if not workflows_path.exists():
        return {"workflows": [], "error": f"Workflows directory not found: {WORKFLOWS_DIR}"}

    workflows = []
    for f in sorted(workflows_path.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            workflows.append({
                "name": f.stem,
                "file": str(f),
                "node_count": len(data) if isinstance(data, dict) else 0,
            })
        except (json.JSONDecodeError, OSError):
            workflows.append({"name": f.stem, "file": str(f), "error": "invalid JSON"})

    return {"workflows": workflows, "count": len(workflows)}


@app.get("/tools/queue_status")
async def queue_status():
    """
    Query the ComfyUI API for current queue depth and running jobs.
    """
    try:
        resp = requests.get(f"{COMFYUI_URL}/queue", timeout=10)
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
    except requests.exceptions.RequestException as e:
        return {"error": f"Failed to query ComfyUI queue: {e}", "pending_count": 0, "running_count": 0}

if __name__ == "__main__":
    # Using a default port for the MCP server, can be overridden by PORT env var.
    port = int(os.environ.get("PORT", 8012))
    print(f"Starting server on http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
