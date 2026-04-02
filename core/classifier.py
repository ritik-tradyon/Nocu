"""Query classifier using Gemini Flash.

Takes a natural language question and outputs:
- Query type (error_analysis, memory_spike, performance, latency)
- Service name
- Time range
- Key search terms for code relevance matching
- Suggested NRQL query approach
"""

import json
from google import genai
from dataclasses import dataclass
from typing import Optional


CLASSIFICATION_PROMPT = """You are a query classifier for a production observability tool.
Given a user's natural language question about their production services, extract structured information.

Available services: {services_list}

{conversation_section}

The user's question may be a follow-up referencing previous messages. Use the conversation
history to resolve ambiguous references like "what about hermes?", "drill deeper",
"same thing for odin", "and the 502s?". If the user references a service or error type
from a previous message, carry that context forward.

Respond ONLY with a JSON object (no markdown, no backticks), with these fields:
{{
    "query_type": one of ["error_analysis", "memory_spike", "performance", "latency", "general"],
    "service_name": the service name mentioned or inferred from conversation (must match one from the available list, or "unknown"),
    "time_range": extracted time range as NRQL-compatible string (e.g. "24 hours ago", "1 hour ago", "7 days ago"). Default to "24 hours ago" if not specified.,
    "severity": one of ["low", "medium", "high"] based on urgency of the question,
    "search_terms": list of 3-5 keywords to search for in the codebase (function names, error types, modules),
    "needs_deep_analysis": true if this requires code-level investigation (memory leaks, complex RCA), false for simple log summaries,
    "summary": one-line summary of what the user is asking (resolve any follow-up references)
}}

User question: {question}"""


@dataclass
class ClassifiedQuery:
    """Result of query classification."""
    query_type: str
    service_name: str
    time_range: str
    severity: str
    search_terms: list[str]
    needs_deep_analysis: bool
    summary: str
    raw_question: str

    @property
    def is_valid(self) -> bool:
        return self.service_name != "unknown" and self.query_type != "general"


class QueryClassifier:
    """Classify user queries using Gemini Flash."""

    def __init__(self, api_key: str, model_name: str = "gemini-2.0-flash"):
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name
        self.available_services: list[str] = []

    def set_available_services(self, services: list[str]):
        """Set the list of known service names for matching."""
        self.available_services = services

    def classify(
        self, question: str, conversation_context: str = ""
    ) -> ClassifiedQuery:
        """Classify a natural language question.

        Args:
            question: The user's current question.
            conversation_context: Formatted recent conversation history.
        """
        conversation_section = ""
        if conversation_context:
            conversation_section = (
                f"Recent conversation with this user:\n{conversation_context}\n"
            )

        prompt = CLASSIFICATION_PROMPT.format(
            services_list=", ".join(self.available_services) if self.available_services else "(not configured)",
            conversation_section=conversation_section,
            question=question,
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name, contents=prompt
            )
            text = response.text.strip()

            # Clean up common LLM response artifacts
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
            text = text.strip()

            data = json.loads(text)

            return ClassifiedQuery(
                query_type=data.get("query_type", "general"),
                service_name=data.get("service_name", "unknown"),
                time_range=data.get("time_range", "24 hours ago"),
                severity=data.get("severity", "medium"),
                search_terms=data.get("search_terms", []),
                needs_deep_analysis=data.get("needs_deep_analysis", False),
                summary=data.get("summary", question),
                raw_question=question,
            )

        except (json.JSONDecodeError, Exception) as e:
            # Fallback: try to extract service name manually
            service = "unknown"
            for svc in self.available_services:
                if svc.lower() in question.lower():
                    service = svc
                    break

            return ClassifiedQuery(
                query_type="general",
                service_name=service,
                time_range="24 hours ago",
                severity="medium",
                search_terms=[],
                needs_deep_analysis=False,
                summary=question,
                raw_question=question,
            )
