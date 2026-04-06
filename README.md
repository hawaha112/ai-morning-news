# AI Morning Briefing

Every day at 7:00 AM, this system automatically fetches AI news from 19+ RSS sources, analyzes each article with an LLM, and generates a curated HTML briefing page.

**Live**: https://hawaha112.github.io/ai-morning-briefing/

## How It Works

```
RSS feeds (19 sources) -> Article extraction -> LLM analysis -> HTML generation -> GitHub Pages
```

1. Fetches RSS from official blogs (OpenAI, Anthropic, Google AI, DeepMind), media (The Verge, TechCrunch), academic (ArXiv), and more
2. Extracts full article text with multi-strategy content extraction
3. LLM analyzes each article: AI relevance filtering, importance scoring, key points extraction
4. Generates a responsive dark-themed HTML page with search and category filters
5. Deploys to GitHub Pages and sends a Telegram notification

## Quick Start

```bash
# Run manually
python3 fetch_news.py

# Run without LLM analysis (faster, no API needed)
python3 fetch_news.py --no-llm

# Run and open in browser
python3 fetch_news.py --open
```

## Configuration

Edit `config.json` to customize:
- **LLM settings**: API endpoint, model, auth (supports any OpenAI-compatible API)
- **RSS sources**: Add/remove feeds with name, URL, category, authority weight
- **Settings**: Max items per source, article age limit, output path

## Automated Daily Run

Uses macOS launchd for scheduling:

```bash
# Install the launchd job
bash install_launchd.sh
```

## Sensitive Config

Telegram tokens and other secrets are stored externally:

```bash
# Create config file
mkdir -p ~/.config/ai-briefing
cat > ~/.config/ai-briefing/.env << 'EOF'
TG_BOT_TOKEN="your_bot_token"
TG_CHAT_ID="your_chat_id"
BRIEFING_URL="https://your-username.github.io/ai-morning-briefing/"
EOF
```

## Requirements

- Python 3.9+ (standard library only, no pip dependencies)
- macOS (for launchd scheduling; the Python scripts work on any OS)
- OpenAI-compatible LLM API (optional, use `--no-llm` to skip)
