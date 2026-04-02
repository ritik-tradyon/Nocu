"""AST-based Python code scanner.

Scans a Python repository and builds an index of:
- All functions/methods with their call graphs
- Flask/FastAPI/Django route endpoints
- Inter-service HTTP calls (requests, httpx, aiohttp)
- Class hierarchies
- Import relationships
"""

import ast
import os
import sys
import json
import argparse
from pathlib import Path
from typing import Optional
from fnmatch import fnmatch

from indexer.models import (
    FunctionInfo, ClassInfo, FileIndex, ServiceIndex
)


# Decorators that indicate HTTP endpoints
ROUTE_DECORATORS = {
    # FastAPI
    "app.get", "app.post", "app.put", "app.delete", "app.patch",
    "router.get", "router.post", "router.put", "router.delete", "router.patch",
    "api_router.get", "api_router.post", "api_router.put", "api_router.delete",
    # Flask
    "app.route", "blueprint.route", "bp.route",
    # Django REST Framework (class-based detected differently)
}

# HTTP methods from decorators
DECORATOR_TO_METHOD = {
    "get": "GET", "post": "POST", "put": "PUT",
    "delete": "DELETE", "patch": "PATCH", "route": "ANY",
}

# Libraries that make external HTTP calls
HTTP_CALL_PATTERNS = {
    "requests.get", "requests.post", "requests.put", "requests.delete",
    "requests.patch", "requests.request",
    "httpx.get", "httpx.post", "httpx.put", "httpx.delete",
    "httpx.AsyncClient", "httpx.Client",
    "aiohttp.ClientSession",
    "urllib.request.urlopen",
}


