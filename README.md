# Nocu 🔭 (Binoculars for Production)

**Streamline observability by connecting production logs to your actual codebase.**

Ask natural language questions about your production services via Telegram and get
code-level root cause analysis — not just log summaries.

## What Nocu Does

```
You (Telegram): "What errors have been happening in Pehchaan in the last 24 hours?"

Nocu:
  1. Classifies your question (Gemini Flash)
  2. Queries New Relic for relevant logs/metrics (NRQL via NerdGraph)
  3. Loads relevant code from your repository (AST-based indexer)
  4. Analyzes logs + code together (Gemini or Claude Code)
  5. Responds with root cause + code references on Telegram
```

## Architecture

```
┌──────────────┐
│  Telegram     │
│  (you ask)    │
└──────┬───────┘
       │
┌──────▼───────┐
│  Telegram Bot │  ← python-telegram-bot
│  (receiver)   │
└──────┬───────┘
       │
┌──────▼───────────────────────────────────┐
│  Orchestrator (core/orchestrator.py)      │
│                                           │
│  1. Classify query      → Gemini Flash    │
│  2. Fetch logs/metrics  → New Relic API   │
│  3. Load code context   → Code Indexer    │
│  4. Analyze             → Gemini / Claude │
│  5. Format response     → Telegram        │
└──────────────────────────────────────────┘
```

## Setup

### 1. Prerequisites
- Python 3.10+
- Telegram Bot Token (from @BotFather)
- New Relic User API Key + Account ID
- Google Gemini API Key
- (Optional) Claude Code CLI installed + Pro subscription

### 2. Install
```bash
cd nocu
pip install -r requirements.txt
```

### 3. Configure
```bash
cp config/settings.example.yaml config/settings.yaml
# Edit with your API keys and service mappings
```

### 4. Index Your Repos
```bash
python -m indexer.scanner --repo /path/to/your/service --name pehchaan
```

### 5. Run
```bash
python -m bot.main
```

## Project Structure

```
nocu/
├── bot/                  # Telegram bot interface
│   └── main.py
├── core/                 # Orchestration logic
│   ├── orchestrator.py   # Main pipeline
│   ├── classifier.py     # Gemini-based query classification
│   └── formatter.py      # Response formatting for Telegram
├── indexer/              # Code repository indexer
│   ├── scanner.py        # AST-based Python code analyzer
│   └── models.py         # Data models for code index
├── fetchers/             # Data source connectors
│   └── newrelic.py       # New Relic NerdGraph/NRQL client
├── analyzers/            # LLM analysis engines
│   ├── gemini.py         # Gemini-based analysis
│   └── claude.py         # Claude Code CLI wrapper
├── config/
│   ├── settings.example.yaml
│   └── services.yaml     # service_name → repo path mapping
├── templates/            # Prompt templates
│   ├── classify.txt
│   ├── error_analysis.txt
│   └── deep_rca.txt
├── requirements.txt
└── README.md
```

## Supported Query Types

| Type | Example | Analyzer |
|------|---------|----------|
| Error Analysis | "What errors in Pehchaan last 24h?" | Gemini |
| Memory/Resource | "Why is auth-service using so much memory?" | Claude Code |
| Performance | "How has order-service performed this week?" | Gemini |
| Latency | "What's causing slow responses in gateway?" | Claude Code |

## License

Private — Internal tool
