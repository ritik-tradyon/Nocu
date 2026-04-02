"""Code context loader.

Loads code context from multiple sources in priority order:
  1. deepmap  — FUNCTION-MAP.md (dense function-level call chains)
  2. servicemap — inter-service dependency maps from Obsidian vault
  3. scanner — Nocu's built-in AST indexer (fallback)

This bridges your existing Master Repo tools with Nocu's analysis pipeline.
"""

import logging
import os
import json
import glob
from typing import Optional
from pathlib import Path
from dataclasses import dataclass

logger = logging.getLogger("nocu.context")


@dataclass
class CodeContext:
    """Code context assembled for LLM analysis."""
    service_name: str
    # Dense function-level context (from deepmap or scanner)
    function_map: str
    # Inter-service dependency context (from servicemap)
    service_dependencies: str
    # Relevant code snippets (loaded from actual files)
    relevant_code: str
    # Endpoint summary
    endpoints_summary: str
    # Repo path for Claude Code to reference
    repo_path: Optional[str]
    # Which sources were used
    sources_used: list[str]

    def to_llm_context(self, max_chars: int = 20000) -> str:
        """Assemble into a single context string for LLM consumption."""
        parts = []

        if self.endpoints_summary:
            parts.append(f"## Service Endpoints\n{self.endpoints_summary}")

        if self.service_dependencies:
            parts.append(f"## Inter-Service Dependencies\n{self.service_dependencies}")

        if self.function_map:
            parts.append(f"## Function Call Map\n{self.function_map}")

        if self.relevant_code:
            parts.append(f"## Relevant Source Code\n{self.relevant_code}")

        combined = "\n\n".join(parts)

        # Truncate if too long (preserve beginning which has structure)
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "\n\n... (truncated for context limits)"

        return combined


