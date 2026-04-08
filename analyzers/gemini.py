"""Gemini-based log and metrics analyzer.

Used for simpler queries (error analysis, performance summaries) that don't
require deep code-level investigation.
"""

import json
import logging
from google import genai
from typing import Optional

logger = logging.getLogger("nocu.gemini")


ERROR_ANALYSIS_PROMPT = """You are a production observability expert analyzing error logs for a Python {framework} service called "{service_name}".

## Code Context (from deepmap call graph analysis)
{relevant_code}

## Error Logs from New Relic (last {time_range})
{error_logs}

## Error Counts by Type
{error_counts}

## Transaction Errors
{transaction_errors}

## Recent Deployments
{deployments}

---

STRICT SOURCE ATTRIBUTION RULE:
Every statement you make must be tagged with its source:
- [NR] — the claim comes directly from New Relic data above (a field value, count, error message, timestamp, etc.)
- [CODE] — the claim is inferred from the code context / call graph
- [NR+CODE] — NR data identified the symptom AND code context explains why

Never blend sources into one sentence without tagging. If you cannot back a claim with [NR] data, you must tag it [CODE] and phrase it as an inference ("the code suggests...", "likely because..."), not a fact.

Structure your response exactly as:

FROM NEW RELIC DATA:
(Only facts directly visible in the NR data above — error messages, counts, URIs, timestamps, hostnames. Tag each line [NR].)

FROM CODE ANALYSIS:
(Only inferences drawn from the call graph / code context. Tag each line [CODE]. Use "likely", "suggests", "based on code".)

COMBINED ASSESSMENT:
(Cross-references where NR symptoms map to code paths. Tag each line [NR+CODE].)

FIX PRIORITY:
(Ordered list of what to fix. Tag the evidence basis for each item.)

Plain text only — no markdown. This goes to a developer via Telegram."""

PERFORMANCE_PROMPT = """You are analyzing performance data for a Python {framework} service called "{service_name}".

## Code Context (from deepmap call graph analysis)
{relevant_code}

## Performance Summary
{performance_data}

## Slowest Endpoints
{slow_endpoints}

---

STRICT SOURCE ATTRIBUTION RULE:
Every statement must be tagged with its source:
- [NR] — directly from the New Relic metrics above (a measured number, latency value, endpoint name)
- [CODE] — inferred from the code context / call graph
- [NR+CODE] — NR identified the slow endpoint AND code explains the likely cause

Never blend sources. If you cannot point to a specific NR value, tag it [CODE] and phrase it as an inference.

Structure your response exactly as:

FROM NEW RELIC DATA:
(Measured facts — latencies, throughput, p95, error rates, slowest endpoint names. Tag each [NR].)

FROM CODE ANALYSIS:
(Why those endpoints are likely slow based on call graph — external calls, DB queries, etc. Tag each [CODE].)

COMBINED ASSESSMENT:
(NR bottleneck mapped to code path. Tag each [NR+CODE].)

OPTIMIZATION TARGETS:
(Ordered list. Tag the evidence basis for each item.)

Plain text only. No markdown. This goes to a developer via Telegram.
"""


class GeminiAnalyzer:
    """Analyze logs and metrics using Gemini."""

    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash"):
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def analyze_errors(
        self,
        service_name: str,
        framework: str,
        error_logs: str,
        error_counts: str,
        transaction_errors: str,
        deployments: str,
        relevant_code: str,
        time_range: str = "24 hours",
    ) -> str:
        """Analyze error patterns and provide RCA."""
        prompt = ERROR_ANALYSIS_PROMPT.format(
            service_name=service_name,
            framework=framework,
            error_logs=error_logs or "(no error logs found)",
            error_counts=error_counts or "(no error count data)",
            transaction_errors=transaction_errors or "(no transaction errors)",
            deployments=deployments or "(no recent deployments)",
            relevant_code=relevant_code or "(no code context available)",
            time_range=time_range,
        )

        try:
            response = self.client.models.generate_content(model=self.model_name, contents=prompt)
            return response.text.strip()
        except Exception as e:
            logger.error("Gemini analyze_errors failed service=%s error=%s", service_name, e, exc_info=True)
            return f"Analysis failed: {e}"

    def analyze_performance(
        self,
        service_name: str,
        framework: str,
        performance_data: str,
        slow_endpoints: str,
        relevant_code: str,
    ) -> str:
        """Analyze performance data."""
        prompt = PERFORMANCE_PROMPT.format(
            service_name=service_name,
            framework=framework,
            performance_data=performance_data or "(no performance data)",
            slow_endpoints=slow_endpoints or "(no slow endpoint data)",
            relevant_code=relevant_code or "(no code context available)",
        )

        try:
            response = self.client.models.generate_content(model=self.model_name, contents=prompt)
            return response.text.strip()
        except Exception as e:
            logger.error("Gemini analyze_performance failed service=%s error=%s", service_name, e, exc_info=True)
            return f"Analysis failed: {e}"

    def analyze_custom(self, prompt: str) -> str:
        """Run a custom analysis prompt."""
        try:
            response = self.client.models.generate_content(model=self.model_name, contents=prompt)
            return response.text.strip()
        except Exception as e:
            logger.error("Gemini analyze_custom failed error=%s", e, exc_info=True)
            return f"Analysis failed: {e}"
