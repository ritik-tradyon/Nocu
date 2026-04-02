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