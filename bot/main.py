"""Nocu Telegram Bot.

Standalone bot that receives questions and runs the Nocu pipeline.
No OpenClaw dependency — direct python-telegram-bot integration.

Setup:
1. Message @BotFather on Telegram → /newbot → get your token
2. Add token to config/settings.yaml
3. Run: python -m bot.main
"""

import os
import sys
import asyncio
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.orchestrator import NocuOrchestrator
from core.scheduler import HealthReportScheduler
from core.formatter import format_blast_radius
from analyzers.blast_radius import BlastRadiusAnalyzer

# Logging
logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("nocu.bot")


# ── Global orchestrator and scheduler (initialized on startup) ──
orchestrator: NocuOrchestrator = None
scheduler: HealthReportScheduler = None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    services = list(orchestrator.config.get("services", {}).keys())
    services_str = ", ".join(services) if services else "(none configured)"

    await update.message.reply_text(
        "🔭 Nocu — Binoculars for Production\n\n"
        "Ask me anything about your production services:\n\n"
        "• 'What errors happened in pehchaan in the last 24 hours?'\n"
        "• 'How is order-service performing?'\n"
        "• 'What's causing the memory spike in auth-service?'\n"
        "• 'Why are responses slow in gateway?'\n\n"
        f"Configured services: {services_str}\n\n"
        "Commands:\n"
        "/start — Show this help\n"
        "/services — List configured services\n"
        "/status — Check Nocu health\n"
        "/index <service> — Re-index a service's code\n"
        "/blast <service> <func/file> — Show blast radius of a code change\n\n"
        "Feedback (after each analysis):\n"
        "/useful <id> — Mark analysis as helpful\n"
        "/notuseful <id> — Mark as not helpful\n"
        "/fix <id> <desc> — Record what actually fixed it\n\n"
        "Memory:\n"
        "/history <service> — Past incidents for a service\n"
        "/recurring <service> — Recurring error patterns\n"
        "/digest — Run health report for all services now"
    )


async def services_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /services command."""
    services = orchestrator.config.get("services", {})
    if not services:
        await update.message.reply_text("No services configured. Edit config/settings.yaml")
        return

    lines = ["📋 Configured Services:\n"]
    for name, cfg in services.items():
        indexed = name in orchestrator.service_indexes
        status = "✅ indexed" if indexed else "⚠️ not indexed"
        desc = cfg.get("description", "")
        lines.append(f"  • {name} ({cfg.get('framework', '?')}) — {status}")
        if desc:
            lines.append(f"    {desc}")

    await update.message.reply_text("\n".join(lines))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command — check component health."""
    checks = []

    # Check Gemini
    try:
        test = orchestrator.classifier.classify("test query for pehchaan errors")
        checks.append("✅ Gemini classifier: OK")
    except Exception as e:
        checks.append(f"❌ Gemini classifier: {e}")

    # Check New Relic
    try:
        result = orchestrator.fetcher.execute_nrql("SELECT count(*) FROM Transaction SINCE 1 minute ago")
        if result.error:
            checks.append(f"⚠️ New Relic: {result.error}")
        else:
            checks.append("✅ New Relic API: OK")
    except Exception as e:
        checks.append(f"❌ New Relic: {e}")

    # Check Claude Code
    if orchestrator.claude_analyzer.is_available():
        checks.append("✅ Claude Code CLI: available")
    else:
        checks.append("ℹ️ Claude Code CLI: not available (will use Gemini for all queries)")

    # Memory stats
    mem_stats = orchestrator.memory.get_stats()
    checks.append(
        f"🧠 Incident memory: {mem_stats['total_incidents']} incidents, "
        f"{mem_stats['known_fixes']} fixes, accuracy {mem_stats['accuracy_rate']}"
    )

    await update.message.reply_text("🔭 Nocu Status\n\n" + "\n".join(checks))


