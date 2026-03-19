#  God registry — discovers gods by scanning gods/*/god.json.
#
#  Used by: deploy script, NSSM setup, Odin (to know available tools
#  and services system-wide).

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("gods.registry")

_GODS_DIR = Path(__file__).parent


def discover_gods(gods_dir: Path | None = None) -> list[dict[str, Any]]:
    """Scan ``gods/*/god.json`` and return metadata for all discovered gods.

    Each returned dict contains the parsed ``god.json`` contents plus:
      - ``path``:  absolute path to the god's directory
      - ``config_path``: absolute path to ``god.json``

    If a ``god.json`` lacks a ``name`` field, the directory name is used.
    Malformed or unreadable files are logged and skipped.
    """
    root = gods_dir or _GODS_DIR
    gods: list[dict[str, Any]] = []

    for god_json_path in sorted(root.glob("*/god.json")):
        try:
            with open(god_json_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping %s: %s", god_json_path, exc)
            continue

        god_dir = god_json_path.parent
        config["path"] = str(god_dir)
        config["config_path"] = str(god_json_path)

        if "name" not in config:
            config["name"] = god_dir.name

        gods.append(config)
        logger.debug("Discovered god: %s at %s", config["name"], god_dir)

    logger.info(
        "Discovered %d god(s): %s", len(gods), [g["name"] for g in gods]
    )
    return gods


def get_god_config(
    name: str, gods_dir: Path | None = None
) -> dict[str, Any] | None:
    """Load configuration for a single god by directory name.

    Returns ``None`` if the god directory or ``god.json`` doesn't exist
    or can't be parsed.
    """
    root = gods_dir or _GODS_DIR
    god_json_path = root / name / "god.json"

    if not god_json_path.exists():
        return None

    try:
        with open(god_json_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load %s: %s", god_json_path, exc)
        return None

    config["path"] = str(god_json_path.parent)
    config["config_path"] = str(god_json_path)
    if "name" not in config:
        config["name"] = name
    return config
