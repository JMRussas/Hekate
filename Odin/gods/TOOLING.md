# Language Tooling Matrix

Each language has analysis tooling that determines how deep planning can go (L1-L5).
L5 = executable spec — a tool writes the code directly, no LLM needed.

## Levels

| Level | Name | What's defined |
|-------|------|---------------|
| L1 | Rough tasks | Title, type, wave |
| L2 | Specs | Description, affected files, deps, complexity |
| L3 | Implementation | Code patterns, test strategy, edge cases |
| L4 | Exact changes | Method signatures, return types, parameter types |
| L5 | Executable spec | Full code body — tool applies directly |

## Languages

### C# — Roslyn (L5)

- **Service**: HekateServer (port 5110)
- **Engine**: HekateMcp.Server.exe (Roslyn-powered)
- **SDK**: .NET 8.0 + 10.0
- **Capabilities**: Full AST, type resolution, find implementations/usages/patterns, contract checking, project dependency graph, LLM consensus, 32 MCP tools
- **Max depth**: L5

### Python — Jedi + Worker (L5)

- **Service**: HekatePythonWorker (port 9200)
- **Engine**: Jedi 0.19.2 + HekateMcp.Worker.Python
- **Runtime**: Python 3.11 (NOT 3.14 — broken FastAPI)
- **Capabilities**: Type inference + completion, find definitions/references/usages, module resolution + import validation, function signatures + return types, AST analysis, pattern matching
- **Max depth**: L5

### TypeScript — TS Compiler + Worker (L5)

- **Service**: HekateTypeScriptWorker (port 9202)
- **Engine**: HekateMcp.Worker.TypeScript
- **Runtime**: Node.js 24+
- **Capabilities**: Full type system via TS compiler API, find definitions/references/implementations, interface + type resolution, import graph + dependency analysis, AST analysis, JSX/TSX React component analysis
- **Max depth**: L5

### C++ — Worker (L3)

- **Service**: HekateCppWorker (port 9201)
- **Engine**: HekateMcp.Worker.Cpp
- **Capabilities**: Basic AST analysis, find definitions + usages, pattern matching, include graph
- **Limitations**: No full type resolution (no Clang integration), L4/L5 requires manual verification
- **Max depth**: L3

## How Planning Uses This

`suggest_target_level()` in `plan_levels.py` checks:
- `has_roslyn=True` → C# project → target L5 for simple/medium
- `has_jedi=True` → Python project → target L5 for simple/medium
- `has_ts_compiler=True` → TypeScript project → target L5 for simple/medium
- Complex tasks → L3 regardless (LLM needs room to explore)
- Research/docs → L2 (no code to analyze)

The pipeline detects language from the project's file extensions and sets the appropriate flag before calling `suggest_target_level()`.
