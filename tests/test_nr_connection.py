"""
Diagnostic script — tests New Relic connectivity and discovers real app/entity names.

Run directly:
    python tests/test_nr_connection.py

Reads credentials from config/settings.yaml.
Does NOT write to any file or database — all output is in-memory and printed.
"""

import sys
import os
import yaml
import json
import requests

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIG_PATH = "config/settings.yaml"
NERDGRAPH_URL = "https://api.newrelic.com/graphql"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def nerdgraph(api_key: str, account_id: str, nrql: str) -> dict:
    """Execute a raw NRQL query, return the full NerdGraph response dict."""
    query = """
    {
        actor {
            account(id: %s) {
                nrql(query: "%s") {
                    results
                    metadata { eventTypes }
                }
            }
        }
    }
    """ % (account_id, nrql.replace('"', '\\"'))

    resp = requests.post(
        NERDGRAPH_URL,
        json={"query": query},
        headers={"Content-Type": "application/json", "API-Key": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def run(label: str, api_key: str, account_id: str, nrql: str):
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"NRQL: {nrql}")
    print("-" * 60)
    try:
        data = nerdgraph(api_key, account_id, nrql)

        if "errors" in data:
            print("  [NerdGraph ERROR]")
            for e in data["errors"]:
                print(f"    {e.get('message')}")
            return

        results = data["data"]["actor"]["account"]["nrql"]["results"]
        if not results:
            print("  [EMPTY] — 0 rows returned")
        else:
            print(f"  [OK] {len(results)} rows")
            for i, row in enumerate(results[:5]):
                print(f"    row {i+1}: {json.dumps(row, default=str)}")
            if len(results) > 5:
                print(f"    ... and {len(results) - 5} more")
    except Exception as e:
        print(f"  [EXCEPTION] {type(e).__name__}: {e}")


def main():
    cfg = load_config()
    api_key = cfg["newrelic"]["api_key"]
    account_id = cfg["newrelic"]["account_id"]

    print(f"Account ID : {account_id}")
    print(f"API Key    : {api_key[:8]}... (truncated)")

    # ── 1. Basic connectivity check ──────────────────────────────────────
    run(
        "Basic connectivity — count all transactions in last hour",
        api_key, account_id,
        "SELECT count(*) FROM Transaction SINCE 1 hour ago",
    )

    # ── 2. Discover actual appName values in Transaction events ──────────
    run(
        "Discover appNames in Transaction (last 1 hour)",
        api_key, account_id,
        "SELECT uniques(appName) FROM Transaction SINCE 1 hour ago LIMIT 50",
    )

    # ── 3. Discover entity.name values in Log events ─────────────────────
    run(
        "Discover entity.name values in Log (last 1 hour)",
        api_key, account_id,
        "SELECT uniques(entity.name) FROM Log SINCE 1 hour ago LIMIT 50",
    )

    # ── 4. Discover level values in Log events for hermes ────────────────
    #    This tells us if 'level' is 'ERROR', 'error', 'Error', etc.
    hermes_app = cfg.get("services", {}).get("hermes", {}).get("newrelic_app_name", "Hermes-Production")
    run(
        f"Discover level values in Log for {hermes_app} (last 1 hour)",
        api_key, account_id,
        f"SELECT uniques(level) FROM Log WHERE entity.name = '{hermes_app}' SINCE 1 hour ago",
    )

    # ── 5. Logs WITHOUT level filter — to check if any logs exist at all ─
    run(
        f"All Log events for {hermes_app} — no level filter (last 1 hour)",
        api_key, account_id,
        f"SELECT count(*) FROM Log WHERE entity.name = '{hermes_app}' SINCE 1 hour ago",
    )

    # ── 6. Logs matching any error/critical level (case-insensitive via IN) ─
    run(
        f"Error/critical logs for {hermes_app} using IN (last 1 hour)",
        api_key, account_id,
        (
            f"SELECT timestamp, message, level FROM Log "
            f"WHERE entity.name = '{hermes_app}' "
            f"AND level IN ('ERROR', 'error', 'Error', 'CRITICAL', 'critical') "
            f"SINCE 1 hour ago LIMIT 10"
        ),
    )

    # ── 6b. Sample of all logs (no level filter) to see what's actually there ──
    run(
        f"Sample of all logs for {hermes_app} — no level filter (last 1 hour)",
        api_key, account_id,
        (
            f"SELECT timestamp, message, level FROM Log "
            f"WHERE entity.name = '{hermes_app}' "
            f"SINCE 1 hour ago LIMIT 5"
        ),
    )

    # ── 7. TransactionError for hermes — verify appName works ────────────
    run(
        f"TransactionError for {hermes_app} (last 1 hour)",
        api_key, account_id,
        (
            f"SELECT count(*) FROM TransactionError "
            f"WHERE appName = '{hermes_app}' SINCE 1 hour ago"
        ),
    )

    # ── 8. Check all services from config ────────────────────────────────
    print(f"\n{'='*60}")
    print("TEST: Transaction counts for all configured services (last 1 hour)")
    print("-" * 60)
    for svc_name, svc_cfg in cfg.get("services", {}).items():
        app = svc_cfg.get("newrelic_app_name", "")
        nrql = f"SELECT count(*) FROM Transaction WHERE appName = '{app}' SINCE 1 hour ago"
        try:
            data = nerdgraph(api_key, account_id, nrql)
            if "errors" in data:
                print(f"  {svc_name} ({app}): NerdGraph ERROR")
                continue
            results = data["data"]["actor"]["account"]["nrql"]["results"]
            count = results[0].get("count", 0) if results else 0
            status = "OK" if count > 0 else "EMPTY — not reporting or no traffic"
            print(f"  {svc_name} ({app}): {count} transactions — {status}")
        except Exception as e:
            print(f"  {svc_name} ({app}): EXCEPTION {e}")


if __name__ == "__main__":
    main()
