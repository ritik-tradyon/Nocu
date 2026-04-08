"""Manual test script for BlastRadiusAnalyzer.

Run with:
    python tests/test_blast_radius.py

Test 1: Parser unit test (no API calls needed)
Test 2: Live integration test (requires settings.yaml + New Relic access)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analyzers.blast_radius import BlastRadiusAnalyzer, BlastRadiusResult, AffectedRoute


# ──────────────────────────────────────────────────────────────
# Sample FUNCTION-MAP.md content (mirrors real deepmap format)
# Taken from a representative FastAPI service structure
# ──────────────────────────────────────────────────────────────
SAMPLE_FUNCTION_MAP = """
## Routes (3)

- POST /api/auth/login → login() — Authenticate a user
  Chain: login → verify_credentials → check_password_hash
- POST /api/auth/register → register() — Register new user
  Chain: register → verify_credentials → hash_password
- GET /api/auth/me → get_me() — Get current user profile

## All Functions (12)

### auth.routes (auth/routes.py)
- async login(body: LoginRequest, db: Session) [ROUTE: POST /api/auth/login] [auth/routes.py:45]
  → calls: verify_credentials
  ← called by:
- async register(body: RegisterRequest, db: Session) [ROUTE: POST /api/auth/register] [auth/routes.py:78]
  → calls: verify_credentials, hash_password
  ← called by:
- async get_me(current_user: User) [ROUTE: GET /api/auth/me] [auth/routes.py:112]
  ← called by:

### auth.services (auth/services.py)
- async verify_credentials(email: str, password: str, db: Session) → User [auth/services.py:20]
  → calls: get_user_by_email, check_password_hash
  ← called by: login, register
- async get_user_by_email(email: str, db: Session) → Optional[User] [auth/services.py:45]
  ← called by: verify_credentials
- check_password_hash(plain: str, hashed: str) → bool [auth/services.py:60]
  ← called by: verify_credentials
- hash_password(plain: str) → str [auth/services.py:68]
  ← called by: register

### auth.utils (auth/utils.py)
- create_access_token(data: dict, expires_delta: timedelta) → str [auth/utils.py:15]
  ← called by: login
- decode_token(token: str) → dict [auth/utils.py:30]
  ← called by: get_me

## Call Graph (caller → callee)