async def index_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /index <service> — re-index a service."""
    if not context.args:
        await update.message.reply_text("Usage: /index <service_name>")
        return

    service_name = context.args[0].lower()
    service_config = orchestrator.config.get("services", {}).get(service_name)

    if not service_config:
        await update.message.reply_text(
            f"Unknown service '{service_name}'. "
            f"Available: {', '.join(orchestrator.config.get('services', {}).keys())}"
        )
        return

    repo_path = service_config.get("repo_path")
    if not repo_path or not os.path.exists(repo_path):
        await update.message.reply_text(
            f"Repo path not found: {repo_path}\n"
            f"Update config/settings.yaml with the correct path."
        )
        return

    await update.message.reply_text(f"🔄 Re-indexing {service_name}...")

    try:
        from indexer.scanner import scan_repository

        index_config = orchestrator.config.get("indexer", {})
        index = scan_repository(
            repo_path=repo_path,
            service_name=service_name,
            framework=service_config.get("framework", "fastapi"),
            exclude_dirs=index_config.get("exclude_dirs"),
        )

        index_dir = index_config.get("index_dir", ".nocu_index")
        output_path = index.save(index_dir)
        orchestrator.service_indexes[service_name] = index

        await update.message.reply_text(
            f"✅ Indexed {service_name}\n"
            f"  Files: {len(index.files)}\n"
            f"  Endpoints: {len(index.endpoints)}\n"
            f"  External calls: {len(index.outbound_calls)}\n"
            f"  Saved to: {output_path}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Indexing failed: {e}")


async def useful_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /useful <incident_id> — mark analysis as useful."""
    if not context.args:
        await update.message.reply_text("Usage: /useful <incident_id>")
        return

    incident_id = context.args[0]
    found = orchestrator.memory.record_feedback(incident_id, was_useful=True)

    if found:
        await update.message.reply_text(f"✅ Marked {incident_id} as useful. Thanks!")
    else:
        await update.message.reply_text(f"❌ Incident {incident_id} not found.")


async def notuseful_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /notuseful <incident_id> — mark analysis as not useful."""
    if not context.args:
        await update.message.reply_text("Usage: /notuseful <incident_id>")
        return

    incident_id = context.args[0]
    found = orchestrator.memory.record_feedback(incident_id, was_useful=False)

    if found:
        await update.message.reply_text(
            f"📝 Marked {incident_id} as not useful.\n"
            f"You can record what the actual fix was with:\n"
            f"/fix {incident_id} <description of what you did>"
        )
    else:
        await update.message.reply_text(f"❌ Incident {incident_id} not found.")


async def fix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /fix <incident_id> <description> — record the actual fix."""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /fix <incident_id> <what you did to fix it>\n"
            "Example: /fix abc123 added connection pool timeout in db_session.py"
        )
        return

    incident_id = context.args[0]
    fix_description = " ".join(context.args[1:])

    found = orchestrator.memory.record_feedback(
        incident_id,
        was_useful=True,  # if they're recording a fix, the analysis led somewhere
        actual_fix=fix_description,
    )

    if found:
        await update.message.reply_text(
            f"✅ Recorded fix for {incident_id}:\n\"{fix_description}\"\n\n"
            f"Nocu will reference this if a similar issue comes up again."
        )
    else:
        await update.message.reply_text(f"❌ Incident {incident_id} not found.")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history <service> — show recent incidents for a service."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /history <service_name>\n"
            f"Available: {', '.join(orchestrator.config.get('services', {}).keys())}"
        )
        return

    service_name = context.args[0].lower()
    history = orchestrator.memory.get_service_history(service_name, limit=10)

    if not history:
        await update.message.reply_text(f"No incidents recorded for {service_name}.")
        return

    lines = [f"📋 Recent incidents for {service_name}:\n"]
    for inc in history:
        ts = inc["timestamp"][:10]
        useful_icon = ""
        if inc["was_useful"] == 1:
            useful_icon = " ✅"
        elif inc["was_useful"] == 0:
            useful_icon = " ❌"

        fix = f"\n    Fix: {inc['actual_fix']}" if inc["actual_fix"] else ""
        lines.append(
            f"  [{inc['id']}] {ts} — {inc['query_type']}{useful_icon}\n"
            f"    Q: {inc['question'][:80]}{fix}"
        )

    await update.message.reply_text("\n".join(lines))


async def recurring_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /recurring <service> — show recurring error patterns."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /recurring <service_name>"
        )
        return

    service_name = context.args[0].lower()
    recurring = orchestrator.memory.get_recurring_errors(service_name)

    if not recurring:
        await update.message.reply_text(
            f"No recurring patterns found for {service_name}.\n"
            f"(Need at least 2 incidents with matching error patterns.)"
        )
        return

    lines = [f"🔄 Recurring errors in {service_name}:\n"]
    for rec in recurring:
        codes = rec["error_codes"] or "none"
        classes = rec["error_classes"] or "none"
        lines.append(
            f"  {rec['occurrence_count']}x — "
            f"codes: {codes}, classes: {classes}\n"
            f"    First: {rec['first_seen'][:10]}, Last: {rec['last_seen'][:10]}"
        )
        if rec["fixes_applied"]:
            lines.append(f"    Fixes tried: {rec['fixes_applied']}")

    await update.message.reply_text("\n".join(lines))