class CodeContextLoader:
    """Load code context from deepmap, servicemap, or scanner."""

    def __init__(self, config: dict):
        self.config = config
        self.code_context_config = config.get("code_context", {})
        self.services_config = config.get("services", {})

        # Source configs
        self.deepmap_config = self.code_context_config.get("deepmap", {})
        self.servicemap_config = self.code_context_config.get("servicemap", {})
        self.scanner_config = self.code_context_config.get("scanner", {})

        # Pre-loaded scanner indexes (if any)
        self._scanner_indexes = {}

    def load_context(
        self,
        service_name: str,
        search_terms: list[str] = None,
    ) -> CodeContext:
        """Load code context for a service, trying sources in priority order.

        Args:
            service_name: The service to load context for.
            search_terms: Keywords to find relevant code files.

        Returns:
            CodeContext with all available information assembled.
        """
        sources_used = []
        function_map = ""
        service_dependencies = ""
        relevant_code = ""
        endpoints_summary = ""

        service_config = self.services_config.get(service_name, {})
        repo_path = service_config.get("repo_path")

        # ── 1. Try deepmap (FUNCTION-MAP.md) ──
        if self.deepmap_config.get("enabled", False):
            dm_result = self._load_deepmap(service_name, search_terms or [])
            if dm_result:
                function_map = dm_result
                sources_used.append("deepmap")

        # ── 2. Try servicemap (inter-service dependencies) ──
        if self.servicemap_config.get("enabled", False):
            sm_result = self._load_servicemap(service_name)
            if sm_result:
                service_dependencies = sm_result
                sources_used.append("servicemap")

        # ── 3. Fallback to scanner if deepmap not available ──
        if not function_map and self.scanner_config.get("enabled", True):
            scanner_result = self._load_scanner(service_name, search_terms or [])
            if scanner_result:
                function_map = scanner_result.get("function_map", "")
                endpoints_summary = scanner_result.get("endpoints_summary", "")
                relevant_code = scanner_result.get("relevant_code", "")
                sources_used.append("scanner")

        # ── 4. Load relevant source files directly (for any source) ──
        if repo_path and search_terms and not relevant_code:
            relevant_code = self._find_relevant_files(
                repo_path, search_terms, service_config.get("framework", "fastapi")
            )
            if relevant_code:
                sources_used.append("direct_file_read")

        return CodeContext(
            service_name=service_name,
            function_map=function_map,
            service_dependencies=service_dependencies,
            relevant_code=relevant_code,
            endpoints_summary=endpoints_summary,
            repo_path=repo_path,
            sources_used=sources_used,
        )

    def _load_deepmap(
        self, service_name: str, search_terms: list[str] = None
    ) -> Optional[str]:
        """Load FUNCTION-MAP.md from deepmap output.

        Deepmap's 00-FUNCTION-MAP.md has these sections:
          ## Routes (N)         — route method/path → handler, call chain, externals
          ## All Functions (N)  — per-module function signatures, calls, called_by
          ## Call Graph          — adjacency list (caller → callee)

        For LLM context, we always include Routes + Call Graph (compact),
        and selectively include function details matching search_terms.
        """
        output_dir = self.deepmap_config.get("output_dir", "")
        file_pattern = self.deepmap_config.get(
            "file_pattern", "{service_name}/FUNCTION-MAP.md"
        )

        # Try multiple possible locations for the file
        candidates = [
            os.path.join(output_dir, file_pattern.format(service_name=service_name)),
            os.path.join(output_dir, service_name, "FUNCTION-MAP.md"),
            os.path.join(output_dir, service_name, "00-FUNCTION-MAP.md"),
            os.path.join(output_dir, f"{service_name}-deep", "00-FUNCTION-MAP.md"),
            os.path.join(output_dir, f"{service_name}-deep", "FUNCTION-MAP.md"),
        ]

        content = None
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        content = f.read()
                    logger.info("Loaded deepmap path=%s chars=%d", path, len(content))
                    break
                except Exception as e:
                    logger.error("Failed to read deepmap path=%s error=%s", path, e)

        if not content:
            return None

        # If no search terms or content is small, return as-is
        if not search_terms or len(content) <= 15000:
            if len(content) > 20000:
                content = content[:20000] + "\n\n... (truncated)"
            return content

        # Smart extraction: always include Routes + Call Graph,
        # and filter All Functions to relevant modules
        sections = self._split_deepmap_sections(content)
        parts = []

        # Always include routes (compact, high value)
        if "routes" in sections:
            parts.append(sections["routes"])

        # Filter "All Functions" section to relevant entries
        if "all_functions" in sections and search_terms:
            relevant = self._filter_deepmap_functions(
                sections["all_functions"], search_terms
            )
            if relevant:
                parts.append(relevant)

        # Always include call graph (compact adjacency list)
        if "call_graph" in sections:
            parts.append(sections["call_graph"])

        result = "\n\n".join(parts)
        if len(result) > 20000:
            result = result[:20000] + "\n\n... (truncated)"
        return result

    def _split_deepmap_sections(self, content: str) -> dict:
        """Split a FUNCTION-MAP.md into its major sections."""
        sections = {}
        current_key = None
        current_lines = []

        for line in content.split("\n"):
            if line.startswith("## Routes"):
                if current_key:
                    sections[current_key] = "\n".join(current_lines)
                current_key = "routes"
                current_lines = [line]
            elif line.startswith("## All Functions"):
                if current_key:
                    sections[current_key] = "\n".join(current_lines)
                current_key = "all_functions"
                current_lines = [line]
            elif line.startswith("## Call Graph"):
                if current_key:
                    sections[current_key] = "\n".join(current_lines)
                current_key = "call_graph"
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_key:
            sections[current_key] = "\n".join(current_lines)

        return sections

    def _filter_deepmap_functions(
        self, functions_section: str, search_terms: list[str]
    ) -> str:
        """Filter the All Functions section to modules matching search terms."""
        lines = functions_section.split("\n")
        result_lines = [lines[0]]  # Keep the header
        include_block = False
        terms_lower = [t.lower() for t in search_terms]

        for line in lines[1:]:
            # Module headers: ### module.path (file.py)
            if line.startswith("### "):
                # Check if this module matches any search term
                line_lower = line.lower()
                include_block = any(t in line_lower for t in terms_lower)
                if include_block:
                    result_lines.append(line)
            elif include_block:
                result_lines.append(line)
            elif line.startswith("- "):
                # Individual function line — check if relevant
                line_lower = line.lower()
                if any(t in line_lower for t in terms_lower):
                    result_lines.append(line)

        if len(result_lines) > 1:
            return "\n".join(result_lines)
        return ""

    def _load_servicemap(self, service_name: str) -> Optional[str]:
        """Load inter-service dependency data from servicemap output."""
        output_dir = self.servicemap_config.get("output_dir", "")
        dep_file = self.servicemap_config.get(
            "dependency_file", "ServiceMap/dependencies.md"
        )

        dep_path = os.path.join(output_dir, dep_file)

        if not os.path.exists(dep_path):
            # Try finding any servicemap output
            alt_paths = [
                os.path.join(output_dir, "ServiceMap", f"{service_name}.md"),
                os.path.join(output_dir, "servicemap", f"{service_name}.md"),
                os.path.join(output_dir, "ServiceMap", "../README.md"),
            ]
            for alt in alt_paths:
                if os.path.exists(alt):
                    dep_path = alt
                    break
            else:
                return None

        try:
            with open(dep_path, "r") as f:
                content = f.read()

            # If it's a global dependency file, extract the section for this service
            if service_name.lower() in content.lower():
                # Try to extract just the relevant section
                lines = content.split("\n")
                relevant_lines = []
                capturing = False

                for line in lines:
                    if service_name.lower() in line.lower() and line.startswith("#"):
                        capturing = True
                    elif capturing and line.startswith("# ") and service_name.lower() not in line.lower():
                        break  # hit the next service section

                    if capturing:
                        relevant_lines.append(line)

                if relevant_lines:
                    return "\n".join(relevant_lines)

            # Return the whole file if it's service-specific or we couldn't extract
            if len(content) > 5000:
                content = content[:5000] + "\n... (truncated)"
            return content

        except Exception as e:
            logger.error("Failed to read servicemap path=%s error=%s", dep_path, e)
            return None

    def _load_scanner(
        self, service_name: str, search_terms: list[str]
    ) -> Optional[dict]:
        """Load from Nocu's built-in scanner index."""
        index_dir = self.scanner_config.get("index_dir", ".nocu_index")
        index_path = os.path.join(index_dir, f"{service_name}.index.json")

        if not os.path.exists(index_path):
            # Try to build the index on the fly
            service_config = self.services_config.get(service_name, {})
            repo_path = service_config.get("repo_path")
            if repo_path and os.path.exists(repo_path):
                try:
                    from indexer.scanner import scan_repository
                    index = scan_repository(
                        repo_path=repo_path,
                        service_name=service_name,
                        framework=service_config.get("framework", "fastapi"),
                        exclude_dirs=self.scanner_config.get("exclude_dirs"),
                    )
                    index.save(index_dir)
                    self._scanner_indexes[service_name] = index
                except Exception as e:
                    logger.error("Failed to build scanner index service=%s error=%s", service_name, e, exc_info=True)
                    return None
            else:
                return None

        # Load from file if not cached
        if service_name not in self._scanner_indexes:
            try:
                from indexer.models import ServiceIndex
                self._scanner_indexes[service_name] = ServiceIndex.load(index_path)
            except Exception as e:
                logger.error("Failed to load scanner index service=%s path=%s error=%s", service_name, index_path, e)
                return None

        index = self._scanner_indexes[service_name]

        # Build context from scanner data
        endpoints_summary = index.get_endpoints_summary()

        # Find relevant files
        relevant = index.get_relevant_code(search_terms) if search_terms else []
        relevant_parts = []
        for rf in relevant[:5]:
            filepath = os.path.join(index.repo_path, rf["filepath"])
            if os.path.exists(filepath):
                try:
                    with open(filepath) as f:
                        content = f.read()
                    if len(content) > 3000:
                        # Extract just relevant functions
                        file_lines = content.split("\n")
                        extracted = []
                        for func in rf.get("functions", []):
                            if hasattr(func, "line_start"):
                                start = max(0, func.line_start - 2)
                                end = func.line_end + 1
                                extracted.extend(file_lines[start:end])
                                extracted.append("...")
                        content = "\n".join(extracted) if extracted else content[:3000]
                    relevant_parts.append(f"--- {rf['filepath']} ---\n{content}")
                except Exception as e:
                    logger.warning("Failed to read source file path=%s error=%s", filepath, e)

        # Build a function map from scanner data
        func_map_lines = []
        for file_idx in index.files:
            if file_idx.functions or file_idx.classes:
                func_map_lines.append(f"\n### {file_idx.filepath}")
                for func in file_idx.functions:
                    calls_str = ", ".join(func.calls[:5]) if func.calls else "none"
                    route = f" [{func.http_method} {func.route_path}]" if func.is_endpoint else ""
                    func_map_lines.append(
                        f"  {func.name}(){route} → calls: {calls_str}"
                    )
                for cls in file_idx.classes:
                    func_map_lines.append(f"  class {cls.name}({', '.join(cls.bases)})")
                    for method in cls.methods:
                        func_map_lines.append(f"    .{method.name}()")

        return {
            "function_map": "\n".join(func_map_lines),
            "endpoints_summary": endpoints_summary,
            "relevant_code": "\n\n".join(relevant_parts),
        }

    def _find_relevant_files(
        self,
        repo_path: str,
        search_terms: list[str],
        framework: str,
    ) -> str:
        """Directly search repo files for relevant code by grepping for terms."""
        relevant_parts = []
        exclude = {"__pycache__", ".git", "venv", "env", ".venv", "node_modules", "migrations"}

        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in exclude]
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, repo_path)

                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception:
                    continue

                content_lower = content.lower()
                score = sum(1 for term in search_terms if term.lower() in content_lower)

                if score > 0:
                    # Truncate long files
                    if len(content) > 2000:
                        content = content[:2000] + "\n... (truncated)"
                    relevant_parts.append((score, rel_path, content))

        # Sort by relevance, take top 5
        relevant_parts.sort(key=lambda x: x[0], reverse=True)
        return "\n\n".join(
            f"--- {path} (matches: {score}) ---\n{content}"
            for score, path, content in relevant_parts[:5]
        )
