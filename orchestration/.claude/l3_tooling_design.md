# L3 Tooling Design Specification

This document outlines the design for the reflection scanner and sidecar file format required to implement the L3 Execution Plan.

## 1. Reflection Scanner Output

The reflection scanner will traverse the `backend` directory and generate a single JSON manifest file named `l3_manifest.json`. This file will serve as a map of the entire codebase.

The structure will be as follows:

```json
{
  "files": {
    "backend/services/some_service.py": {
      "classes": {
        "SomeService": {
          "name": "SomeService",
          "methods": {
            "__init__": {
              "name": "__init__",
              "signature": "def __init__(self, db: Session):",
              "start_line": 10,
              "end_line": 12
            },
            "do_something": {
              "name": "do_something",
              "signature": "def do_something(self, user_id: int) -> dict:",
              "start_line": 14,
              "end_line": 25
            }
          }
        }
      },
      "functions": {
        "helper_function": {
          "name": "helper_function",
          "signature": "def helper_function(data: list) -> None:",
          "start_line": 28,
          "end_line": 35
        }
      }
    }
  },
  "timestamp": "2026-03-05T12:00:00Z"
}
```

This structure allows for quick lookups of file contents, class methods, and standalone functions, including their precise location and signature. It will be generated using Python's `ast` module to ensure safety and accuracy without code execution.

## 2. Sidecar File Format

Sidecar files will be created for each function and method that requires semantic understanding. They will be stored in a `.sidecars` directory, mirroring the source code's structure (e.g., the sidecar for a function in `backend/services/some_service.py` would be in `.sidecars/backend/services/some_service.py/function_name.json`).

Each sidecar will be a JSON file with the following schema:

```json
{
  "guid": "a-unique-uuid-v4-for-the-code-chunk",
  "filePath": "backend/services/some_service.py",
  "objectPath": "SomeService.do_something",
  "signature": "def do_something(self, user_id: int) -> dict:",
  "summary": "A natural language summary of what the method does, its inputs, outputs, and side effects.",
  "complexity": {
    "lineCount": 12,
    "cyclomaticComplexity": 3
  },
  "dependencies": [
    {
      "type": "internal_call",
      "target": "SomeService.helper_function"
    },
    {
      "type": "external_import",
      "target": "sqlalchemy.orm.Session"
    }
  ],
  "version": "1.0",
  "lastUpdated": "2026-03-05T12:00:00Z"
}
```

### Field Definitions:

*   **guid**: A unique identifier for this specific version of the code chunk. A new GUID will be generated if the function's body changes.
*   **filePath**: The path to the source file.
*   **objectPath**: The qualified name of the object within the file (e.g., `ClassName.methodName` or `functionName`).
*   **signature**: The function/method signature from the reflection manifest.
*   **summary**: An LLM-generated behavioral summary.
*   **complexity**: Metrics about the code chunk. `cyclomaticComplexity` can be calculated using a library like `radon`.
*   **dependencies**: A list of other code or libraries this chunk depends on. This is crucial for building context for the orchestrator.

This design provides a solid foundation for the L3 tooling. The next step will be to implement the reflection scanner based on this specification.
