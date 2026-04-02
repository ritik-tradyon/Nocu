"""Gemini-based log and metrics analyzer.

Used for simpler queries (error analysis, performance summaries) that don't
require deep code-level investigation.
"""

import json
from google import genai
from typing import Optional


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

You have both the production error data AND the full function call graph for this service.
The code context includes route-to-function call chains, which functions call external services
(HTTP, database, AWS, Redis, Celery), and the reverse call graph (what calls what).

Analyze these errors and provide:

1. ERROR SUMMARY: Top errors by frequency and severity

2. CALL CHAIN TRACE: For the top errors, trace backwards through the call chain.
   Which route handler triggers the error? What's the call path from endpoint to failure?
   Reference specific function names and files.

3. EXTERNAL CALL FAILURES: Are errors coming from external calls (DB queries, HTTP to
   other services, AWS/Redis operations)? Cross-reference error messages with the
   external call markers in the code context.

4. DEPLOYMENT CORRELATION: Did any recent deployment introduce these errors?

5. FIX PRIORITY: What to fix first, ordered by impact. Be specific — name the function,
   the file, and what change to make.

Be direct and specific. This goes to a developer via Telegram.
Use plain text formatting — no markdown headers or bold."""

PERFORMANCE_PROMPT = """You are analyzing performance data for a Python {framework} service called "{service_name}".

## Code Context (from deepmap call graph analysis)
{relevant_code}

## Performance Summary
{performance_data}

## Slowest Endpoints
{slow_endpoints}

---

You have the function call graph showing which endpoints call which internal functions,
and which of those make external calls (DB, HTTP, AWS, Redis).

Analyze performance and provide:

1. HEALTH ASSESSMENT: Overall service health based on response times and error rates

2. BOTTLENECK TRACE: For the slowest endpoints, trace the call chain. Where is time
   being spent? Which external calls (DB queries, HTTP calls to other services) are
   likely causing latency? Reference specific functions.

3. OPTIMIZATION TARGETS: Which functions or external calls to optimize first.
   Be specific — name the function, its file, and the likely cause.

Keep it concise for Telegram. Plain text formatting.
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
            return f"Analysis failed: {e}"

    def analyze_custom(self, prompt: str) -> str:
        """Run a custom analysis prompt."""
        try:
            response = self.client.models.generate_content(model=self.model_name, contents=prompt)
            return response.text.strip()
        except Exception as e:
            return f"Analysis failed: {e}"
