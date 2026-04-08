"""Format analysis results for Telegram delivery.

Handles Telegram's message length limits (4096 chars) and formatting.
"""

from typing import Optional


# Telegram max message length
MAX_MESSAGE_LENGTH = 4096


def format_response(
    analysis: str,
    query_type: str,
    service_name: str,
    time_range: str,
    analyzer_used: str,
    error: Optional[str] = None,
) -> list[str]:
    """Format an analysis result for Telegram.

    Returns a list of messages (split if exceeding Telegram's limit).
    """
    if error:
        return [f"❌ Error analyzing {service_name}: {error}"]

    # Build the header
    type_emoji = {
        "error_analysis": "🔴",
        "memory_spike": "📈",
        "performance": "📊",
        "latency": "🐌",
        "general": "🔍",
    }
    emoji = type_emoji.get(query_type, "🔍")
    header = f"{emoji} {service_name} — {query_type.replace('_', ' ').title()}\n"
    header += f"⏰ Period: {time_range}\n"
    header += f"🤖 Analyzed by: {analyzer_used}\n"
    header += "─" * 30 + "\n\n"

    full_text = header + analysis

    # Split into chunks if needed
    return _split_message(full_text)


def format_error_message(error: str) -> str:
    """Format an error message."""
    return f"❌ Nocu Error\n\n{error}"


def format_status_message(service_name: str, status: str) -> str:
    """Format a status update (e.g., 'analyzing...')."""
    return f"🔭 Analyzing {service_name}...\n{status}"


def format_blast_radius(result) -> list[str]:
    """Format a BlastRadiusResult for Telegram delivery.

    Returns a list of messages (split if exceeding Telegram's limit).
    """
    if result.not_found:
        if result.error:
            msg = f"❓ Couldn't find \"{result.target}\" in {result.service_name}.\n\n{result.error}"
        else:
            msg = (
                f"❓ Couldn't find \"{result.target}\" in {result.service_name}'s deepmap.\n\n"
                f"Run deepmap on this service first, or check the function/file name spelling."
            )
        return [msg]

    routes = result.affected_routes
    if not routes:
        return [
            f"💥 Blast Radius: {result.target} ({result.service_name})\n\n"
            f"No route handlers found upstream of this function.\n"
            f"It may be a utility function not reachable from any endpoint, "
            f"or deepmap may be out of date."
        ]

    # Header
    total_requests = sum(r.weekly_requests for r in routes)
    lines = [
        f"💥 Blast Radius: {result.target} ({result.service_name})",
        f"",
        f"Changing this affects {len(routes)} route{'s' if len(routes) != 1 else ''}:",
        f"",
    ]

    # Group by risk level
    risk_order = ["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
    risk_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢", "UNKNOWN": "⚪"}
    risk_label = {"HIGH": "HIGH RISK", "MEDIUM": "MEDIUM RISK", "LOW": "LOW RISK", "UNKNOWN": "UNKNOWN (no NR data)"}

    by_risk = {level: [] for level in risk_order}
    for route in routes:
        by_risk[route.risk_level].append(route)

    for level in risk_order:
        group = by_risk[level]
        if not group:
            continue
        lines.append(f"{risk_emoji[level]} {risk_label[level]}")
        for r in group:
            if r.weekly_requests > 0:
                lines.append(f"{r.method} {r.path} ({r.weekly_requests:,} req/week)")
            else:
                lines.append(f"{r.method} {r.path}")
            lines.append(f"  handler: {r.handler}()")
        lines.append("")

    # Call chain summary (show target → immediate callers → route handlers)
    matched_set = set(result.matched_functions)
    route_handlers = {r.handler for r in routes}
    intermediate = [
        fn for fn in result.upstream_chain
        if fn not in matched_set and fn not in route_handlers
    ]
    chain_parts = list(result.matched_functions)
    if intermediate:
        chain_parts.append(", ".join(intermediate[:8]))
    if route_handlers:
        chain_parts.append(", ".join(sorted(route_handlers)))
    lines.append("Call chain: " + " ← ".join(chain_parts))
    lines.append("")

    # Risk summary
    if total_requests > 0:
        lines.append(
            f"⚠️ Touch this with care — {total_requests:,} requests/week "
            f"across {len(routes)} route{'s' if len(routes) != 1 else ''} depend on it."
        )
    else:
        lines.append(
            f"⚠️ {len(routes)} route{'s' if len(routes) != 1 else ''} depend on this. "
            f"No New Relic traffic data found — routes may be inactive or not yet instrumented."
        )

    return _split_message("\n".join(lines))


def _split_message(text: str) -> list[str]:
    """Split a long message into Telegram-compatible chunks.

    Tries to split at paragraph boundaries, then sentence boundaries.
    """
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    messages = []
    remaining = text

    while remaining:
        if len(remaining) <= MAX_MESSAGE_LENGTH:
            messages.append(remaining)
            break

        # Find a good split point
        chunk = remaining[:MAX_MESSAGE_LENGTH]

        # Try to split at a paragraph break
        split_at = chunk.rfind("\n\n")
        if split_at < MAX_MESSAGE_LENGTH // 2:
            # No good paragraph break — try newline
            split_at = chunk.rfind("\n")
        if split_at < MAX_MESSAGE_LENGTH // 2:
            # No good newline — try sentence end
            split_at = chunk.rfind(". ")
            if split_at > 0:
                split_at += 1  # include the period
        if split_at < MAX_MESSAGE_LENGTH // 4:
            # Last resort: hard split
            split_at = MAX_MESSAGE_LENGTH - 1

        messages.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return messages