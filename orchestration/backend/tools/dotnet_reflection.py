#  Orchestration Engine - .NET Reflection Tool
#
#  Wraps the dotnet-reflector console app to extract type metadata
#  from .NET assemblies. Used by the C# planner prompt to generate
#  function-level decomposition with typed contracts.
#
#  Depends on: tools/base.py, tools/dotnet-reflector/ (.NET console app)
#  Used by:    tools/registry.py, services/planner.py

import asyncio
import json
import logging
import shutil
from pathlib import Path

from backend.tools.base import Tool

logger = logging.getLogger("orchestration.tools.dotnet_reflection")

# Path to the reflector project
_REFLECTOR_DIR = Path(__file__).resolve().parent.parent.parent / "tools" / "dotnet-reflector"


def is_dotnet_available() -> bool:
    """Check if the dotnet CLI is available on this machine."""
    return shutil.which("dotnet") is not None


async def _run_subprocess(cmd: list[str], cwd: str | None = None, timeout: float = 60) -> tuple[int, str, str]:
    """Run a subprocess asynchronously and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", "Process timed out"
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


async def build_project(csproj_path: str) -> tuple[bool, str]:
    """Build a .NET project and return (success, output_dll_path_or_error)."""
    csproj = Path(csproj_path)
    if not csproj.exists():
        return False, f"Project file not found: {csproj_path}"

    code, stdout, stderr = await _run_subprocess(
        ["dotnet", "build", str(csproj), "-c", "Release", "--nologo", "-v", "q"],
        timeout=120,
    )
    if code != 0:
        return False, f"Build failed:\n{stderr or stdout}"

    # Find the output DLL
    project_dir = csproj.parent
    project_name = csproj.stem
    # Check common output paths
    for pattern in [
        project_dir / "bin" / "Release" / "**" / f"{project_name}.dll",
    ]:
        matches = list(project_dir.glob(f"bin/Release/**/{project_name}.dll"))
        if matches:
            return True, str(matches[0])

    return False, "Build succeeded but could not find output DLL"


async def reflect_assembly(
    assembly_path: str,
    namespace_filter: str | None = None,
) -> dict:
    """Run the reflector against an assembly and return parsed JSON.

    Args:
        assembly_path: Path to the .dll to reflect.
        namespace_filter: Optional namespace prefix to filter types.

    Returns:
        Parsed reflection metadata dict.

    Raises:
        RuntimeError: If the reflector fails.
    """
    reflector_dll = _REFLECTOR_DIR / "bin" / "Release" / "net8.0" / "dotnet-reflector.dll"

    # Build the reflector if not already built
    if not reflector_dll.exists():
        code, _, stderr = await _run_subprocess(
            ["dotnet", "build", str(_REFLECTOR_DIR / "dotnet-reflector.csproj"), "-c", "Release", "--nologo", "-v", "q"],
            timeout=60,
        )
        if code != 0:
            raise RuntimeError(f"Failed to build reflector: {stderr}")

    cmd = ["dotnet", str(reflector_dll), assembly_path]
    if namespace_filter:
        cmd.extend(["--namespace", namespace_filter])

    code, stdout, stderr = await _run_subprocess(cmd, timeout=30)
    if code != 0:
        raise RuntimeError(f"Reflection failed: {stderr}")

    return json.loads(stdout)


def format_type_map(reflection_data: dict) -> str:
    """Format reflection data as a human-readable type map for LLM prompts.

    Returns a structured text representation suitable for injection into
    system prompts or context blocks.
    """
    lines = []
    assembly_name = reflection_data.get("assembly_name", "Unknown")
    lines.append(f"Assembly: {assembly_name}")
    lines.append("")

    for cls in reflection_data.get("classes", []):
        kind = cls.get("kind", "class")
        name = cls.get("name", "?")
        ns = cls.get("namespace", "")
        full_name = f"{ns}.{name}" if ns else name

        header = f"{kind} {full_name}"
        if cls.get("base_class"):
            header += f" : {cls['base_class']}"
        if cls.get("interfaces"):
            ifaces = ", ".join(cls["interfaces"])
            header += f", {ifaces}" if cls.get("base_class") else f" : {ifaces}"

        lines.append(header)

        # Constructors
        for ctor in cls.get("constructors") or []:
            params = ", ".join(f"{p['type']} {p['name']}" for p in ctor.get("parameters", []))
            lines.append(f"  ctor({params})")

        # Properties
        for prop in cls.get("properties") or []:
            accessors = ""
            if prop.get("has_getter"):
                accessors += "get; "
            if prop.get("has_setter"):
                accessors += "set; "
            lines.append(f"  {prop['type']} {prop['name']} {{ {accessors.strip()} }}")

        # Methods
        for method in cls.get("methods", []):
            lines.append(f"  {method['signature']}")

        lines.append("")

    return "\n".join(lines)


class DotNetReflectionTool(Tool):
    """Tool for extracting .NET assembly type metadata via reflection."""

    name = "dotnet_reflection"
    description = (
        "Reflect on a .NET assembly or project to extract type metadata "
        "(classes, methods, signatures, dependencies). Useful for understanding "
        "existing code structure before generating implementations."
    )
    parameters = {
        "type": "object",
        "properties": {
            "assembly_path": {
                "type": "string",
                "description": "Path to a .dll assembly file to reflect on.",
            },
            "csproj_path": {
                "type": "string",
                "description": "Path to a .csproj file. Will build first, then reflect on the output.",
            },
            "namespace_filter": {
                "type": "string",
                "description": "Optional namespace prefix to filter types (e.g., 'MyApp.Services').",
            },
        },
        "required": [],
    }

    async def execute(self, params: dict) -> str:
        assembly_path = params.get("assembly_path")
        csproj_path = params.get("csproj_path")
        ns_filter = params.get("namespace_filter")

        if not assembly_path and not csproj_path:
            return "Error: Provide either assembly_path or csproj_path."

        # Build from csproj if needed
        if csproj_path and not assembly_path:
            success, result = await build_project(csproj_path)
            if not success:
                return f"Error: {result}"
            assembly_path = result

        try:
            data = await reflect_assembly(assembly_path, ns_filter)
            return format_type_map(data)
        except Exception as e:
            return f"Error: {e}"
