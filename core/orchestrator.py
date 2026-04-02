"""Nocu Orchestrator — the central pipeline.

Receives a question, coordinates classification, data fetching,
code loading, analysis, and formatting.
"""

import os
import json
import time
import yaml
from pathlib import Path
from typing import Optional, Callable

from core.classifier import QueryClassifier, ClassifiedQuery
from core.context_loader import CodeContextLoader
from core.formatter import format_response, format_error_message
from fetchers.newrelic import NewRelicFetcher
from analyzers.gemini import GeminiAnalyzer
from analyzers.claude import ClaudeAnalyzer


class NocuOrchestrator:
    """Main orchestration pipeline for Nocu."""

    def __init__(self, config_path: str = "config/settings.yaml"):
        self.config = self._load_config(config_path)
        self._init_components()

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from YAML."""
        with open(config_path) as f:
            return yaml.safe_load(f)

    def _init_components(self):
        """Initialize all pipeline components."""
        # Gemini classifier (lightweight, fast)
        self.classifier = QueryClassifier(
            api_key=self.config["gemini"]["api_key"],
            model_name=self.config["gemini"]["classifier_model"],
        )
        self.classifier.set_available_services(
            list(self.config.get("services", {}).keys())
        )

        # New Relic fetcher
        nr_config = self.config["newrelic"]
        self.fetcher = NewRelicFetcher(
            api_key=nr_config["api_key"],
            account_id=nr_config["account_id"],
            region=nr_config.get("region", "US"),
        )

        # Gemini analyzer (for simple queries)
        self.gemini_analyzer = GeminiAnalyzer(
            api_key=self.config["gemini"]["api_key"],
            model_name=self.config["gemini"]["analyzer_model"],
        )

        # Claude Code analyzer (for deep RCA)
        claude_config = self.config.get("claude", {})
        self.claude_analyzer = ClaudeAnalyzer(
            timeout_seconds=claude_config.get("timeout_seconds", 120),
            enabled=claude_config.get("enabled", True),
            cli_path=claude_config.get("cli_path"),
        )
        self.deep_analysis_types = set(
            claude_config.get("deep_analysis_types", [])
        )

        # Code context loader (deepmap → servicemap → scanner fallback)
        self.context_loader = CodeContextLoader(self.config)
        sources = []
        cc = self.config.get("code_context", {})
        if cc.get("deepmap", {}).get("enabled"):
            sources.append("deepmap")
        if cc.get("servicemap", {}).get("enabled"):
            sources.append("servicemap")
        if cc.get("scanner", {}).get("enabled"):
            sources.append("scanner (fallback)")
        print(f"[nocu] Code context sources: {', '.join(sources) or 'none configured'}")

    async def process_question(
        self,
        question: str,
        status_callback: Optional[Callable] = None,
    ) -> list[str]:
        """Process a natural language question through the full pipeline.

        Args:
            question: The user's question.
            status_callback: Optional async function to send status updates.

        Returns:
            List of formatted response messages (may be split for Telegram).
        """
        start_time = time.time()

        # ── Step 1: Classify the query ──
        if status_callback:
            await status_callback("🧠 Understanding your question...")

        classified = self.classifier.classify(question)

        if not classified.is_valid:
            return [format_error_message(
                f"I couldn't understand which service you're asking about.\n\n"
                f"Available services: {', '.join(self.config.get('services', {}).keys())}\n\n"
                f"Try: 'What errors happened in <service_name> in the last 24 hours?'"
            )]

        service_config = self.config.get("services", {}).get(classified.service_name)
        if not service_config:
            return [format_error_message(
                f"Service '{classified.service_name}' is not configured.\n"
                f"Available: {', '.join(self.config.get('services', {}).keys())}"
            )]

        app_name = service_config["newrelic_app_name"]
        framework = service_config.get("framework", "fastapi")

        # ── Step 2: Fetch observability data ──
        if status_callback:
            await status_callback(f"📡 Fetching data from New Relic for {classified.service_name}...")

        observability_data = self._fetch_data(classified, app_name)

        # ── Step 3: Load relevant code context ──
        if status_callback:
            await status_callback("📂 Loading relevant code...")

        code_context = self._load_code_context(classified)

        # ── Step 4: Analyze ──
        analyzer_used = self._pick_analyzer(classified)

        if status_callback:
            await status_callback(f"🔍 Analyzing with {analyzer_used}...")

        analysis = self._run_analysis(
            classified=classified,
            observability_data=observability_data,
            code_context=code_context,
            service_config=service_config,
            framework=framework,
            analyzer_used=analyzer_used,
        )

        # ── Step 5: Format response ──
        elapsed = time.time() - start_time
        analysis += f"\n\n⏱ Analysis completed in {elapsed:.1f}s"

        return format_response(
            analysis=analysis,
            query_type=classified.query_type,
            service_name=classified.service_name,
            time_range=classified.time_range,
            analyzer_used=analyzer_used,
        )

    def _fetch_data(self, classified: ClassifiedQuery, app_name: str) -> dict:
        """Fetch relevant data from New Relic based on query type."""
        data = {}

        if classified.query_type == "error_analysis":
            data["error_logs"] = self.fetcher.get_error_logs(
                app_name, since=classified.time_range
            )
            data["error_counts"] = self.fetcher.get_error_counts_by_type(
                app_name, since=classified.time_range
            )
            data["transaction_errors"] = self.fetcher.get_transaction_errors(
                app_name, since=classified.time_range
            )
            data["deployments"] = self.fetcher.get_recent_deployments(app_name)

        elif classified.query_type == "memory_spike":
            data["memory"] = self.fetcher.get_memory_usage(
                app_name, since=classified.time_range
            )
            data["error_logs"] = self.fetcher.get_error_logs(
                app_name, since=classified.time_range
            )
            data["performance"] = self.fetcher.get_performance_summary(
                app_name, since=classified.time_range
            )
            data["deployments"] = self.fetcher.get_recent_deployments(app_name)

        elif classified.query_type == "performance":
            data["performance"] = self.fetcher.get_performance_summary(
                app_name, since=classified.time_range
            )
            data["slow_endpoints"] = self.fetcher.get_slowest_transactions(
                app_name, since=classified.time_range
            )
            data["error_counts"] = self.fetcher.get_error_counts_by_type(
                app_name, since=classified.time_range
            )

        elif classified.query_type == "latency":
            data["performance"] = self.fetcher.get_performance_summary(
                app_name, since=classified.time_range
            )
            data["slow_endpoints"] = self.fetcher.get_slowest_transactions(
                app_name, since=classified.time_range
            )
            data["error_logs"] = self.fetcher.get_error_logs(
                app_name, since=classified.time_range
            )

        else:
            # General — fetch everything
            data["performance"] = self.fetcher.get_performance_summary(
                app_name, since=classified.time_range
            )
            data["error_counts"] = self.fetcher.get_error_counts_by_type(
                app_name, since=classified.time_range
            )

        return data

    def _load_code_context(self, classified: ClassifiedQuery) -> dict:
        """Load relevant code context using the multi-source context loader.

        Priority: deepmap → servicemap → scanner → direct file search
        """
        context = self.context_loader.load_context(
            service_name=classified.service_name,
            search_terms=classified.search_terms,
        )

        if context.sources_used:
            print(f"[nocu] Code context loaded from: {', '.join(context.sources_used)}")
        else:
            print(f"[nocu] No code context available for {classified.service_name}")

        return {
            "endpoints_summary": context.endpoints_summary,
            "relevant_code": context.to_llm_context(),
            "repo_path": context.repo_path,
        }

    def _pick_analyzer(self, classified: ClassifiedQuery) -> str:
        """Decide which analyzer to use."""
        if (classified.needs_deep_analysis
                and classified.query_type in self.deep_analysis_types
                and self.claude_analyzer.is_available()):
            return "Claude Code"
        return "Gemini"

    def _run_analysis(
        self,
        classified: ClassifiedQuery,
        observability_data: dict,
        code_context: dict,
        service_config: dict,
        framework: str,
        analyzer_used: str,
    ) -> str:
        """Run the analysis with the selected analyzer."""

        if analyzer_used == "Claude Code":
            # Combine all observability data into one string
            obs_parts = []
            for key, result in observability_data.items():
                obs_parts.append(f"### {key}\n{result.to_summary()}")
            obs_text = "\n\n".join(obs_parts)

            return self.claude_analyzer.analyze(
                question=classified.raw_question,
                service_name=classified.service_name,
                framework=framework,
                observability_data=obs_text,
                endpoints_summary=code_context["endpoints_summary"],
                relevant_code=code_context["relevant_code"],
                repo_path=code_context.get("repo_path"),
            )

        else:
            # Gemini analysis — route by query type
            if classified.query_type == "error_analysis":
                return self.gemini_analyzer.analyze_errors(
                    service_name=classified.service_name,
                    framework=framework,
                    error_logs=observability_data.get("error_logs", "").to_summary()
                        if observability_data.get("error_logs") else "",
                    error_counts=observability_data.get("error_counts", "").to_summary()
                        if observability_data.get("error_counts") else "",
                    transaction_errors=observability_data.get("transaction_errors", "").to_summary()
                        if observability_data.get("transaction_errors") else "",
                    deployments=observability_data.get("deployments", "").to_summary()
                        if observability_data.get("deployments") else "",
                    relevant_code=code_context["relevant_code"],
                    time_range=classified.time_range,
                )

            elif classified.query_type in ("performance", "latency"):
                return self.gemini_analyzer.analyze_performance(
                    service_name=classified.service_name,
                    framework=framework,
                    performance_data=observability_data.get("performance", "").to_summary()
                        if observability_data.get("performance") else "",
                    slow_endpoints=observability_data.get("slow_endpoints", "").to_summary()
                        if observability_data.get("slow_endpoints") else "",
                    relevant_code=code_context["relevant_code"],
                )

            else:
                # General — combine everything into a custom prompt
                obs_parts = []
                for key, result in observability_data.items():
                    obs_parts.append(f"{key}: {result.to_summary()}")

                custom_prompt = (
                    f"Question about {classified.service_name}: {classified.raw_question}\n\n"
                    f"Data:\n{'|'.join(obs_parts)}\n\n"
                    f"Code context:\n{code_context['relevant_code']}\n\n"
                    f"Provide a concise answer. Plain text for Telegram."
                )
                return self.gemini_analyzer.analyze_custom(custom_prompt)
