#  Hekate Orchestration - Composable Subgraph System
#
#  Three layers:
#  1. JSON subgraph files — ComfyUI API-format graph templates with {{placeholder}} variables
#  2. Python translators — per-architecture logic that picks subgraphs and fills params
#  3. Resolver (this module) — loads JSON, resolves placeholders, remaps node IDs
#
#  Ported from noz-ai.
#
#  Depends on: image/comfy/graph.py, image/seed.py
#  Used by:    image/comfy/backend.py
from __future__ import annotations

import importlib
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from backend.image.comfy.graph import Builder, ComfyGraph, ImageUpload, decode_image

logger = logging.getLogger(__name__)

_MODEL_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")

# Configurable paths — set by backend.start()
_comfyui_dir: str = ""
_models_dir: str = ""


def configure(comfyui_dir: str = "", models_dir: str = "") -> None:
    global _comfyui_dir, _models_dir
    _comfyui_dir = comfyui_dir
    _models_dir = models_dir


def scan_model_dir(subfolder: str) -> set[str]:
    files: set[str] = set()
    for base in [Path(_comfyui_dir) / "ComfyUI" / "models" / subfolder, Path(_models_dir) / subfolder]:
        if base.exists():
            for f in base.iterdir():
                if f.is_file() and f.suffix.lower() in _MODEL_EXTENSIONS:
                    files.add(f.name)
    return files


_BLOCKS_DIR = Path(__file__).parent
_subgraph_cache: dict[str, dict] = {}
_translator_cache: dict[str, Any] = {}

_TYPE_COERCE = {
    "string": str,
    "int": int,
    "float": float,
    "boolean": bool,
}


def _load_subgraph(name: str) -> dict:
    if name in _subgraph_cache:
        return _subgraph_cache[name]
    path = _BLOCKS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Subgraph not found: {path}")
    with open(path) as f:
        data = json.load(f)
    _subgraph_cache[name] = data
    return data


def _clear_cache():
    _subgraph_cache.clear()
    _translator_cache.clear()


_PLACEHOLDER_RE = re.compile(r"^\{\{(\w+)\}\}$")


def _resolve_value(value: Any, params: dict, input_specs: dict) -> Any:
    if not isinstance(value, str):
        return value
    m = _PLACEHOLDER_RE.match(value)
    if not m:
        return value
    key = m.group(1)
    spec = input_specs.get(key, {})
    input_type = spec.get("type", "string")
    if key in params:
        raw = params[key]
    elif "default" in spec:
        raw = spec["default"]
    else:
        raise ValueError(f"Missing required subgraph input: {key}")
    coerce = _TYPE_COERCE.get(input_type)
    if coerce and not isinstance(raw, coerce):
        raw = coerce(raw)
    return raw


def _is_node_ref(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
    )


def resolve_subgraph(
    name: str,
    params: dict,
    job_id: str = "",
    add_save_node: bool = True,
) -> ComfyGraph:
    subgraph = _load_subgraph(name)
    input_specs = subgraph.get("inputs", {})
    nodes = subgraph["nodes"]
    outputs = subgraph.get("outputs", {})

    counter = 0
    id_map: dict[str, str] = {}
    for local_id in nodes:
        id_map[local_id] = str(counter)
        counter += 1

    uploads: list[ImageUpload] = []

    for key, spec in input_specs.items():
        if spec.get("type") != "image":
            continue
        if key not in params:
            if "default" in spec:
                continue
            raise ValueError(f"Missing required image input: {key}")
        image_value = params[key]
        img = decode_image(image_value)
        img_name = f"block_{name}_{key}_{job_id or uuid.uuid4().hex[:8]}.png"
        uploads.append(ImageUpload(image=img, name=img_name))
        load_id = str(counter)
        counter += 1
        id_map[f"__load_{key}"] = load_id
        params = {**params, key: [load_id, 0]}

    prompt: dict[str, dict] = {}

    for key, spec in input_specs.items():
        if spec.get("type") != "image" or key not in params:
            continue
        load_key = f"__load_{key}"
        if load_key in id_map:
            load_id = id_map[load_key]
            img_name = next(u.name for u in uploads if u.name.startswith(f"block_{name}_{key}_"))
            prompt[load_id] = {
                "class_type": "LoadImage",
                "inputs": {"image": img_name},
                "_meta": {"title": "LoadImage", "group": name},
            }

    for local_id, node_def in nodes.items():
        global_id = id_map[local_id]
        resolved_inputs = {}
        for inp_name, inp_value in node_def["inputs"].items():
            if _is_node_ref(inp_value):
                local_ref_id, out_idx = inp_value
                if local_ref_id in id_map:
                    resolved_inputs[inp_name] = [id_map[local_ref_id], out_idx]
                else:
                    resolved_inputs[inp_name] = inp_value
            else:
                resolved_inputs[inp_name] = _resolve_value(inp_value, params, input_specs)
        prompt[global_id] = {
            "class_type": node_def["class_type"],
            "inputs": resolved_inputs,
            "_meta": {"title": node_def["class_type"], "group": name},
        }

    output_ref = outputs.get("image", {})
    output_node_local = output_ref.get("node", "")
    output_index = output_ref.get("index", 0)
    output_node_id = id_map.get(output_node_local, "0")

    save_node_id = output_node_id
    if add_save_node:
        save_id = str(counter)
        counter += 1
        prefix = f"noz_{job_id}" if job_id else "noz_output"
        prompt[save_id] = {
            "class_type": "SaveImage",
            "inputs": {
                "images": [output_node_id, output_index],
                "filename_prefix": prefix,
            },
            "_meta": {"title": "SaveImage", "group": "output"},
        }
        save_node_id = save_id

    return ComfyGraph(prompt=prompt, uploads=uploads, output_node=save_node_id)


