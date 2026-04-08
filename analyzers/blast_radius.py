"""Blast Radius Analyzer.

Given a function or file target, traverses deepmap's reverse call graph to find
all route handlers that depend on it, then enriches with live New Relic traffic
data to rank routes by risk.
"""

import os
import re
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("nocu.blast_radius")


@dataclass
class AffectedRoute:
    method: str           # GET, POST, etc.
    path: str             # /api/auth/login
    handler: str          # login
    weekly_requests: int = 0
    risk_level: str = "UNKNOWN"  # HIGH / MEDIUM / LOW / UNKNOWN


@dataclass
class BlastRadiusResult:
    target: str
    service_name: str
    matched_functions: list[str]
    upstream_chain: list[str]
    affected_routes: list[AffectedRoute]
    not_found: bool = False
    error: Optional[str] = None


class BlastRadiusAnalyzer:
    """Analyze the blast radius of a code change using deepmap + New Relic."""

    def __init__(self, newrelic_fetcher, settings: dict):
        self.nr = newrelic_fetcher
        self.settings = settings

    def analyze(self, service_name: str, target: str) -> BlastRadiusResult:
        """Main entry point. Returns a ranked list of affected routes."""
        content = self._load_deepmap_content(service_name)
        if not content:
            return BlastRadiusResult(
                target=target,
                service_name=service_name,
                matched_functions=[],
                upstream_chain=[],
                affected_routes=[],
                not_found=True,
                error=(
                    f"No deepmap found for '{service_name}'. "
                    f"Run deepmap on this service first:\n"
                    f"  python3 ~/Downloads/deepmapbootstrap/deepmap.py update "
                    f"--repo ~/PycharmProjects/{service_name}"
                ),
            )

        graph_data = self._parse_deepmap_graph(content, target)
        if graph_data["not_found"]:
            return BlastRadiusResult(
                target=target,
                service_name=service_name,
                matched_functions=[],
                upstream_chain=[],
                affected_routes=[],
                not_found=True,
            )

        traffic_data = self._fetch_route_traffic(service_name, graph_data["affected_routes"])
        return self._build_result(target, service_name, graph_data, traffic_data)

    # ─────────────────────────────────────────────────────────
    # Step 1: Load deepmap content
    # ─────────────────────────────────────────────────────────

    def _load_deepmap_content(self, service_name: str) -> Optional[str]:
        """Find and read FUNCTION-MAP.md for the given service."""
        cc = self.settings.get("code_context", {})
        output_dir = cc.get("deepmap", {}).get("output_dir", "")

        candidates = [
            os.path.join(output_dir, service_name, "FUNCTION-MAP.md"),
            os.path.join(output_dir, service_name, "00-FUNCTION-MAP.md"),
            os.path.join(output_dir, f"{service_name}-deep", "00-FUNCTION-MAP.md"),
            os.path.join(output_dir, f"{service_name}-deep", "FUNCTION-MAP.md"),
        ]

        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        content = f.read()
                    logger.info("blast_radius: loaded deepmap path=%s chars=%d", path, len(content))
                    return content
                except Exception as e:
                    logger.error("blast_radius: failed to read path=%s error=%s", path, e)

        logger.warning(
            "blast_radius: no deepmap found service=%s tried=%s",
            service_name, candidates,
        )
        return None

    # ─────────────────────────────────────────────────────────
    # Step 2: Parse deepmap graph
    # ─────────────────────────────────────────────────────────

    def _parse_deepmap_graph(self, content: str, target: str) -> dict:
        """Parse FUNCTION-MAP.md and do a BFS upstream traversal from target.

        The deepmap All Functions format:
          ### module_name (file/path.py)
          - async func_name(params) [ROUTE: POST /path] [file.py:line]
            → calls: callee1, callee2
            ← called by: caller1, caller2

        The Call Graph format:
          - caller → callee1, callee2

        Returns:
            {
                "not_found": bool,
                "matched_functions": [...],
                "upstream_chain": [...],
                "affected_routes": [{"method": ..., "path": ..., "handler": ...}]
            }
        """
        sections = self._split_sections(content)

        # Build function metadata from "All Functions" section
        # func_meta[name] = {"called_by": set(), "is_route": bool, "method": "", "path": ""}
        func_meta = defaultdict(lambda: {
            "called_by": set(),
            "is_route": False,
            "method": "",
            "path": "",
            "file": "",
        })

        self._parse_all_functions(sections.get("all_functions", ""), func_meta)
        self._parse_call_graph(sections.get("call_graph", ""), func_meta)

        # Find functions matching the target
        matched = self._find_matching_functions(
            target, func_meta, sections.get("all_functions", "")
        )

        if not matched:
            logger.info("blast_radius: no match for target=%r", target)
            return {
                "not_found": True,
                "matched_functions": [],
                "upstream_chain": [],
                "affected_routes": [],
            }

        logger.info("blast_radius: matched target=%r functions=%s", target, matched)

        # BFS upstream: follow called_by links until we reach route handlers
        upstream_chain, affected_routes = self._bfs_upstream(matched, func_meta)

        return {
            "not_found": False,
            "matched_functions": matched,
            "upstream_chain": upstream_chain,
            "affected_routes": affected_routes,
        }

    def _split_sections(self, content: str) -> dict:
        """Split FUNCTION-MAP.md into its major sections."""
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
            elif current_key:
                current_lines.append(line)

        if current_key:
            sections[current_key] = "\n".join(current_lines)

        return sections

    def _parse_all_functions(self, text: str, func_meta: dict) -> None:
        """Parse the All Functions section and populate func_meta in-place."""
        current_func = None
        current_module_file = ""

        for line in text.split("\n"):
            # Module header: ### module_name (file/path.py)
            if line.startswith("### "):
                file_match = re.search(r'\(([^)]+\.py)\)', line)
                current_module_file = file_match.group(1) if file_match else ""
                current_func = None
                continue

            stripped = line.strip()

            # Function definition line: starts with "- "
            if stripped.startswith("- "):
                body = stripped[2:]
                # Extract function name (before first `(`)
                name_match = re.match(r'(?:async\s+)?(\w+)\s*\(', body)
                if name_match:
                    current_func = name_match.group(1)
                    func_meta[current_func]["file"] = current_module_file

                    # Check for ROUTE annotation
                    route_match = re.search(
                        r'\[ROUTE:\s*(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|WEBSOCKET)\s+([^\]]+)\]',
                        body,
                    )
                    if route_match:
                        func_meta[current_func]["is_route"] = True
                        func_meta[current_func]["method"] = route_match.group(1)
                        func_meta[current_func]["path"] = route_match.group(2).strip()

            # called_by line (indented, directly after function definition)
            elif "← called by:" in line and current_func:
                cb_part = line.split("← called by:")[1].strip()
                callers = [c.strip() for c in cb_part.split(",") if c.strip()]
                func_meta[current_func]["called_by"].update(callers)

    def _parse_call_graph(self, text: str, func_meta: dict) -> None:
        """Parse the Call Graph section and augment called_by in func_meta."""
        for line in text.split("\n"):
            if "→" not in line:
                continue
            stripped = line.strip().lstrip("- ")
            parts = stripped.split("→", 1)
            if len(parts) != 2:
                continue
            caller = parts[0].strip()
            callees = [c.strip() for c in parts[1].split(",") if c.strip()]
            for callee in callees:
                func_meta[callee]["called_by"].add(caller)

    def _find_matching_functions(
        self, target: str, func_meta: dict, all_functions_text: str
    ) -> list[str]:
        """Find all functions matching the target string.

        If target contains '/' or ends with '.py', match against file paths.
        Otherwise, do a substring match on function names.
        """
        target_lower = target.lower()
        matched = []

        is_file_target = "/" in target or target.endswith(".py")

        if is_file_target:
            # Re-parse to associate functions with their file paths
            current_module_file = ""
            for line in all_functions_text.split("\n"):
                if line.startswith("### "):
                    file_match = re.search(r'\(([^)]+\.py)\)', line)
                    current_module_file = file_match.group(1) if file_match else ""
                elif line.strip().startswith("- ") and target_lower in current_module_file.lower():
                    name_match = re.match(r'\s*-\s+(?:async\s+)?(\w+)\s*\(', line)
                    if name_match:
                        fn = name_match.group(1)
                        if fn not in matched:
                            matched.append(fn)
        else:
            for fname in func_meta:
                if target_lower in fname.lower():
                    matched.append(fname)

        return matched

    def _bfs_upstream(
        self, start_functions: list[str], func_meta: dict
    ) -> tuple[list[str], list[dict]]:
        """BFS upstream through called_by links until route handlers are found.

        Returns:
            (upstream_chain, affected_routes)
        """
        visited = set(start_functions)
        queue = deque(start_functions)
        upstream_chain = list(start_functions)
        affected_routes = []

        while queue:
            fn = queue.popleft()
            meta = func_meta.get(fn, {})

            if meta.get("is_route"):
                affected_routes.append({
                    "method": meta["method"],
                    "path": meta["path"],
                    "handler": fn,
                })
                # Don't traverse further up — route handlers are the boundary
                continue

            for caller in meta.get("called_by", set()):
                if caller not in visited:
                    visited.add(caller)
                    queue.append(caller)
                    upstream_chain.append(caller)

        return upstream_chain, affected_routes

    # ─────────────────────────────────────────────────────────
    # Step 3: Fetch New Relic traffic
    # ─────────────────────────────────────────────────────────

    def _fetch_route_traffic(self, service_name: str, routes: list[dict]) -> dict:
        """Fetch 7-day request counts per route path from New Relic.

        Returns: {"/api/path": count} — only paths found in NR data.
                 Paths absent from the dict should be treated as UNKNOWN.
        """
        if not routes:
            return {}

        service_cfg = self.settings.get("services", {}).get(service_name, {})
        app_name = service_cfg.get("newrelic_app_name", "")
        if not app_name:
            logger.warning("blast_radius: no newrelic_app_name for service=%s", service_name)
            return {}

        window_days = (
            self.settings.get("features", {})
            .get("blast_radius", {})
            .get("traffic_window_days", 7)
        )

        nrql = (
            f"SELECT count(*) FROM Transaction "
            f"WHERE appName = '{app_name}' AND transactionType = 'Web' "
            f"FACET name SINCE {window_days} days ago LIMIT 500"
        )
        logger.debug("blast_radius: NRQL=%r", nrql)

        result = self.nr.execute_nrql(nrql)
        if result.error:
            logger.error("blast_radius: NR query failed error=%s", result.error)
            return {}

        logger.debug("blast_radius: NR returned %d transaction rows", len(result.results))

        # Map NR transaction names → request counts
        # NR names FastAPI routes as "WebTransaction/FastAPI/handler_name"
        # or "WebTransaction/Uri//path/segment"
        nr_rows = {}
        for row in result.results:
            name = row.get("name", "")
            count = row.get("count", 0)
            nr_rows[name.lower()] = (name, int(count))

        # Match routes to NR transaction names
        traffic = {}
        for route in routes:
            handler = route["handler"].lower()
            path = route["path"].lower().rstrip("/")
            matched_count = None

            for nr_name_lower, (nr_name, count) in nr_rows.items():
                # Primary: match by handler function name (most reliable)
                # NR FastAPI: "WebTransaction/FastAPI/handler_name"
                if handler in nr_name_lower:
                    matched_count = count
                    logger.debug(
                        "blast_radius: matched handler=%s to NR=%s count=%d",
                        handler, nr_name, count,
                    )
                    break
                # Fallback: match by path segments
                if path and path in nr_name_lower:
                    matched_count = count
                    logger.debug(
                        "blast_radius: matched path=%s to NR=%s count=%d",
                        path, nr_name, count,
                    )
                    break

            if matched_count is not None:
                traffic[route["path"]] = matched_count

        return traffic

    # ─────────────────────────────────────────────────────────
    # Step 4: Build result
    # ─────────────────────────────────────────────────────────

    def _build_result(
        self,
        target: str,
        service_name: str,
        graph_data: dict,
        traffic_data: dict,
    ) -> BlastRadiusResult:
        """Merge graph + traffic data into a ranked BlastRadiusResult."""
        blast_cfg = self.settings.get("features", {}).get("blast_radius", {})
        thresholds = blast_cfg.get("thresholds", {})
        high_threshold = thresholds.get("high", 1000)
        medium_threshold = thresholds.get("medium", 100)

        affected_routes = []
        for route in graph_data["affected_routes"]:
            path = route["path"]
            if path in traffic_data:
                weekly = traffic_data[path]
                if weekly > high_threshold:
                    risk = "HIGH"
                elif weekly > medium_threshold:
                    risk = "MEDIUM"
                else:
                    risk = "LOW"
            else:
                weekly = 0
                risk = "UNKNOWN"

            affected_routes.append(AffectedRoute(
                method=route["method"],
                path=path,
                handler=route["handler"],
                weekly_requests=weekly,
                risk_level=risk,
            ))

        # Sort by weekly requests descending (UNKNOWN/0 goes last)
        affected_routes.sort(
            key=lambda r: (0 if r.risk_level == "UNKNOWN" else 1, r.weekly_requests),
            reverse=True,
        )

        return BlastRadiusResult(
            target=target,
            service_name=service_name,
            matched_functions=graph_data["matched_functions"],
            upstream_chain=graph_data["upstream_chain"],
            affected_routes=affected_routes,
        )
