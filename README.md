# aiashley — Global News Digest

A daily international news digest. Pulls from 16 trusted sources across 4 regions, uses Claude to rank, summarize, and synthesize — generates a clean static HTML page each morning.

## How it works

1. **Fetch** — `feedparser` pulls recent items from 16 RSS feeds
2. **Extract** — `trafilatura` extracts full article body text (bypasses headline-only summaries)
3. **Analyze** — Claude ranks the top 5 stories, writes bullet summaries, detects cross-source framing differences, and writes a daily brief paragraph with citations
4. **Build** — Outputs a single `dist/index.html` in The Monograph dark mode aesthetic
5. **Deploy** — GitHub Actions runs this daily at 6AM UTC, commits the result, Vercel auto-deploys

## Setup

### 1. Clone and install locally

```bash
git clone <your-repo-url>
cd news-digest
pip install -r requirements.txt
```

### 2. Add your Anthropic API key

```bash
export ANTHROPIC_API_KEY=your_key_here
```

### 3. Run locally to test

```bash
python fetch_and_build.py
open dist/index.html
```

### 4. Deploy to Vercel

- Connect the repo to Vercel
- Set **Output Directory** to `dist`
- Set **Build Command** to (none / blank)
- Point your domain (e.g. `news.aiashley.io`) to the Vercel deployment

### 5. Add GitHub Secret for automation

In your GitHub repo → Settings → Secrets → Actions:
- Add `ANTHROPIC_API_KEY` with your key

The GitHub Action in `.github/workflows/daily-digest.yml` will then run every morning at 6AM UTC, build a fresh digest, commit `dist/index.html`, and Vercel will auto-deploy.

### Adjust run time

Edit the cron in `.github/workflows/daily-digest.yml`:
```yaml
- cron: '0 6 * * *'   # 6AM UTC = 2AM ET / 8AM Paris
```

## Customization

- **Add/remove sources** — edit the `FEEDS` dict in `fetch_and_build.py`
- **Change story count** — edit `MAX_ARTICLES_PER_FEED` and the prompt
- **Adjust digest time** — change the cron schedule
- **Tweak the UI** — all styles are inline in `build_html()`, using CSS variables

## Cost

Each run makes ~1 Claude API call (Opus) with ~60 articles in context.
Estimated cost: **$0.05–0.15 per day** depending on article volume.
