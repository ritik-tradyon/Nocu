"""Scheduled Health Reports.

Runs periodic health checks across all configured services and sends
a digest via Telegram. Integrates with incident memory to detect
trends and compare against baselines.

Uses python-telegram-bot's built-in JobQueue (APScheduler under the hood)
so it runs inside the same process as the bot — no separate cron needed.
"""

import logging
from datetime import time as dt_time, datetime, timezone
from typing import Optional

from telegram.ext import ContextTypes

logger = logging.getLogger("nocu.scheduler")

# ──────────────────────────────────────────────
# Health check queries per service
# ──────────────────────────────────────────────

HEALTH_CHECK_QUERIES = {
    "error_summary": (
        "error_analysis",
        "Give me an error summary for {service} in the last {period}",
    ),
    "performance_summary": (
        "performance",
        "How has {service} performed in the last {period}",
    ),
}

DIGEST_QUESTION = "Health digest for {service} over the last {period}"


class HealthReportScheduler:
    """Schedule and run periodic health reports."""

    def __init__(self, orchestrator, config: dict):
        self.orchestrator = orchestrator
        self.config = config

        schedule_config = config.get("schedule", {})
        self.enabled = schedule_config.get("enabled", False)
        self.report_hour = schedule_config.get("report_hour", 9)
        self.report_minute = schedule_config.get("report_minute", 0)
        self.period = schedule_config.get("period", "24 hours")
        self.chat_ids = schedule_config.get("chat_ids", [])
        self.timezone_offset = schedule_config.get("timezone_offset_hours", 5.5)  # IST default

        # Which checks to run
        self.checks = schedule_config.get("checks", ["error_summary", "performance_summary"])

    def register(self, app):
        """Register the scheduled job with the telegram bot's job queue.

        Call this after building the Application but before run_polling.
        """
        if not self.enabled:
            logger.info("[scheduler] Scheduled reports disabled in config")
            return

        if not self.chat_ids:
            logger.warning(
                "[scheduler] No chat_ids configured for scheduled reports. "
                "Add schedule.chat_ids to settings.yaml"
            )
            return

        if app.job_queue is None:
            logger.warning(
                "[scheduler] JobQueue not available. "
                "Install with: pip install 'python-telegram-bot[job-queue]'"
            )
            return

        # Calculate UTC time from configured local time + offset
        # e.g., 9:00 IST (UTC+5:30) = 3:30 UTC
        utc_hour = int(self.report_hour - self.timezone_offset) % 24
        utc_minute = int(
            self.report_minute - (self.timezone_offset % 1) * 60
        ) % 60

        report_time = dt_time(hour=utc_hour, minute=utc_minute)

        app.job_queue.run_daily(
            self._run_daily_digest,
            time=report_time,
            name="nocu_daily_digest",
        )

        logger.info(
            f"[scheduler] Daily digest scheduled at "
            f"{self.report_hour:02d}:{self.report_minute:02d} local "
            f"(UTC {utc_hour:02d}:{utc_minute:02d}) "
            f"for chat_ids: {self.chat_ids}"
        )

    async def _run_daily_digest(self, context: ContextTypes.DEFAULT_TYPE):
        """Run the daily health digest and send to configured chats."""
        logger.info("[scheduler] Running daily digest...")

        services = self.orchestrator.config.get("services", {})
        if not services:
            return

        digest = await self._build_digest(services)

        for chat_id in self.chat_ids:
            try:
                # Split if needed (Telegram 4096 char limit)
                if len(digest) <= 4096:
                    await context.bot.send_message(
                        chat_id=chat_id, text=digest
                    )
                else:
                    chunks = self._split_message(digest)
                    for chunk in chunks:
                        await context.bot.send_message(
                            chat_id=chat_id, text=chunk
                        )
            except Exception as e:
                logger.error(f"[scheduler] Failed to send digest to {chat_id}: {e}")

    async def _build_digest(self, services: dict) -> str:
        """Build the full health digest across all services."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"🔭 Nocu Daily Digest — {now}",
            f"Period: last {self.period}",
            "─" * 30,
            "",
        ]

        for service_name, service_config in services.items():
            app_name = service_config.get("newrelic_app_name", "")
            if not app_name:
                continue

            service_report = await self._check_service(
                service_name, app_name
            )
            lines.append(service_report)
            lines.append("")

        # Recurring patterns across all services
        recurring_section = self._get_recurring_summary()
        if recurring_section:
            lines.append(recurring_section)

        # Memory stats
        stats = self.orchestrator.memory.get_stats()
        lines.append(
            f"📊 Memory: {stats['total_incidents']} incidents, "
            f"{stats['known_fixes']} fixes, "
            f"accuracy {stats['accuracy_rate']}"
        )

        return "\n".join(lines)

    async def _check_service(self, service_name: str, app_name: str) -> str:
        """Run health checks for a single service and return a summary."""
        fetcher = self.orchestrator.fetcher

        # Fetch key metrics
        try:
            perf = fetcher.get_performance_summary(app_name, since=self.period)
            errors = fetcher.get_error_counts_by_type(app_name, since=self.period)
        except Exception as e:
            return f"❌ {service_name}: failed to fetch data ({e})"

        # Parse performance
        status_emoji = "✅"
        issues = []

        if not perf.is_empty and perf.results:
            result = perf.results[0]
            avg_resp = result.get("avg_response_sec", 0)
            p95_resp = result.get("p95_response_sec", 0)
            total_reqs = result.get("total_requests", 0)
            error_rate = result.get("error_rate", 0)

            # Check against thresholds
            if error_rate and float(error_rate) > 5:
                status_emoji = "🔴"
                issues.append(f"error rate {error_rate:.1f}%")
            elif error_rate and float(error_rate) > 1:
                status_emoji = "🟡"
                issues.append(f"error rate {error_rate:.1f}%")

            if p95_resp and float(p95_resp) > 2.0:
                if status_emoji != "🔴":
                    status_emoji = "🟡"
                issues.append(f"p95 {p95_resp:.2f}s")

            perf_line = (
                f"  {total_reqs:,} reqs | "
                f"avg {avg_resp:.3f}s | "
                f"p95 {p95_resp:.3f}s | "
                f"err {error_rate:.1f}%"
                if total_reqs else "  no traffic"
            )
        else:
            perf_line = "  no performance data"

        # Compare with memory baseline
        baseline_note = self._compare_baseline(service_name, perf)

        # Parse errors
        error_line = ""
        if not errors.is_empty and errors.results:
            top_errors = []
            for row in errors.results[:3]:
                count = row.get("count", 0)
                error_class = row.get("error.class", "unknown")
                if count:
                    top_errors.append(f"{error_class} ({count}x)")
            if top_errors:
                error_line = f"  Top errors: {', '.join(top_errors)}"

                # Store in memory for trend tracking
                error_codes = []
                error_classes = [
                    row.get("error.class", "") for row in errors.results
                    if row.get("error.class")
                ]
                self.orchestrator.memory.store_incident(
                    question=f"[scheduled] Health check for {service_name}",
                    query_type="error_analysis",
                    service_name=service_name,
                    time_range=self.period,
                    search_terms=[],
                    error_codes=error_codes,
                    error_classes=error_classes,
                    nr_data_summary=f"perf: {perf.results[0] if perf.results else 'none'}",
                    analyzer_used="scheduler",
                    analysis=f"Automated health check. {len(errors.results)} error types found.",
                )

        # Build service section
        header = f"{status_emoji} {service_name}"
        if issues:
            header += f" — {', '.join(issues)}"

        parts = [header, perf_line]
        if error_line:
            parts.append(error_line)
        if baseline_note:
            parts.append(f"  {baseline_note}")

        return "\n".join(parts)

    def _compare_baseline(self, service_name: str, current_perf) -> str:
        """Compare current metrics against recent incident history baseline."""
        if current_perf.is_empty or not current_perf.results:
            return ""

        history = self.orchestrator.memory.get_service_history(
            service_name, limit=7
        )
        if len(history) < 3:
            return ""  # not enough data for comparison

        # Check for error rate trending up
        current_result = current_perf.results[0]
        current_error_rate = current_result.get("error_rate", 0)
        if not current_error_rate:
            return ""

        try:
            current_error_rate = float(current_error_rate)
        except (ValueError, TypeError):
            return ""

        # Count how many recent incidents had errors
        recent_error_count = sum(
            1 for h in history
            if h.get("error_codes") and h["error_codes"] != "[]"
        )

        if recent_error_count >= 3 and current_error_rate > 1:
            return "⚠️ Errors recurring — check /recurring " + service_name

        return ""

    def _get_recurring_summary(self) -> str:
        """Get a cross-service recurring errors summary."""
        services = self.orchestrator.config.get("services", {})
        recurring_lines = []

        for service_name in services:
            recurring = self.orchestrator.memory.get_recurring_errors(
                service_name, min_occurrences=3
            )
            for rec in recurring:
                codes = rec.get("error_codes", "[]")
                count = rec.get("occurrence_count", 0)
                recurring_lines.append(
                    f"  {service_name}: {codes} — seen {count}x"
                )

        if recurring_lines:
            return "🔄 Recurring patterns:\n" + "\n".join(recurring_lines)
        return ""

    def _split_message(self, text: str) -> list[str]:
        """Split message for Telegram's 4096 char limit."""
        if len(text) <= 4096:
            return [text]

        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= 4096:
                chunks.append(remaining)
                break
            split_at = remaining[:4096].rfind("\n\n")
            if split_at < 2000:
                split_at = remaining[:4096].rfind("\n")
            if split_at < 1000:
                split_at = 4095
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip()
        return chunks

    # ──────────────────────────────────────────────
    # On-demand digest (via /digest command)
    # ──────────────────────────────────────────────

    async def run_on_demand(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        """Run digest immediately (triggered by /digest command)."""
        services = self.orchestrator.config.get("services", {})
        if not services:
            await context.bot.send_message(
                chat_id=chat_id, text="No services configured."
            )
            return

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔭 Running health check across {len(services)} services..."
        )

        digest = await self._build_digest(services)

        if len(digest) <= 4096:
            await context.bot.send_message(chat_id=chat_id, text=digest)
        else:
            for chunk in self._split_message(digest):
                await context.bot.send_message(chat_id=chat_id, text=chunk)
