"""New Relic data fetcher using NerdGraph (GraphQL) API.

Executes NRQL queries to fetch logs, metrics, errors, and performance data.
"""

import json
import logging
import requests
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger("nocu.newrelic")


# NerdGraph endpoints by region
NERDGRAPH_ENDPOINTS = {
    "US": "https://api.newrelic.com/graphql",
    "EU": "https://api.eu.newrelic.com/graphql",
}


@dataclass
class NRQLResult:
    """Result from a NRQL query."""
    query: str
    results: list[dict]
    metadata: Optional[dict] = None
    error: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return len(self.results) == 0

    def to_summary(self, max_rows: int = 50) -> str:
        """Format results as a readable summary for LLM consumption."""
        if self.error:
            return f"Query failed: {self.error}"
        if self.is_empty:
            return "No results found."

        lines = [f"NRQL: {self.query}", f"Results ({len(self.results)} rows):"]
        for i, row in enumerate(self.results[:max_rows]):
            line_parts = []
            for k, v in row.items():
                if k == "timestamp":
                    continue  # handled separately
                line_parts.append(f"{k}={v}")
            timestamp = row.get("timestamp", "")
            prefix = f"  [{timestamp}] " if timestamp else f"  [{i+1}] "
            lines.append(prefix + " | ".join(line_parts))

        if len(self.results) > max_rows:
            lines.append(f"  ... and {len(self.results) - max_rows} more rows")

        return "\n".join(lines)


class NewRelicFetcher:
    """Fetch observability data from New Relic via NerdGraph."""

    def __init__(self, api_key: str, account_id: str, region: str = "US"):
        self.api_key = api_key
        self.account_id = account_id
        self.endpoint = NERDGRAPH_ENDPOINTS.get(region, NERDGRAPH_ENDPOINTS["US"])
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "API-Key": self.api_key,
        })

    def execute_nrql(self, nrql: str) -> NRQLResult:
        """Execute a NRQL query via NerdGraph."""
        query = """
        {
            actor {
                account(id: %s) {
                    nrql(query: "%s") {
                        results
                        metadata {
                            facets
                            eventTypes
                            timeWindow {
                                begin
                                end
                            }
                        }
                    }
                }
            }
        }
        """ % (self.account_id, nrql.replace('"', '\\"'))

        try:
            response = self.session.post(
                self.endpoint,
                json={"query": query},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            # Check for GraphQL errors
            if "errors" in data:
                error_msg = "; ".join(e.get("message", "") for e in data["errors"])
                logger.error("NerdGraph error nrql=%r error=%s", nrql[:120], error_msg)
                return NRQLResult(query=nrql, results=[], error=error_msg)

            # Extract results
            nrql_data = data["data"]["actor"]["account"]["nrql"]
            result = NRQLResult(
                query=nrql,
                results=nrql_data.get("results", []),
                metadata=nrql_data.get("metadata"),
            )
            logger.debug("NerdGraph ok nrql=%r rows=%d", nrql[:120], len(result.results))
            return result

        except requests.RequestException as e:
            logger.error("NerdGraph request failed nrql=%r error=%s", nrql[:120], e)
            return NRQLResult(query=nrql, results=[], error=str(e))
        except (KeyError, TypeError) as e:
            logger.error("NerdGraph unexpected response nrql=%r error=%s", nrql[:120], e)
            return NRQLResult(query=nrql, results=[], error=f"Unexpected response format: {e}")

    # ──────────────────────────────────────────────
    # Pre-built query methods for common use cases
    # ──────────────────────────────────────────────

    def get_error_logs(
        self,
        app_name: str,
        since: str = "24 hours ago",
        limit: int = 100,
    ) -> NRQLResult:
        """Fetch error logs for a service."""
        nrql = (
            f"SELECT timestamp, message, error.class, error.message, "
            f"level, hostname "
            f"FROM Log "
            f"WHERE entity.name = '{app_name}' AND level = 'ERROR' "
            f"SINCE {since} LIMIT {limit}"
        )
        return self.execute_nrql(nrql)

    def get_error_counts_by_type(
        self,
        app_name: str,
        since: str = "24 hours ago",
    ) -> NRQLResult:
        """Get error counts grouped by error class/message."""
        nrql = (
            f"SELECT count(*) "
            f"FROM TransactionError "
            f"WHERE appName = '{app_name}' "
            f"FACET error.class, error.message "
            f"SINCE {since} LIMIT 25"
        )
        return self.execute_nrql(nrql)

    def get_transaction_errors(
        self,
        app_name: str,
        since: str = "24 hours ago",
        limit: int = 50,
    ) -> NRQLResult:
        """Fetch transaction errors with details."""
        nrql = (
            f"SELECT timestamp, transactionName, error.class, "
            f"error.message, request.uri, host "
            f"FROM TransactionError "
            f"WHERE appName = '{app_name}' "
            f"SINCE {since} LIMIT {limit}"
        )
        return self.execute_nrql(nrql)

    def get_memory_usage(
        self,
        app_name: str,
        since: str = "1 hour ago",
    ) -> NRQLResult:
        """Get memory usage over time."""
        nrql = (
            f"SELECT average(apm.service.memory.physical) as 'memory_mb' "
            f"FROM Metric "
            f"WHERE appName = '{app_name}' "
            f"SINCE {since} TIMESERIES AUTO"
        )
        return self.execute_nrql(nrql)

    def get_performance_summary(
        self,
        app_name: str,
        since: str = "24 hours ago",
    ) -> NRQLResult:
        """Get performance summary: throughput, response time, error rate."""
        nrql = (
            f"SELECT average(duration) as 'avg_response_sec', "
            f"percentile(duration, 95) as 'p95_response_sec', "
            f"count(*) as 'total_requests', "
            f"percentage(count(*), WHERE error IS true) as 'error_rate' "
            f"FROM Transaction "
            f"WHERE appName = '{app_name}' "
            f"SINCE {since}"
        )
        return self.execute_nrql(nrql)

    def get_slowest_transactions(
        self,
        app_name: str,
        since: str = "24 hours ago",
        limit: int = 10,
    ) -> NRQLResult:
        """Get slowest transaction types."""
        nrql = (
            f"SELECT average(duration) as 'avg_sec', count(*) as 'calls' "
            f"FROM Transaction "
            f"WHERE appName = '{app_name}' "
            f"FACET name "
            f"SINCE {since} LIMIT {limit}"
        )
        return self.execute_nrql(nrql)

    def get_infrastructure_metrics(
        self,
        hostname: str,
        since: str = "1 hour ago",
    ) -> NRQLResult:
        """Get CPU, memory, disk metrics for a host."""
        nrql = (
            f"SELECT average(cpuPercent) as 'cpu_pct', "
            f"average(memoryUsedPercent) as 'mem_pct', "
            f"average(diskUsedPercent) as 'disk_pct' "
            f"FROM SystemSample "
            f"WHERE hostname LIKE '%{hostname}%' "
            f"SINCE {since} TIMESERIES AUTO"
        )
        return self.execute_nrql(nrql)

    def get_recent_deployments(
        self,
        app_name: str,
        since: str = "7 days ago",
    ) -> NRQLResult:
        """Check for recent deployments that might correlate with issues."""
        nrql = (
            f"SELECT timestamp, revision, description, user "
            f"FROM Deployment "
            f"WHERE appName = '{app_name}' "
            f"SINCE {since} LIMIT 10"
        )
        return self.execute_nrql(nrql)

    def custom_query(self, nrql: str) -> NRQLResult:
        """Execute an arbitrary NRQL query."""
        return self.execute_nrql(nrql)