async def digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /digest — run health report on demand."""
    if scheduler:
        await scheduler.run_on_demand(context, update.effective_chat.id)
    else:
        await update.message.reply_text(
            "Scheduler not initialized. Check config/settings.yaml schedule section."
        )


async def blast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /blast <service> <target> — show blast radius of a code change."""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /blast <service> <function_or_file>\n\n"
            "Examples:\n"
            "  /blast pehchaan verify_credentials\n"
            "  /blast odin app/services/payment.py\n"
            "  /blast hermes process_notification\n\n"
            f"Available services: {', '.join(orchestrator.config.get('services', {}).keys())}"
        )
        return

    service_name = context.args[0].lower()
    target = " ".join(context.args[1:])

    services = orchestrator.config.get("services", {})
    if service_name not in services:
        await update.message.reply_text(
            f"Unknown service '{service_name}'.\n"
            f"Available: {', '.join(services.keys())}"
        )
        return

    await update.message.reply_text(f"🔍 Analyzing blast radius for `{target}` in {service_name}...")

    try:
        analyzer = BlastRadiusAnalyzer(
            newrelic_fetcher=orchestrator.fetcher,
            settings=orchestrator.config,
        )
        result = analyzer.analyze(service_name, target)
        messages = format_blast_radius(result)
        for msg in messages:
            await update.message.reply_text(msg)
    except Exception as e:
        logger.exception("blast_command error service=%s target=%r error=%s", service_name, target, e)
        await update.message.reply_text(
            f"❌ Blast radius analysis failed.\n\nError: {str(e)[:300]}\n\nCheck logs for details."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle any text message as a Nocu query."""
    # Security: check if chat is allowed
    allowed_ids = orchestrator.config.get("telegram", {}).get("allowed_chat_ids", [])
    if allowed_ids and update.effective_chat.id not in allowed_ids:
        await update.message.reply_text(
            "🔒 This bot is restricted. Your chat ID is not in the allowed list.\n"
            f"Your chat ID: {update.effective_chat.id}"
        )
        return

    question = update.message.text.strip()
    if not question:
        return

    logger.info(f"Query from {update.effective_user.username}: {question}")

    # Status callback to send progress updates
    async def send_status(status: str):
        try:
            await update.message.reply_text(status)
        except Exception:
            pass

    try:
        responses = await orchestrator.process_question(
            question=question,
            user_id=str(update.effective_user.id),
            status_callback=send_status,
        )

        for response in responses:
            await update.message.reply_text(response)

    except Exception as e:
        logger.exception(f"Pipeline error: {e}")
        await update.message.reply_text(
            f"❌ Something went wrong during analysis.\n\n"
            f"Error: {str(e)[:500]}\n\n"
            f"Try rephrasing your question or check /status."
        )


def main():
    """Start the Nocu Telegram bot."""
    global orchestrator, scheduler

    # Find config file
    config_path = os.environ.get("NOCU_CONFIG", "config/settings.yaml")
    if not os.path.exists(config_path):
        print(f"Config not found at {config_path}")
        print("Copy config/settings.example.yaml → config/settings.yaml and fill in your keys.")
        sys.exit(1)

    # Initialize orchestrator
    print("[nocu] Initializing...")
    orchestrator = NocuOrchestrator(config_path)

    # Initialize scheduler
    scheduler = HealthReportScheduler(orchestrator, orchestrator.config)

    # Get bot token
    bot_token = orchestrator.config.get("telegram", {}).get("bot_token")
    if not bot_token or bot_token == "YOUR_TELEGRAM_BOT_TOKEN":
        print("Set your Telegram bot token in config/settings.yaml")
        print("Get one from @BotFather on Telegram.")
        sys.exit(1)

    # Build and start bot
    app = ApplicationBuilder().token(bot_token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("services", services_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("index", index_command))
    app.add_handler(CommandHandler("useful", useful_command))
    app.add_handler(CommandHandler("notuseful", notuseful_command))
    app.add_handler(CommandHandler("fix", fix_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("recurring", recurring_command))
    app.add_handler(CommandHandler("digest", digest_command))
    app.add_handler(CommandHandler("blast", blast_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Register scheduled health reports
    scheduler.register(app)

    print("[nocu] 🔭 Bot is running! Send a message on Telegram.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
