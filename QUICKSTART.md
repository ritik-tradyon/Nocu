# Nocu Quickstart — Getting It Running

## Step 0: Prerequisites Check

You need these before starting:
- [ ] Python 3.10+ (you already have this)
- [ ] Telegram Bot Token (from @BotFather)
- [ ] New Relic User API Key (not license key)
- [ ] New Relic Account ID
- [ ] Google Gemini API Key (free tier: https://aistudio.google.com/apikey)
- [ ] Claude Code CLI installed + authenticated (for deep RCA)

## Step 1: Get Your API Keys

### Telegram Bot Token
1. Open Telegram, search for @BotFather
2. Send /newbot
3. Name it "Nocu" or whatever you want
4. Copy the token (looks like: 123456:ABC-DEF...)
5. Also send a message to your new bot so it has a chat to respond to

### New Relic User API Key
1. Go to https://one.newrelic.com/api-keys
2. Click "Create a key"
3. Key type: "User"
4. Copy the key (starts with NRAK-)
5. Your Account ID is visible in the URL or under Account Settings

### Gemini API Key
1. Go to https://aistudio.google.com/apikey
2. Click "Create API Key"
3. Free tier gives you 15 RPM on Flash models — more than enough

### Claude Code (if not already set up)
```bash
npm install -g @anthropic-ai/claude-code
claude login
# Authenticate with your Pro subscription
```

## Step 2: Set Up Nocu

```bash
# Clone/copy the nocu project somewhere
cd /home/ritik/PycharmProjects
mkdir nocu && cd nocu
# (copy all the Nocu files here)

# Install dependencies
pip install -r requirements.txt --break-system-packages
# Or if that fails:
pip install -r requirements.txt --user

# Create your config
cp config/settings.example.yaml config/settings.yaml
```

## Step 3: Configure settings.yaml

Open config/settings.yaml and fill in:

```yaml
telegram:
  bot_token: "YOUR_TOKEN_FROM_BOTFATHER"
  allowed_chat_ids: []  # optional: restrict to your chat ID

newrelic:
  api_key: "NRAK-XXXXXXXXXXXXXXXXXXXX"
  account_id: "1234567"
  region: "US"

gemini:
  api_key: "YOUR_GEMINI_API_KEY"
  classifier_model: "gemini-2.0-flash"
  analyzer_model: "gemini-2.5-flash"
```

### CRITICAL: Update New Relic App Names

For each service, you need the EXACT app name as it appears in New Relic.
Find these in New Relic → APM & Services → look at each service name.

```yaml
services:
  pehchaan:
    repo_path: "/home/ritik/PycharmProjects/pehchaan"
    newrelic_app_name: "EXACT_NAME_FROM_NEWRELIC"  # <-- THIS MUST MATCH
```

### Configure Deepmap Integration

Update the Obsidian vault path to where deepmap outputs:

```yaml
code_context:
  deepmap:
    enabled: true
    output_dir: "/home/ritik/Downloads/ObsidianVaults"
    # Adjust the pattern based on how deepmap names its output
    # Check your vault: ls /home/ritik/Downloads/ObsidianVaults/
    # Look for files like: pehchaan-deep/00-FUNCTION-MAP.md
    file_pattern: "{service_name}-deep/00-FUNCTION-MAP.md"
```

## Step 4: Verify Deepmap Output Exists

```bash
# Check if deepmap has been run for your services
ls /home/ritik/Downloads/ObsidianVaults/*/00-FUNCTION-MAP.md
ls /home/ritik/Downloads/ObsidianVaults/*-deep/00-FUNCTION-MAP.md

# If not, run deepmap first:
cd /path/to/deepmap
python deepmap.py scan-all \
  --repos-dir /home/ritik/PycharmProjects \
  --vaults-dir /home/ritik/Downloads/ObsidianVaults/deep
```

## Step 5: Test Components Individually

Before running the full bot, verify each component:

```bash
cd /home/ritik/PycharmProjects/nocu

# Test 1: Check New Relic connection
python3 -c "
from fetchers.newrelic import NewRelicFetcher
f = NewRelicFetcher(
    api_key='YOUR_NRAK_KEY',
    account_id='YOUR_ACCOUNT_ID'
)
result = f.execute_nrql('SELECT count(*) FROM Transaction SINCE 1 hour ago')
print('NR connection:', 'OK' if not result.error else result.error)
print('Result:', result.results)
"

# Test 2: Check Gemini
python3 -c "
from google import genai
client = genai.Client(api_key='YOUR_GEMINI_KEY')
r = client.models.generate_content(
    model='gemini-2.0-flash',
    contents='Say hello in one word'
)
print('Gemini:', r.text)
"

# Test 3: Check code context loading
python3 -c "
from core.context_loader import CodeContextLoader
loader = CodeContextLoader({
    'code_context': {
        'deepmap': {
            'enabled': True,
            'output_dir': '/home/ritik/Downloads/ObsidianVaults',
            'file_pattern': '{service_name}-deep/00-FUNCTION-MAP.md',
        },
        'servicemap': {'enabled': False},
        'scanner': {
            'enabled': True,
            'index_dir': '.nocu_index',
        },
    },
    'services': {
        'pehchaan': {
            'repo_path': '/home/ritik/PycharmProjects/pehchaan',
            'framework': 'fastapi',
        }
    }
})
ctx = loader.load_context('pehchaan', ['auth', 'login'])
print(f'Sources: {ctx.sources_used}')
print(f'Context length: {len(ctx.to_llm_context())} chars')
print(ctx.to_llm_context()[:500])
"

# Test 4: Check Claude Code (optional)
python3 -c "
from analyzers.claude import ClaudeAnalyzer
c = ClaudeAnalyzer()
print('Claude Code available:', c.is_available())
"
```

## Step 6: Run the Bot

```bash
cd /home/ritik/PycharmProjects/nocu
python -m bot.main
```

You should see:
```
[nocu] Initializing...
[nocu] Code context sources: deepmap, scanner (fallback)
[nocu] 🔭 Bot is running! Send a message on Telegram.
```

## Step 7: Test It

Open Telegram, find your bot, and try:

1. `/start` — should show help + configured services
2. `/status` — checks all components
3. `/services` — lists services and index status
4. `What errors happened in pehchaan in the last 24 hours?`
5. `How is odin performing?`

## Troubleshooting

### "No code context available"
→ Deepmap hasn't been run, or the file_pattern in settings.yaml doesn't match.
   Run: `ls /home/ritik/Downloads/ObsidianVaults/` and check the actual directory names.
   The scanner will auto-run as a fallback.

### New Relic returns empty results
→ Check the newrelic_app_name matches EXACTLY what's in New Relic.
   Go to New Relic → APM → copy the exact service name.

### Gemini rate limit errors
→ Free tier is 15 RPM. If you're hitting it, wait 60 seconds.
   Or add a paid Gemini API key (very cheap).

### Claude Code timeout
→ Increase timeout_seconds in settings.yaml.
→ Or check: `claude --version` and `claude status`.

### pip install fails
→ Use: `pip install -r requirements.txt --break-system-packages`
→ Or: `pip install -r requirements.txt --user`
   (Known constraint on your machine — cascading fallback pattern)

### Bot doesn't respond
→ Check allowed_chat_ids in settings.yaml — if set, your chat ID must be listed.
→ Send `/start` first to initialize the chat.

## Running as Background Service

Once everything works, run it persistently:

```bash
# Simple: use nohup
nohup python -m bot.main > nocu.log 2>&1 &

# Or use systemd (create /etc/systemd/system/nocu.service):
# [Unit]
# Description=Nocu Observability Bot
# After=network.target
#
# [Service]
# Type=simple
# User=ritik
# WorkingDirectory=/home/ritik/PycharmProjects/nocu
# ExecStart=/usr/bin/python3 -m bot.main
# Restart=on-failure
#
# [Install]
# WantedBy=multi-user.target
```