def _load_translator(arch: str) -> Any:
    if arch in _translator_cache:
        return _translator_cache[arch]
    try:
        mod = importlib.import_module(f"backend.image.comfy.blocks.{arch}")
    except ImportError:
        raise ValueError(f"No translator for architecture: {arch}")
    if not hasattr(mod, "translate"):
        raise ValueError(f"Translator module {arch} missing translate() function")
    _translator_cache[arch] = mod
    return mod


def get_available_archs() -> list[str]:
    return [info["name"] for info in get_models_info()]


def get_models_info() -> list[dict]:
    _CHECKPOINT_DIRS = {"flux": "diffusion_models", "zimage": "diffusion_models"}

    results = []
    for path in _BLOCKS_DIR.glob("*.py"):
        if path.name.startswith("_"):
            continue
        name = path.stem
        try:
            mod = importlib.import_module(f"backend.image.comfy.blocks.{name}")
            if not hasattr(mod, "translate"):
                continue
        except ImportError:
            continue

        arch = getattr(mod, "ARCH", name)
        is_default = getattr(mod, "IS_DEFAULT", False)
        checkpoint = getattr(mod, "DEFAULT_CHECKPOINT", "")
        clip_file = getattr(mod, "DEFAULT_CLIP_FILE", "")
        vae_file = getattr(mod, "DEFAULT_VAE_FILE", "")

        errors: list[str] = []
        if checkpoint:
            ckpt_dir = _CHECKPOINT_DIRS.get(arch, "checkpoints")
            if checkpoint not in scan_model_dir(ckpt_dir):
                errors.append(f"missing checkpoint: {checkpoint}")
        if clip_file:
            if clip_file not in scan_model_dir("clip") | scan_model_dir("text_encoders"):
                errors.append(f"missing clip: {clip_file}")
        if vae_file:
            if vae_file not in scan_model_dir("vae"):
                errors.append(f"missing vae: {vae_file}")

        results.append({
            "name": arch,
            "backend": "comfy",
            "checkpoint": checkpoint,
            "valid": len(errors) == 0,
            "is_default": is_default,
            "errors": errors,
        })

    results.sort(key=lambda r: (not r["is_default"], r["name"]))
    return results


def translate_for_chat(arch: str, params: dict, job_id: str = "") -> ComfyGraph:
    lora_prefix = params.pop("lora_prefix", "")
    lora_postfix = params.pop("lora_postfix", "")
    lora_file = params.pop("lora_file", "")
    lora_strength = params.pop("lora_strength", 1.0)
    references = params.pop("references", [])

    prompt = params.get("prompt", "")
    if lora_prefix:
        prompt = f"{lora_prefix}, {prompt}" if prompt else lora_prefix
    if lora_postfix:
        prompt = f"{prompt}, {lora_postfix}" if prompt else lora_postfix
    params["prompt"] = prompt

    translator = _load_translator(arch)
    subgraph_name, resolved_params = translator.translate(params)

    if "seed" in resolved_params and isinstance(resolved_params["seed"], str):
        from backend.image.seed import hash_seed
        resolved_params["seed"] = hash_seed(resolved_params["seed"])

    graph = resolve_subgraph(subgraph_name, resolved_params, job_id=job_id)

    if lora_file:
        _inject_lora(graph, lora_file, lora_strength)

    for ref in references:
        _inject_reference(graph, ref, arch, job_id=job_id)

    return graph


_MODEL_LOADERS = {"UNETLoader", "CheckpointLoaderSimple"}
_CLIP_LOADERS = {"CLIPLoader", "CLIPLoaderGGUF"}


