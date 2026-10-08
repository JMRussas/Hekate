"""Runner-source provenance (e1/provenance.py; HK-ISSUE-016, root msgs 2135/2144): host-observed, never raises."""

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

from e1 import cli_worker as W
from e1 import provenance as PV

SCOPE = PV.SCOPE


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=W.git_env()).stdout.strip()


def make_src(tmp_path: Path, names=("cli_worker", "pilot", "durable", "acts_durable")) -> tuple[Path, Path, list]:
    """A repo whose SCOPE dir holds an e1 package; returns (repo, src_dir, fake modules)."""
    repo = tmp_path / "repo"
    src = repo / SCOPE
    (src / "e1").mkdir(parents=True)
    mods = []
    for n in names:
        p = src / "e1" / f"{n}.py"
        p.write_text(f"# {n}\n", encoding="utf-8")
        mods.append(SimpleNamespace(__file__=str(p)))
    (repo / "outside.txt").write_text("x\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false", "commit", "-qm", "c0")
    return repo, src, mods


def test_a_clean_tree_records_head_no_dirt_and_the_module_hashes(tmp_path):
    repo, src, mods = make_src(tmp_path)
    rec = PV.observe(src, mods, env=W.git_env())
    g = rec["git"]
    assert (g["head"], g["dirty"], g["dirtyPaths"], g["error"]) == (git(repo, "rev-parse", "HEAD"), False, [], None)
    assert Path(g["toplevel"]).resolve() == repo.resolve()
    assert rec["modules"] == {f"e1/{n}.py": hashlib.sha256((src / "e1" / f"{n}.py").read_bytes()).hexdigest()
                              for n in ("cli_worker", "pilot", "durable", "acts_durable")}
    assert rec["moduleErrors"] == [] and rec["schema"] == PV.SCHEMA and "not loaded bytecode" in rec["label"]
    assert rec["python"]["version"].count(".") == 2 and rec["python"]["implementation"]


def test_dirty_lists_modified_untracked_and_both_ends_of_a_staged_rename_only_in_scope(tmp_path):
    repo, src, mods = make_src(tmp_path)
    (src / "e1" / "pilot.py").write_text("# changed\n", encoding="utf-8")
    (src / "e1" / "new.py").write_text("# new\n", encoding="utf-8")
    git(repo, "mv", f"{SCOPE}/e1/durable.py", f"{SCOPE}/e1/durable2.py")       # staged rename: two paths in -z
    (repo / "outside.txt").write_text("changed\n", encoding="utf-8")            # dirty, but OUTSIDE the scope
    g = PV.observe_git(src, env=W.git_env())
    assert g["dirty"] is True and g["error"] is None
    assert sorted(g["dirtyPaths"]) == sorted([f"{SCOPE}/e1/pilot.py", f"{SCOPE}/e1/new.py", f"{SCOPE}/e1/durable2.py"])


def test_not_a_git_repo_is_recorded_not_raised(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    g = PV.observe_git(plain, env=W.git_env())
    assert (g["head"], g["dirty"], g["toplevel"]) == (None, None, None)
    assert g["error"]["step"] == "rev-parse" and g["error"]["rc"] not in (0, None) and "not a git repository" in g["error"]["stderr"]


def test_git_unavailable_or_hung_is_recorded_with_its_type(tmp_path):
    def missing(*a, **kw):
        raise FileNotFoundError("git")

    def hung(*a, **kw):
        assert kw["timeout"] == PV.GIT_TIMEOUT_S <= 30
        raise subprocess.TimeoutExpired(a[0], kw["timeout"])
    for run, typ in ((missing, "FileNotFoundError"), (hung, "TimeoutExpired")):
        g = PV.observe_git(tmp_path, run=run)
        assert g["head"] is None and g["error"] == {"step": "rev-parse", "rc": None, "type": typ, "stderr": ""}


def test_bounds_on_dirty_paths_stderr_and_modules(tmp_path):
    repo, src, mods = make_src(tmp_path)
    for i in range(PV.DIRTY_MAX + 8):
        (src / f"u{i:03}.txt").write_text("u\n", encoding="utf-8")
    g = PV.observe_git(src, env=W.git_env())
    assert len(g["dirtyPaths"]) == PV.DIRTY_MAX and g["dirtyPathsTruncated"] is True

    def failing(*a, **kw):
        return subprocess.CompletedProcess(a[0], 128, "", "x" * 2000 + "fatal: why")
    err = PV.observe_git(tmp_path, run=failing)["error"]
    assert len(err["stderr"]) == PV.ERR_TAIL and err["stderr"].endswith("fatal: why")

    many = []
    for i in range(PV.MODULES_MAX + 5):
        p = src / "e1" / f"m{i:03}.py"
        p.write_text("#\n", encoding="utf-8")
        many.append(SimpleNamespace(__file__=str(p)))
    rec = PV.observe_modules(src, many + mods)
    assert len(rec["modules"]) == PV.MODULES_MAX and rec["modulesTruncated"] is True


def test_oversized_unreadable_foreign_and_missing_required_modules(tmp_path):
    repo, src, mods = make_src(tmp_path, names=("cli_worker", "pilot", "durable"))       # acts_durable not loaded
    big = src / "e1" / "big.py"
    big.write_bytes(b"#" * (PV.MODULE_BYTES_MAX + 1))
    gone = SimpleNamespace(__file__=str(src / "e1" / "gone.py"))                         # unreadable (absent)
    foreign = SimpleNamespace(__file__=str(tmp_path / "elsewhere.py"))                   # outside src: ignored
    builtin = SimpleNamespace()                                                          # no __file__: ignored
    rec = PV.observe_modules(src, [*mods, SimpleNamespace(__file__=str(big)), gone, foreign, builtin])
    assert "e1/big.py" not in rec["modules"] and "e1/gone.py" not in rec["modules"]
    assert {"module": "e1/big.py", "type": "too_large"} in rec["moduleErrors"]
    assert {"module": "e1/gone.py", "type": "FileNotFoundError"} in rec["moduleErrors"]
    assert {"module": "e1/acts_durable.py", "type": "not_loaded"} in rec["moduleErrors"]
    assert set(rec["modules"]) == {"e1/cli_worker.py", "e1/pilot.py", "e1/durable.py"}


def test_the_real_runner_observes_its_own_loaded_modules():
    import e1.acts_durable, e1.durable, e1.pilot  # noqa: F401,E401 -- the REQUIRED set, as task_runner loads them
    src = Path(PV.__file__).resolve().parents[1]
    rec = PV.observe(src, PV.loaded_e1_modules(), env=W.git_env())
    assert {"e1/provenance.py", "e1/cli_worker.py", "e1/pilot.py", "e1/durable.py", "e1/acts_durable.py"} <= set(rec["modules"])
    assert not [e for e in rec["moduleErrors"] if e["type"] == "not_loaded"]