- login → verify_credentials, create_access_token
- register → verify_credentials, hash_password
- verify_credentials → get_user_by_email, check_password_hash
"""


# ──────────────────────────────────────────────────────────────
# Minimal mock NewRelicFetcher (no network calls)
# ──────────────────────────────────────────────────────────────
class MockNRFetcher:
    def execute_nrql(self, nrql: str):
        from analyzers.blast_radius import BlastRadiusResult
        from dataclasses import dataclass
        from typing import Optional

        # Simulate NR returning transaction data
        class FakeResult:
            query = nrql
            error = None
            results = [
                {"name": "WebTransaction/FastAPI/login", "count": 12500},
                {"name": "WebTransaction/FastAPI/register", "count": 340},
                {"name": "WebTransaction/FastAPI/get_me", "count": 78},
            ]
        return FakeResult()


# ──────────────────────────────────────────────────────────────
# Minimal settings (no file paths needed for parser test)
# ──────────────────────────────────────────────────────────────
MOCK_SETTINGS = {
    "services": {
        "testservice": {
            "newrelic_app_name": "testservice-prod",
        }
    },
    "code_context": {
        "deepmap": {
            "output_dir": "/nonexistent",
        }
    },
    "features": {
        "blast_radius": {
            "traffic_window_days": 7,
            "thresholds": {"high": 1000, "medium": 100},
        }
    },
}


# ──────────────────────────────────────────────────────────────
# Test 1: Parser + graph traversal (no network calls)
# ──────────────────────────────────────────────────────────────
def test_parser():
    print("=" * 60)
    print("TEST 1: Parser unit test (no API calls)")
    print("=" * 60)

    analyzer = BlastRadiusAnalyzer(MockNRFetcher(), MOCK_SETTINGS)

    # --- 1a. Match by function name ---
    graph = analyzer._parse_deepmap_graph(SAMPLE_FUNCTION_MAP, "verify_credentials")

    assert not graph["not_found"], "Should have found verify_credentials"
    assert "verify_credentials" in graph["matched_functions"], (
        f"Expected verify_credentials in matched_functions, got {graph['matched_functions']}"
    )

    route_handlers = {r["handler"] for r in graph["affected_routes"]}
    assert "login" in route_handlers, f"Expected login in affected_routes, got {route_handlers}"
    assert "register" in route_handlers, f"Expected register in affected_routes, got {route_handlers}"
    assert "get_me" not in route_handlers, f"get_me should NOT be affected"

    route_methods = {r["handler"]: r["method"] for r in graph["affected_routes"]}
    assert route_methods["login"] == "POST", f"Expected POST, got {route_methods['login']}"
    assert route_methods["register"] == "POST", f"Expected POST, got {route_methods['register']}"

    route_paths = {r["handler"]: r["path"] for r in graph["affected_routes"]}
    assert route_paths["login"] == "/api/auth/login", f"Wrong path: {route_paths['login']}"

    print("  [PASS] verify_credentials → correct affected routes (login, register)")
    print(f"         matched: {graph['matched_functions']}")
    print(f"         upstream chain: {graph['upstream_chain']}")
    print(f"         affected routes: {[r['handler'] for r in graph['affected_routes']]}")

    # --- 1b. Match by file path ---
    graph_file = analyzer._parse_deepmap_graph(SAMPLE_FUNCTION_MAP, "auth/services.py")
    assert not graph_file["not_found"], "Should have matched auth/services.py"
    expected_service_fns = {"verify_credentials", "get_user_by_email", "check_password_hash", "hash_password"}
    matched_set = set(graph_file["matched_functions"])
    assert expected_service_fns == matched_set, (
        f"Expected {expected_service_fns}, got {matched_set}"
    )
    print(f"  [PASS] auth/services.py → matched functions: {sorted(matched_set)}")

    # --- 1c. Not found ---
    graph_miss = analyzer._parse_deepmap_graph(SAMPLE_FUNCTION_MAP, "nonexistent_xyz")
    assert graph_miss["not_found"], "Should return not_found for unknown target"
    print("  [PASS] nonexistent_xyz → not_found=True")

    # --- 1d. Full analyze with mock NR ---
    # Patch _load_deepmap_content to return our sample
    original_load = analyzer._load_deepmap_content
    analyzer._load_deepmap_content = lambda svc: SAMPLE_FUNCTION_MAP

    result = analyzer.analyze("testservice", "verify_credentials")

    assert not result.not_found
    assert result.target == "verify_credentials"
    assert result.service_name == "testservice"
    assert len(result.affected_routes) == 2

    login_route = next(r for r in result.affected_routes if r.handler == "login")
    register_route = next(r for r in result.affected_routes if r.handler == "register")

    assert login_route.weekly_requests == 12500, f"Expected 12500, got {login_route.weekly_requests}"
    assert login_route.risk_level == "HIGH", f"Expected HIGH, got {login_route.risk_level}"
    assert register_route.weekly_requests == 340, f"Expected 340, got {register_route.weekly_requests}"
    assert register_route.risk_level == "MEDIUM", f"Expected MEDIUM, got {register_route.risk_level}"

    # login (12500) should come before register (340) — sorted by traffic desc
    assert result.affected_routes[0].handler == "login", "login should be first (highest traffic)"

    print(f"  [PASS] Full analyze: login={login_route.weekly_requests} req HIGH, "
          f"register={register_route.weekly_requests} req MEDIUM")

    analyzer._load_deepmap_content = original_load
    print("\nTEST 1 PASSED\n")


# ──────────────────────────────────────────────────────────────
# Test 2: Live integration test (requires settings.yaml)
# ──────────────────────────────────────────────────────────────
def test_live(service_name: str, target: str):
    print("=" * 60)
    print(f"TEST 2: Live integration test ({service_name} / {target})")
    print("=" * 60)

    try:
        import yaml
        config_path = os.environ.get("NOCU_CONFIG", "config/settings.yaml")
        with open(config_path) as f:
            settings = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"  [SKIP] config/settings.yaml not found — skipping live test")
        return

    from fetchers.newrelic import NewRelicFetcher
    nr_cfg = settings["newrelic"]
    fetcher = NewRelicFetcher(
        api_key=nr_cfg["api_key"],
        account_id=nr_cfg["account_id"],
        region=nr_cfg.get("region", "US"),
    )

    analyzer = BlastRadiusAnalyzer(fetcher, settings)
    result = analyzer.analyze(service_name, target)

    print(f"\nBlastRadiusResult for '{target}' in {service_name}:")
    print(f"  not_found:         {result.not_found}")
    if result.error:
        print(f"  error:             {result.error}")
    print(f"  matched_functions: {result.matched_functions}")
    print(f"  upstream_chain:    {result.upstream_chain[:10]}{'...' if len(result.upstream_chain) > 10 else ''}")
    print(f"  affected_routes:   {len(result.affected_routes)}")
    for r in result.affected_routes:
        print(f"    [{r.risk_level}] {r.method} {r.path}  ({r.weekly_requests:,} req/week)  handler={r.handler}()")

    # Also print formatted output
    from core.formatter import format_blast_radius
    print("\n--- Telegram output preview ---")
    for msg in format_blast_radius(result):
        print(msg)
        print("---")

    print("\nTEST 2 COMPLETE\n")


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Always run Test 1 first — it has no external dependencies
    test_parser()

    # Test 2: live — pass service and target as CLI args or use defaults
    # Usage: python tests/test_blast_radius.py [service] [function]
    service = sys.argv[1] if len(sys.argv) > 1 else "pehchaan"
    target = sys.argv[2] if len(sys.argv) > 2 else "verify_token"

    test_live(service, target)