def _inject_lora(graph: ComfyGraph, lora_file: str, strength: float) -> None:
    prompt = graph.prompt
    model_loader_id = None
    model_out_idx = None
    clip_loader_id = None
    clip_out_idx = None

    for nid, node in prompt.items():
        ct = node["class_type"]
        if ct in _MODEL_LOADERS:
            model_loader_id = nid
            if ct == "CheckpointLoaderSimple":
                model_out_idx = 0
                clip_loader_id = nid
                clip_out_idx = 1
            else:
                model_out_idx = 0
        elif ct in _CLIP_LOADERS:
            clip_loader_id = nid
            clip_out_idx = 0

    if (model_loader_id is None or model_out_idx is None
            or clip_loader_id is None or clip_out_idx is None):
        logger.warning("Cannot inject LoRA: no model/clip loader found")
        return

    new_id = str(max(int(nid) for nid in prompt) + 1)
    prompt[new_id] = {
        "class_type": "LoraLoader",
        "inputs": {
            "model": [model_loader_id, model_out_idx],
            "clip": [clip_loader_id, clip_out_idx],
            "lora_name": lora_file,
            "strength_model": strength,
            "strength_clip": strength,
        },
        "_meta": {"title": "LoraLoader", "group": "lora"},
    }

    _rewire_refs(prompt, model_loader_id, model_out_idx, new_id, 0, exclude={new_id})
    _rewire_refs(prompt, clip_loader_id, clip_out_idx, new_id, 1, exclude={new_id})


_SAMPLER_NODES = {"KSampler", "KSamplerAdvanced", "SamplerCustomAdvanced"}


def _find_model_tip(prompt: dict) -> tuple[str, int] | None:
    for nid, node in prompt.items():
        if node["class_type"] in _SAMPLER_NODES:
            model_ref = node["inputs"].get("model")
            if isinstance(model_ref, list) and len(model_ref) == 2:
                return (str(model_ref[0]), model_ref[1])
    return None


def _inject_reference(graph: ComfyGraph, ref_data: dict, arch: str, job_id: str = "") -> None:
    from backend.image.references import REFERENCE_TYPES

    ref_type_key = ref_data.get("type", "composition")
    ref_type = REFERENCE_TYPES.get(ref_type_key)
    if ref_type is None:
        logger.warning("Unknown reference type: %s", ref_type_key)
        return
    if ref_type.arch != "*" and ref_type.arch != arch:
        logger.warning("Reference type %s not compatible with arch %s", ref_type_key, arch)
        return

    prompt = graph.prompt
    strength = ref_data.get("strength", ref_type.default_strength)

    tip = _find_model_tip(prompt)
    if tip is None:
        logger.warning("Cannot inject reference: no sampler node found")
        return
    model_tip_id, model_tip_idx = tip

    next_id = max(int(nid) for nid in prompt) + 1

    img = decode_image(ref_data["data"])
    w, h = img.size
    if w != h:
        size = max(w, h)
        padded = Image.new("RGB", (size, size), (0, 0, 0))
        padded.paste(img, ((size - w) // 2, (size - h) // 2))
        img = padded
    img_name = f"ref_{ref_type_key}_{job_id or uuid.uuid4().hex[:8]}.png"
    graph.uploads.append(ImageUpload(image=img, name=img_name))

    load_img_id = str(next_id); next_id += 1
    prompt[load_img_id] = {
        "class_type": "LoadImage",
        "inputs": {"image": img_name},
        "_meta": {"title": "LoadImage", "group": "reference"},
    }

    ipa_loader_id = str(next_id); next_id += 1
    prompt[ipa_loader_id] = {
        "class_type": "IPAdapterModelLoader",
        "inputs": {"ipadapter_file": ref_type.adapter_model},
        "_meta": {"title": "IPAdapterModelLoader", "group": "reference"},
    }

    clip_vision_id = str(next_id); next_id += 1
    prompt[clip_vision_id] = {
        "class_type": "CLIPVisionLoader",
        "inputs": {"clip_name": ref_type.clip_vision_model},
        "_meta": {"title": "CLIPVisionLoader", "group": "reference"},
    }

    ipa_apply_id = str(next_id); next_id += 1
    weight_type_map = {"composition": "composition", "style": "style transfer"}
    weight_type = weight_type_map.get(ref_type.key, "linear")

    prompt[ipa_apply_id] = {
        "class_type": ref_type.adapter_node,
        "inputs": {
            "model": [model_tip_id, model_tip_idx],
            "ipadapter": [ipa_loader_id, 0],
            "clip_vision": [clip_vision_id, 0],
            "image": [load_img_id, 0],
            "weight": strength,
            "weight_type": weight_type,
            "start_at": 0.0,
            "end_at": 1.0,
            "combine_embeds": "concat",
            "embeds_scaling": "V only",
        },
        "_meta": {"title": ref_type.adapter_node, "group": "reference"},
    }

    _rewire_refs(prompt, model_tip_id, model_tip_idx, ipa_apply_id, 0, exclude={ipa_apply_id})


def _rewire_refs(prompt: dict, old_id: str, old_idx: int, new_id: str, new_idx: int, exclude: set[str] | None = None) -> None:
    exclude = exclude or set()
    for nid, node in prompt.items():
        if nid in exclude:
            continue
        for inp_name, inp_val in node["inputs"].items():
            if (isinstance(inp_val, list) and len(inp_val) == 2
                    and str(inp_val[0]) == str(old_id) and inp_val[1] == old_idx):
                node["inputs"][inp_name] = [new_id, new_idx]