class PythonFileAnalyzer(ast.NodeVisitor):
    """Analyze a single Python file using AST."""

    def __init__(self, filepath: str, repo_root: str):
        self.filepath = filepath
        self.rel_path = os.path.relpath(filepath, repo_root)
        self.imports: list[str] = []
        self.functions: list[FunctionInfo] = []
        self.classes: list[ClassInfo] = []
        self.external_calls: list[dict] = []
        self._current_class: Optional[str] = None

    def analyze(self) -> FileIndex:
        """Parse and analyze the file."""
        try:
            with open(self.filepath, "r", encoding="utf-8", errors="ignore") as f:
                source = f.read()
            tree = ast.parse(source, filename=self.filepath)
            self.visit(tree)
        except SyntaxError:
            pass  # skip files with syntax errors

        return FileIndex(
            filepath=self.rel_path,
            imports=self.imports,
            functions=self.functions,
            classes=self.classes,
            external_calls=self.external_calls,
        )

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.append(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        for alias in node.names:
            self.imports.append(f"{module}.{alias.name}")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._process_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._process_function(node)

    def _process_function(self, node):
        """Extract function info including decorators, calls, and route info."""
        # Get decorators
        decorators = []
        is_endpoint = False
        http_method = None
        route_path = None

        for dec in node.decorator_list:
            dec_str = self._decorator_to_string(dec)
            decorators.append(dec_str)

            # Check if this is a route decorator
            dec_lower = dec_str.lower()
            for pattern in ROUTE_DECORATORS:
                if pattern in dec_lower:
                    is_endpoint = True
                    # Extract HTTP method from decorator name
                    dec_parts = dec_lower.split(".")
                    method_part = dec_parts[-1].split("(")[0]
                    http_method = DECORATOR_TO_METHOD.get(method_part, "ANY")

                    # Extract route path from decorator args
                    if isinstance(dec, ast.Call) and dec.args:
                        if isinstance(dec.args[0], ast.Constant):
                            route_path = dec.args[0].value

        # Get function arguments
        args = []
        for arg in node.args.args:
            if arg.arg != "self":
                args.append(arg.arg)

        # Get all function calls within this function
        calls = self._extract_calls(node)

        # Check for external HTTP calls
        for call in calls:
            for pattern in HTTP_CALL_PATTERNS:
                if pattern in call:
                    self.external_calls.append({
                        "caller": node.name,
                        "call_pattern": call,
                        "line": node.lineno,
                        "filepath": self.rel_path,
                    })

        # Get docstring
        docstring = ast.get_docstring(node)

        func_info = FunctionInfo(
            name=node.name,
            filepath=self.rel_path,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            decorators=decorators,
            args=args,
            calls=calls,
            is_endpoint=is_endpoint,
            http_method=http_method,
            route_path=route_path,
            docstring=docstring[:200] if docstring else None,
        )

        if self._current_class:
            # This is a method — add to current class
            for cls in self.classes:
                if cls.name == self._current_class:
                    cls.methods.append(func_info)
                    break
        else:
            self.functions.append(func_info)

    def visit_ClassDef(self, node: ast.ClassDef):
        """Extract class info."""
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(self._attr_to_string(base))

        docstring = ast.get_docstring(node)

        cls_info = ClassInfo(
            name=node.name,
            filepath=self.rel_path,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            bases=bases,
            docstring=docstring[:200] if docstring else None,
        )
        self.classes.append(cls_info)

        # Visit methods within this class
        old_class = self._current_class
        self._current_class = node.name
        self.generic_visit(node)
        self._current_class = old_class

    def _extract_calls(self, node) -> list[str]:
        """Extract all function/method calls within a node."""
        calls = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                call_str = self._call_to_string(child)
                if call_str:
                    calls.append(call_str)
        return list(set(calls))  # deduplicate

    def _call_to_string(self, node: ast.Call) -> Optional[str]:
        """Convert a Call node to a readable string."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            return self._attr_to_string(node.func)
        return None

    def _attr_to_string(self, node: ast.Attribute) -> str:
        """Convert an Attribute node to dotted string."""
        parts = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))

    def _decorator_to_string(self, node) -> str:
        """Convert a decorator node to string."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return self._attr_to_string(node)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                base = node.func.id
            elif isinstance(node.func, ast.Attribute):
                base = self._attr_to_string(node.func)
            else:
                return "unknown_decorator"

            # Include first arg if it's a string (route path)
            args = []
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    args.append(f'"{arg.value}"')
            if args:
                return f"{base}({', '.join(args)})"
            return f"{base}(...)"
        return "unknown_decorator"


def scan_repository(
    repo_path: str,
    service_name: str,
    framework: str = "fastapi",
    include_patterns: list[str] = None,
    exclude_dirs: list[str] = None,
) -> ServiceIndex:
    """Scan an entire Python repository and build a ServiceIndex.

    Args:
        repo_path: Absolute path to the repository root.
        service_name: Name of the service (used in config mapping).
        framework: Web framework used (fastapi, flask, django).
        include_patterns: Glob patterns for files to include.
        exclude_dirs: Directory names to skip.

    Returns:
        ServiceIndex with all analyzed files, endpoints, and calls.
    """
    if include_patterns is None:
        include_patterns = ["**/*.py"]
    if exclude_dirs is None:
        exclude_dirs = [
            "__pycache__", ".git", "venv", "env", ".venv",
            "node_modules", "migrations", "tests", "test",
        ]

    repo_path = os.path.abspath(repo_path)
    index = ServiceIndex(
        service_name=service_name,
        repo_path=repo_path,
        framework=framework,
    )

    py_files = []
    for root, dirs, files in os.walk(repo_path):
        # Filter out excluded directories (modifies in-place to prevent descent)
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        for filename in files:
            filepath = os.path.join(root, filename)
            if any(fnmatch(filepath, pat) or filename.endswith(".py") for pat in include_patterns):
                py_files.append(filepath)

    print(f"[indexer] Scanning {len(py_files)} Python files in {repo_path}...")

    for filepath in py_files:
        analyzer = PythonFileAnalyzer(filepath, repo_path)
        file_index = analyzer.analyze()
        index.files.append(file_index)

        # Collect all endpoints
        for func in file_index.functions:
            if func.is_endpoint:
                index.endpoints.append(func)
        for cls in file_index.classes:
            for method in cls.methods:
                if method.is_endpoint:
                    index.endpoints.append(method)

    # Collect external/outbound calls
    for file_idx in index.files:
        index.outbound_calls.extend(file_idx.external_calls)

    print(f"[indexer] Found {len(index.endpoints)} endpoints, "
          f"{sum(len(f.functions) for f in index.files)} functions, "
          f"{len(index.outbound_calls)} external HTTP calls")

    return index


def main():
    parser = argparse.ArgumentParser(description="Index a Python repository for Nocu")
    parser.add_argument("--repo", required=True, help="Path to the repository")
    parser.add_argument("--name", required=True, help="Service name")
    parser.add_argument("--framework", default="fastapi", choices=["fastapi", "flask", "django"])
    parser.add_argument("--output", default=".nocu_index", help="Output directory for index files")
    args = parser.parse_args()

    index = scan_repository(args.repo, args.name, args.framework)
    output_path = index.save(args.output)
    print(f"[indexer] Index saved to {output_path}")

    # Print summary
    print(f"\n{'='*50}")
    print(f"Service: {index.service_name}")
    print(f"Files scanned: {len(index.files)}")
    print(f"Endpoints:")
    print(index.get_endpoints_summary() or "  (none found)")
    print(f"Outbound HTTP calls: {len(index.outbound_calls)}")
    for call in index.outbound_calls[:10]:
        print(f"  {call['caller']}() → {call['call_pattern']} [{call['filepath']}:{call['line']}]")


if __name__ == "__main__":
    main()
