# tools/sidecar_generator.py
import json
import os
import sys

SIDECAR_DIR = ".sidecars"
MANIFEST_FILE = "l3_manifest.json"

def get_sidecar_path(file_path: str, object_path: str) -> str:
    """
    Determines the path for a sidecar file based on the source file
    and object path.
    """
    # The object_path can be Class.method or just function.
    # We'll use it to create a unique filename.
    sidecar_filename = f"{object_path.replace('.', '_')}.json"
    return os.path.join(SIDECAR_DIR, file_path, sidecar_filename)

def find_next_missing_sidecar(manifest: dict):
    """
    Iterates through the manifest to find the first function or method
    that does not have a corresponding sidecar file.
    """
    for file_path, file_data in manifest.get("files", {}).items():
        # Process standalone functions
        for func_name, func_data in file_data.get("functions", {}).items():
            sidecar_path = get_sidecar_path(file_path, func_name)
            if not os.path.exists(sidecar_path):
                return file_path, func_name, func_data

        # Process methods within classes
        for class_name, class_data in file_data.get("classes", {}).items():
            for method_name, method_data in class_data.get("methods", {}).items():
                object_path = f"{class_name}.{method_name}"
                sidecar_path = get_sidecar_path(file_path, object_path)
                if not os.path.exists(sidecar_path):
                    return file_path, object_path, method_data
    
    return None, None, None

def get_source_segment(file_path: str, start: int, end: int) -> str:
    """
    Extracts a segment of source code from a file.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    # line numbers are 1-based, list indices are 0-based
    return "".join(lines[start-1:end])

if __name__ == "__main__":
    if not os.path.exists(MANIFEST_FILE):
        print(f"Error: Manifest file '{MANIFEST_FILE}' not found.", file=sys.stderr)
        print("Please run the reflection_scanner.py script first.", file=sys.stderr)
        sys.exit(1)

    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)

    file_path, object_path, data = find_next_missing_sidecar(manifest_data)

    if not file_path:
        print("All sidecars are up to date.")
        sys.exit(0)

    print("--- Next sidecar to generate ---")
    print(f"File Path: {file_path}")
    print(f"Object Path: {object_path}")
    print(f"Signature: {data['signature']}")
    
    source_code = get_source_segment(file_path, data['start_line'], data['end_line'])
    
    print("\n--- Source Code ---")
    print(source_code)
