"""Publication configuration checks; no services, CLI workers or data stores start."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def admin(monkeypatch):
    return load("hades/server.py", "publication_admin", monkeypatch)


def test_relative_compose_and_python_marker_use_configured_paths(admin, tmp_path, monkeypatch):
    source = tmp_path / "source with spaces"
    config = tmp_path / "services.json"
    config.write_text(json.dumps({
        "services": {"engine": {"app": "python"}, "explicit": {"app": "custom-worker.exe"}},
        "infra": {"compose_file": "context-store/docker-compose.yml"},
    }))
    monkeypatch.setattr(admin, "SERVICES_FILE", config)
    monkeypatch.setattr(admin, "SOURCE_ROOT", source)
    interpreter = str(tmp_path / "Python tools" / "python.exe")
    monkeypatch.setenv("HEKATE_PYTHON", interpreter)
    services, infra = admin._load_services()
    assert services["engine"]["app"] == interpreter
    assert services["explicit"]["app"] == "custom-worker.exe"
    assert Path(infra["compose_file"]) == source / "context-store/docker-compose.yml"


def test_absolute_compose_is_preserved_and_python_uses_current_interpreter(admin, tmp_path, monkeypatch):
    compose = str(tmp_path / "separate install" / "docker-compose.yml")
    config = tmp_path / "services.json"
    config.write_text(json.dumps({"services": {"engine": {"app": "python"}}, "infra": {"compose_file": compose}}))
    monkeypatch.setattr(admin, "SERVICES_FILE", config)
    monkeypatch.delenv("HEKATE_PYTHON", raising=False)
    services, infra = admin._load_services()
    assert services["engine"]["app"] == sys.executable
    assert infra["compose_file"] == compose


def test_conversation_mining_is_opt_in_and_uses_exact_configured_directories(tmp_path, monkeypatch):
    monkeypatch.delenv("HEKATE_CLAUDE_LOG_DIRS", raising=False)
    miner = load("orchestration/backend/services/learning/mine_patterns.py", "publication_miner", monkeypatch)
    learner = load("orchestration/backend/services/learning/execution_learner.py", "publication_learner", monkeypatch)
    assert miner.DIRS == []
    assert learner._CONVERSATION_DIRS == []
    assert miner.OUTPUT == ROOT / "orchestration/backend/services/learning/conversation_patterns.json"
    first, second = tmp_path / "logs one", tmp_path / "logs two"
    monkeypatch.setenv("HEKATE_CLAUDE_LOG_DIRS", str(first) + miner.os.pathsep + str(second))
    miner = load("orchestration/backend/services/learning/mine_patterns.py", "publication_configured_miner", monkeypatch)
    learner = load("orchestration/backend/services/learning/execution_learner.py", "publication_configured_learner", monkeypatch)
    assert miner.DIRS == [first, second]
    assert learner._CONVERSATION_DIRS == [first, second]
