#  Orchestration Engine - Ollama Tool
#
#  Local LLM inference via Ollama HTTP API.
#
#  Depends on: backend/config.py, tools/base.py
#  Used by:    services/executor.py (via tool registry)

import httpx

from backend.config import OLLAMA_DEFAULT_MODEL, OLLAMA_GENERATE_TIMEOUT, OLLAMA_HOSTS
from backend.tools.base import Tool


class LocalLLMTool(Tool):
    name = "local_llm"
    description = (
        "Send a prompt to a local LLM (Ollama) for free inference. "
        "Use this for drafts, summaries, simple code generation, "
        "formatting, and any task that doesn't require Claude-level reasoning."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The prompt to send"},
            "system": {"type": "string", "default": "", "description": "Optional system prompt"},
            "model": {
                "type": "string",
                "default": OLLAMA_DEFAULT_MODEL,
                "description": "Model name",
            },
            "host": {
                "type": "string",
                "enum": list(OLLAMA_HOSTS.keys()),
                "default": "local",
                "description": "Which Ollama host to use",
            },
        },
        "required": ["prompt"],
    }

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._http = http_client

    async def execute(self, params: dict) -> str:
        prompt = params["prompt"]
        system = params.get("system", "")
        model = params.get("model", OLLAMA_DEFAULT_MODEL)
        host_key = params.get("host", "local")

        host_url = OLLAMA_HOSTS.get(host_key, OLLAMA_HOSTS.get("local", "http://localhost:11434"))
        url = f"{host_url}/api/generate"

        body = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            body["system"] = system

        try:
            if self._http:
                resp = await self._http.post(url, json=body, timeout=OLLAMA_GENERATE_TIMEOUT)
            else:
                async with httpx.AsyncClient(timeout=OLLAMA_GENERATE_TIMEOUT) as client:
                    resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "")
        except httpx.ConnectError:
            return f"Error: Ollama not reachable at {host_url}"
        except Exception as e:
            return f"Error: Ollama request failed: {e}"
