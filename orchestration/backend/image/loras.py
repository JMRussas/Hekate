#  Hekate Orchestration - LoRA Discovery
#
#  Scans models_dir/loras/ for .safetensors + companion .json metadata.
#  Ported from noz-ai.
#
#  Depends on: image/comfy/blocks/__init__.py (scan_model_dir)
#  Used by:    tools/comfyui.py
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_models_dir: str = ""


def configure(models_dir: str) -> None:
    global _models_dir
    _models_dir = models_dir


@dataclass
class LoraEntry:
    key: str
    file: str
    name: str
    description: str
    arch: str
    prompt_prefix: str = ""
    prompt_postfix: str = ""
    strength: float = 1.0
    keywords: list[str] = field(default_factory=list)


_cache: dict[str, LoraEntry] | None = None


def _derive_key(filename: str) -> str:
    name = filename.rsplit(".", 1)[0]
    name = re.sub(r"[^a-zA-Z0-9]+", "-", name)
    return name.strip("-").lower()


def discover_loras() -> dict[str, LoraEntry]:
    global _cache
    if _cache is not None:
        return _cache

    loras_path = Path(_models_dir) / "loras"
    if not loras_path.is_dir():
        _cache = {}
        return _cache

    result: dict[str, LoraEntry] = {}
    for safetensors in loras_path.glob("*.safetensors"):
        json_path = safetensors.with_suffix(".json")
        if not json_path.exists():
            continue

        try:
            with open(json_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        name = meta.get("name", "") or safetensors.stem
        arch = meta.get("arch", "")
        keywords = meta.get("keywords", [])
        if not arch or not keywords:
            continue

        description = meta.get("description", "") or f"{name} style ({', '.join(keywords)})"
        key = _derive_key(safetensors.name)
        result[key] = LoraEntry(
            key=key, file=safetensors.name, name=name, description=description,
            arch=arch, prompt_prefix=meta.get("prompt_prefix", ""),
            prompt_postfix=meta.get("prompt_postfix", ""),
            strength=float(meta.get("strength", 1.0)), keywords=keywords,
        )

    logger.info("Discovered %d LoRAs in %s", len(result), loras_path)
    _cache = result
    return result


def reload_loras():
    global _cache
    _cache = None


def get_loras_for_arch(arch: str) -> dict[str, LoraEntry]:
    return {k: v for k, v in discover_loras().items() if v.arch == arch}


def get_lora_enum() -> list[str]:
    return ["none"] + sorted(discover_loras().keys())


def get_lora_tool_description() -> str:
    loras = discover_loras()
    if not loras:
        return "No style LoRAs available."
    by_arch: dict[str, list[LoraEntry]] = {}
    for entry in loras.values():
        by_arch.setdefault(entry.arch, []).append(entry)
    lines = ["Available styles (by model):"]
    for arch in sorted(by_arch):
        entries = sorted(by_arch[arch], key=lambda e: e.key)
        parts = [f"{e.key} ({e.description})" for e in entries]
        lines.append(f"  {arch}: {', '.join(parts)}")
    lines.append("Use 'none' for the base model without style modification.")
    return "\n".join(lines)
