"""Data models for the code index."""

from dataclasses import dataclass, field
from typing import Optional
import json
from pathlib import Path


@dataclass
class FunctionInfo:
    """A function or method in the codebase."""
    name: str
    filepath: str
    line_start: int
    line_end: int
    decorators: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)  # functions this calls
    is_endpoint: bool = False  # True if it's a Flask/FastAPI route
    http_method: Optional[str] = None  # GET, POST, etc.
    route_path: Optional[str] = None  # /api/users, etc.
    docstring: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "filepath": self.filepath,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "decorators": self.decorators,
            "args": self.args,
            "calls": self.calls,
            "is_endpoint": self.is_endpoint,
            "http_method": self.http_method,
            "route_path": self.route_path,
            "docstring": self.docstring,
        }


@dataclass
class ClassInfo:
    """A class in the codebase."""
    name: str
    filepath: str
    line_start: int
    line_end: int
    bases: list[str] = field(default_factory=list)
    methods: list[FunctionInfo] = field(default_factory=list)
    docstring: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "filepath": self.filepath,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "bases": self.bases,
            "methods": [m.to_dict() for m in self.methods],
            "docstring": self.docstring,
        }


@dataclass
class FileIndex:
    """Index of a single Python file."""
    filepath: str
    imports: list[str] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    # External service calls detected (requests.get, httpx, etc.)
    external_calls: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "filepath": self.filepath,
            "imports": self.imports,
            "functions": [f.to_dict() for f in self.functions],
            "classes": [c.to_dict() for c in self.classes],
            "external_calls": self.external_calls,
        }


@dataclass
class ServiceIndex:
    """Complete index of a service/repository."""
    service_name: str
    repo_path: str
    framework: str
    files: list[FileIndex] = field(default_factory=list)
    endpoints: list[FunctionInfo] = field(default_factory=list)  # all route handlers
    # inter-service calls: this service -> other service endpoints
    outbound_calls: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "service_name": self.service_name,
            "repo_path": self.repo_path,
            "framework": self.framework,
            "file_count": len(self.files),
            "files": [f.to_dict() for f in self.files],
            "endpoints": [e.to_dict() for e in self.endpoints],
            "outbound_calls": self.outbound_calls,
        }

    def save(self, output_dir: str):
        """Save index to JSON file."""
        path = Path(output_dir) / f"{self.service_name}.index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        return path

    @classmethod
    def load(cls, path: str) -> "ServiceIndex":
        """Load index from JSON file."""
        with open(path) as f:
            data = json.load(f)

        index = cls(
            service_name=data["service_name"],
            repo_path=data["repo_path"],
            framework=data["framework"],
        )

        for file_data in data.get("files", []):
            functions = []
            for fd in file_data.get("functions", []):
                functions.append(FunctionInfo(**fd))

            classes = []
            for cd in file_data.get("classes", []):
                methods = [FunctionInfo(**m) for m in cd.pop("methods", [])]
                classes.append(ClassInfo(**cd, methods=methods))

            index.files.append(FileIndex(
                filepath=file_data["filepath"],
                imports=file_data.get("imports", []),
                functions=functions,
                classes=classes,
                external_calls=file_data.get("external_calls", []),
            ))

        for ep in data.get("endpoints", []):
            index.endpoints.append(FunctionInfo(**ep))

        index.outbound_calls = data.get("outbound_calls", [])
        return index

    def get_relevant_code(self, error_patterns: list[str], max_files: int = 10) -> list[dict]:
        """Find code files most relevant to given error patterns.

        Returns list of {filepath, functions, reason} dicts.
        """
        relevant = []

        for file_idx in self.files:
            score = 0
            matched_functions = []
            reasons = []

            for pattern in error_patterns:
                pattern_lower = pattern.lower()

                # Check if any function names match
                for func in file_idx.functions:
                    if pattern_lower in func.name.lower():
                        score += 3
                        matched_functions.append(func)
                        reasons.append(f"function name matches '{pattern}'")

                    # Check if function calls match
                    for call in func.calls:
                        if pattern_lower in call.lower():
                            score += 1
                            matched_functions.append(func)
                            reasons.append(f"calls '{call}' related to '{pattern}'")

                # Check class names
                for cls_info in file_idx.classes:
                    if pattern_lower in cls_info.name.lower():
                        score += 2
                        reasons.append(f"class name matches '{pattern}'")

                # Check file path
                if pattern_lower in file_idx.filepath.lower():
                    score += 2
                    reasons.append(f"file path matches '{pattern}'")

                # Check imports for related modules
                for imp in file_idx.imports:
                    if pattern_lower in imp.lower():
                        score += 1
                        reasons.append(f"imports '{imp}'")

            if score > 0:
                relevant.append({
                    "filepath": file_idx.filepath,
                    "score": score,
                    "functions": list({f.name: f for f in matched_functions}.values()),
                    "reasons": list(set(reasons)),
                })

        # Sort by relevance score, return top N
        relevant.sort(key=lambda x: x["score"], reverse=True)
        return relevant[:max_files]

    def get_endpoints_summary(self) -> str:
        """Get a concise summary of all endpoints for LLM context."""
        lines = []
        for ep in self.endpoints:
            method = ep.http_method or "ANY"
            route = ep.route_path or "unknown"
            doc = f" — {ep.docstring[:80]}" if ep.docstring else ""
            lines.append(f"  {method} {route} → {ep.name}() [{ep.filepath}:{ep.line_start}]{doc}")
        return "\n".join(lines)