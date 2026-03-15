# tools/reflection_scanner.py
import ast
import json
import os
from datetime import datetime, timezone
from radon.visitors import ComplexityVisitor

def get_signature(node):
    """Generates a function or method signature from an AST node."""
    if isinstance(node, ast.AsyncFunctionDef):
        prefix = "async def"
    else:
        prefix = "def"

    name = node.name
    args = []
    for arg in node.args.args:
        arg_str = arg.arg
        if arg.annotation:
            arg_str += f": {ast.unparse(arg.annotation).strip()}"
        args.append(arg_str)
    
    signature = f"{prefix} {name}({', '.join(args)})"
    if node.returns:
        signature += f" -> {ast.unparse(node.returns).strip()}"
    signature += ":"
    
    return signature

class CodeVisitor(ast.NodeVisitor):
    """
    Visits an AST tree to extract information about classes, methods,
    and functions.
    """
    def __init__(self, source_code):
        self.source_lines = source_code.splitlines()
        self.classes = {}
        self.functions = {}

    def _get_complexity(self, node):
        """Calculate cyclomatic complexity for a node."""
        source_segment = ast.get_source_segment(
            "".join(line + '\n' for line in self.source_lines), node
        )
        if source_segment:
            try:
                visitor = ComplexityVisitor.from_code(source_segment)
                return visitor.functions[0].complexity
            except Exception:
                return -1 # Indicate an error
        return -1

    def visit_FunctionDef(self, node):
        self.functions[node.name] = {
            "name": node.name,
            "signature": get_signature(node),
            "start_line": node.lineno,
            "end_line": node.end_lineno,
            "complexity": {
                "cyclomatic_complexity": self._get_complexity(node)
            }
        }
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.functions[node.name] = {
            "name": node.name,
            "signature": get_signature(node),
            "start_line": node.lineno,
            "end_line": node.end_lineno,
            "complexity": {
                "cyclomatic_complexity": self._get_complexity(node)
            }
        }
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        methods = {}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods[item.name] = {
                    "name": item.name,
                    "signature": get_signature(item),
                    "start_line": item.lineno,
                    "end_line": item.end_lineno,
                    "complexity": {
                        "cyclomatic_complexity": self._get_complexity(item)
                    }
                }
        
        self.classes[node.name] = {
            "name": node.name,
            "methods": methods,
        }



def scan_directory(directory="backend"):
    """
    Scans a directory for Python files and generates a codebase manifest.
    """
    manifest = {"files": {}}
    
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".py"):
                file_path = os.path.join(root, file).replace('\\', '/')
                
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        source = f.read()
                        tree = ast.parse(source, filename=file_path)
                        
                        visitor = CodeVisitor(source)
                        visitor.visit(tree)
                        
                        manifest["files"][file_path] = {
                            "classes": visitor.classes,
                            "functions": visitor.functions,
                        }
                except Exception as e:
                    print(f"Error parsing {file_path}: {e}")

    manifest["timestamp"] = datetime.now(timezone.utc).isoformat()
    return manifest

if __name__ == "__main__":
    output_file = "l3_manifest.json"
    print("Starting reflection scan...")
    codebase_manifest = scan_directory()
    
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(codebase_manifest, f, indent=2)
        
    print(f"Scan complete. Manifest saved to {output_file}")
