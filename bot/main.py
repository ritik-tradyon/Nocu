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

# Logging
logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("nocu.bot")


# ── Global orchestrator (initialized on startup) ──
orchestrator: NocuOrchestrator = None


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
        "/index <service> — Re-index a service's code"
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

    # Check indexes
    indexed = len(orchestrator.service_indexes)
    total = len(orchestrator.config.get("services", {}))
    checks.append(f"📂 Code indexes: {indexed}/{total} services indexed")

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
    global orchestrator

    # Find config file
    config_path = os.environ.get("NOCU_CONFIG", "config/settings.yaml")
    if not os.path.exists(config_path):
        print(f"Config not found at {config_path}")
        print("Copy config/settings.example.yaml → config/settings.yaml and fill in your keys.")
        sys.exit(1)

    # Initialize orchestrator
    print("[nocu] Initializing...")
    orchestrator = NocuOrchestrator(config_path)

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("[nocu] 🔭 Bot is running! Send a message on Telegram.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
