"""CodeFileSource — render an indexed codebase's file/directory tree.

The context store stores files with absolute paths and no directory nodes,
so this source synthesizes directory containment by splitting each file's
path relative to the project's rootPath.

One-shot: load() walks the file list once and exits. There is no
subscribe() — when files are re-indexed the user re-runs the viewer.
"""
from __future__ import annotations

from pathlib import PurePath

from context_store_code import CodeClient, DEFAULT_CTX_STORE
from graph import Graph, NodeKind
from source import CanvasSource


def _short_id(uuid: str) -> str:
    return uuid.split("-", 1)[0]


def _relative_parts(file_path: str, root_path: str) -> list[str]:
    """Split file_path into directory parts + filename relative to root_path.
    Falls back to the full path components if root_path doesn't prefix it
    (which can happen when files were indexed before the project's rootPath
    was set, or with mixed path separators)."""
    fp = PurePath(file_path.replace("\\", "/"))
    if root_path:
        rp = PurePath(root_path.replace("\\", "/"))
        try:
            rel = fp.relative_to(rp)
            return list(rel.parts)
        except ValueError:
            pass
    return list(fp.parts)


class CodeFileSource(CanvasSource):
    def __init__(self, project_name: str, *, base_url: str = DEFAULT_CTX_STORE):
        self.project_name = project_name
        self.base_url = base_url
        self.title = f"code · files · {project_name}"

    async def load(self, graph: Graph) -> None:
        async with CodeClient(self.base_url) as client:
            project = await client.get_project_by_name(self.project_name)
            files = await client.list_files(project.id)

        if not files:
            graph.set_status_line(f"{project.name}: no files indexed")
            return

        project_nid = _short_id(project.id)
        graph.add_node(
            project_nid, NodeKind.DIRECTORY.value,
            config={"rootPath": project.root_path, "files": len(files)},
            intent=project.name,
        )

        dir_nodes: dict[tuple[str, ...], str] = {(): project_nid}
        for f in files:
            parts = _relative_parts(f.file_path, project.root_path)
            *dir_parts, filename = parts
            parent_nid = self._ensure_dir(graph, dir_nodes, tuple(dir_parts), project_nid)
            file_nid = _short_id(f.id)
            graph.add_node(
                file_nid, NodeKind.FILE.value,
                config={"nodes": f.node_count},
                intent=filename,
                parent_id=parent_nid,
            )

        graph.set_status_line(
            f"{project.name}: {len(files)} files in {len(dir_nodes) - 1} dirs"
        )
        graph.mark_plan_complete()

    def _ensure_dir(
        self,
        graph: Graph,
        cache: dict[tuple[str, ...], str],
        parts: tuple[str, ...],
        root_nid: str,
    ) -> str:
        if parts in cache:
            return cache[parts]
        parent_parts = parts[:-1]
        parent_nid = self._ensure_dir(graph, cache, parent_parts, root_nid) if parent_parts else root_nid
        dir_nid = f"d_{'_'.join(parts)}"[:32] or root_nid
        graph.add_node(
            dir_nid, NodeKind.DIRECTORY.value, config={},
            intent=parts[-1] + "/",
            parent_id=parent_nid,
        )
        cache[parts] = dir_nid
        return dir_nid
