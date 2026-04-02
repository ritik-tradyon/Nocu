"""
Emulates a Telegram user sending a deep-dive query through the full Nocu pipeline.
Run from the nocu project root:
    python test_claude_flow.py
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.orchestrator import NocuOrchestrator

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)

QUESTION = (
    "For the past 30 minutes athena has given 500 in the response, "
    "do a deep dive as to what went wrong"
)

CONFIG_PATH = "config/settings.yaml"


async def main():
    print("\n" + "=" * 60)
    print("Nocu pipeline test")
    print(f"Question: {QUESTION}")
    print("=" * 60 + "\n")

    orchestrator = NocuOrchestrator(CONFIG_PATH)

    # Dump resolved invocation so we can see exactly what subprocess will run
    ca = orchestrator.claude_analyzer
    print(f"[test] cli_path    : {ca.cli_path}")
    print(f"[test] _node_bin   : {ca._node_bin}")
    print(f"[test] _cli_script : {ca._cli_script}")
    print(f"[test] enabled     : {ca.enabled}")

    available = ca.is_available()
    print(f"[test] is_available: {available}")
    print()

    async def status_callback(msg: str):
        print(f"  [status] {msg}")

    responses = await orchestrator.process_question(
        question=QUESTION,
        status_callback=status_callback,
    )

    print("\n" + "=" * 60)
    print("RESPONSE:")
    print("=" * 60)
    for r in responses:
        print(r)
        print("-" * 60)


if __name__ == "__main__":
    asyncio.run(main())
