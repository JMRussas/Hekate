import os
import json
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
import glob

# Configuration
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://localhost:8188")
WORKFLOWS_DIR = os.path.join(os.path.dirname(__file__), "workflows")

app = FastAPI(
    title="ComfyUI FastMCP Server",
    description="A FastMCP server to interact with a ComfyUI instance.",
)

# --- Tool definitions ---

@app.get("/tools/list_workflows")
async def list_workflows():
    """
    Scans the designated workflows directory and returns available workflow templates.
    """
    if not os.path.exists(WORKFLOWS_DIR) or not os.path.isdir(WORKFLOWS_DIR):
        return []
    
    workflow_files = glob.glob(os.path.join(WORKFLOWS_DIR, "*.json"))
    workflows = [os.path.splitext(os.path.basename(wf))[0] for wf in workflow_files]
    return workflows

@app.get("/tools/queue_status")
async def queue_status():
    """
    Queries the ComfyUI API to return queue depth and running jobs.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{COMFYUI_URL}/queue")
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect to ComfyUI: {e}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"ComfyUI API error: {e.response.text}")


# --- Models ---

class GenerateImageRequest(BaseModel):
    prompt: str
    workflow_name: str = "default"
    width: int = 512
    height: int = 512
    seed: int = 0


def _inject_params(workflow: dict, prompt: str, width: int, height: int, seed: int) -> dict:
    """Inject generation parameters into a ComfyUI workflow."""
    for node_id, node in workflow.items():
        ct = node.get("class_type", "")
        if ct == "KSampler":
            node["inputs"]["seed"] = seed
            # Follow the positive input reference to find the prompt node
            pos_ref = node["inputs"].get("positive")
            if isinstance(pos_ref, list):
                prompt_node_id = pos_ref[0]
                if prompt_node_id in workflow:
                    workflow[prompt_node_id]["inputs"]["text"] = prompt
        elif ct == "EmptyLatentImage":
            node["inputs"]["width"] = width
            node["inputs"]["height"] = height
    return workflow


@app.post("/tools/generate_image")
async def generate_image(req: GenerateImageRequest):
    """Load a workflow, inject parameters, and queue it on ComfyUI."""
    wf_path = os.path.join(WORKFLOWS_DIR, f"{req.workflow_name}.json")
    if not os.path.exists(wf_path):
        raise HTTPException(status_code=404, detail=f"Workflow '{req.workflow_name}' not found")

    with open(wf_path) as f:
        workflow = json.load(f)

    workflow = _inject_params(workflow, req.prompt, req.width, req.height, req.seed)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{COMFYUI_URL}/prompt", json={"prompt": workflow})
            response.raise_for_status()
            data = response.json()
            return {"prompt_id": data["prompt_id"], "number": data.get("number"), "status": "queued"}
    except httpx.RequestError as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect to ComfyUI: {e}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"ComfyUI API error: {e.response.text}")


@app.get("/tools/get_result")
async def get_result(job_id: str):
    """Retrieve the result of a queued generation job by its prompt_id."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{COMFYUI_URL}/history/{job_id}")
            response.raise_for_status()
            history = response.json()
    except httpx.RequestError as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect to ComfyUI: {e}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"ComfyUI API error: {e.response.text}")

    if job_id not in history:
        return {"status": "pending", "job_id": job_id}

    job = history[job_id]
    completed = job.get("status", {}).get("completed", False)
    images = []
    for node_id, output in job.get("outputs", {}).items():
        for img in output.get("images", []):
            images.append({
                "filename": img["filename"],
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
                "url": f"{COMFYUI_URL}/view?filename={img['filename']}&subfolder={img.get('subfolder', '')}&type={img.get('type', 'output')}",
            })

    return {
        "status": "completed" if completed else "running",
        "job_id": job_id,
        "images": images,
    }


# --- Health Check ---
@app.get("/health")
async def health_check():
    """
    Checks if the server can connect to the ComfyUI instance.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(COMFYUI_URL)
            response.raise_for_status()
        return {"status": "ok", "comfyui_url": COMFYUI_URL}
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        raise HTTPException(
            status_code=503,
            detail=f"Unable to connect to ComfyUI at {COMFYUI_URL}. Error: {str(e)}",
        )

if __name__ == "__main__":
    import uvicorn
    # Create workflows directory if it doesn't exist
    if not os.path.exists(WORKFLOWS_DIR):
        os.makedirs(WORKFLOWS_DIR)
        print(f"Created workflows directory at: {WORKFLOWS_DIR}")
        # Create a dummy workflow file for testing
        dummy_workflow_path = os.path.join(WORKFLOWS_DIR, "default.json")
        with open(dummy_workflow_path, "w") as f:
            json.dump({"name": "default", "steps": []}, f)
        print(f"Created dummy workflow: {dummy_workflow_path}")

    uvicorn.run(app, host="0.0.0.0", port=8000)
