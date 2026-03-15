#!/usr/bin/env python3
"""
aiashley — Global News Digest
Daily pipeline: fetch RSS → extract articles → Claude summarization → static HTML
"""

import feedparser
import trafilatura
import anthropic
import json
import os
import re
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ─────────────────────────────────────────────
# RSS FEED SOURCES
# ─────────────────────────────────────────────
FEEDS = {
    "Wire Services": [
        {"name": "Reuters", "url": "https://feeds.reuters.com/reuters/worldNews", "lean": "Center"},
        {"name": "AP News", "url": "https://rsshub.app/apnews/topics/world-news", "lean": "Center"},
    ],
    "United Kingdom": [
        {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "lean": "Center"},
        {"name": "BBC Europe", "url": "https://feeds.bbci.co.uk/news/world/europe/rss.xml", "lean": "Center"},
        {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss", "lean": "Center-Left"},
        {"name": "Financial Times", "url": "https://www.ft.com/world?format=rss", "lean": "Center-Right"},
        {"name": "The Economist", "url": "https://www.economist.com/international/rss.xml", "lean": "Center-Right"},
    ],
    "France": [
        {"name": "Le Monde", "url": "https://www.lemonde.fr/en/international/rss_full.xml", "lean": "Center-Left"},
        {"name": "France 24", "url": "https://www.france24.com/en/rss", "lean": "Center"},
        {"name": "RFI English", "url": "https://www.rfi.fr/en/rss", "lean": "Center"},
    ],
    "Canada": [
        {"name": "CBC World", "url": "https://www.cbc.ca/cmlink/rss-world", "lean": "Center"},
        {"name": "Globe and Mail", "url": "https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/world/", "lean": "Center"},
    ],
    "Australia & NZ": [
        {"name": "ABC Australia", "url": "https://www.abc.net.au/news/feed/51120/rss.xml", "lean": "Center"},
        {"name": "RNZ", "url": "https://www.rnz.co.nz/rss/world.xml", "lean": "Center"},
    ],
    "Non-Western": [
        {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "lean": "Center-Left"},
        {"name": "The Hindu", "url": "https://www.thehindu.com/news/international/feeder/default.rss", "lean": "Center-Left"},
        {"name": "Nikkei Asia", "url": "https://asia.nikkei.com/rss/feed/nar", "lean": "Center"},
        {"name": "Channel NewsAsia", "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10", "lean": "Center"},
    ],
}

MAX_ARTICLES_PER_FEED = 5   # How many recent items to pull per feed
MAX_ARTICLES_TOTAL = 60     # Cap before sending to Claude
ARTICLE_CHAR_LIMIT = 3000   # Truncate body text sent to Claude


# ─────────────────────────────────────────────
# STEP 1: FETCH RSS ENTRIES
# ─────────────────────────────────────────────
def fetch_feed(source: dict, region: str) -> list[dict]:
    try:
        parsed = feedparser.parse(source["url"])
        items = []
        for entry in parsed.entries[:MAX_ARTICLES_PER_FEED]:
            items.append({
                "source": source["name"],
                "lean": source["lean"],
                "region": region,
                "title": entry.get("title", "").strip(),
                "url": entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": entry.get("summary", ""),
                "body": "",  # filled later
            })
        return items
    except Exception as e:
        print(f"  [WARN] Failed to fetch {source['name']}: {e}")
        return []


def fetch_all_feeds() -> list[dict]:
    print("📡 Fetching RSS feeds...")
    articles = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(fetch_feed, source, region): source["name"]
            for region, sources in FEEDS.items()
            for source in sources
        }
        for future in as_completed(futures):
            result = future.result()
            articles.extend(result)
            print(f"  ✓ {futures[future]} ({len(result)} items)")
    print(f"  → {len(articles)} total articles fetched\n")
    return articles[:MAX_ARTICLES_TOTAL]


# ─────────────────────────────────────────────
# STEP 2: EXTRACT ARTICLE BODY TEXT
# ─────────────────────────────────────────────
def extract_body(article: dict) -> dict:
    try:
        downloaded = trafilatura.fetch_url(article["url"])
        if downloaded:
            text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if text:
                article["body"] = text[:ARTICLE_CHAR_LIMIT]
    except Exception:
        pass  # Fall back to RSS summary
    if not article["body"]:
        # Strip HTML from summary as fallback
        article["body"] = re.sub(r"<[^>]+>", "", article.get("summary", ""))[:ARTICLE_CHAR_LIMIT]
    return article


def extract_all_bodies(articles: list[dict]) -> list[dict]:
    print("📄 Extracting article content...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(extract_body, a): i for i, a in enumerate(articles)}
        results = [None] * len(articles)
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()
    extracted = [r for r in results if r is not None]
    print(f"  → Content extracted for {sum(1 for a in extracted if a['body'])} / {len(extracted)} articles\n")
    return extracted


# ─────────────────────────────────────────────
# STEP 3: CLAUDE — RANK, SUMMARIZE, SYNTHESIZE
# ─────────────────────────────────────────────
def build_article_payload(articles: list[dict]) -> str:
    lines = []
    for i, a in enumerate(articles):
        lines.append(f"[{i}] SOURCE: {a['source']} ({a['region']}, {a['lean']})")
        lines.append(f"    TITLE: {a['title']}")
        lines.append(f"    URL: {a['url']}")
        lines.append(f"    BODY: {a['body'][:1500] if a['body'] else '(no body extracted)'}")
        lines.append("")
    return "\n".join(lines)


def run_claude_analysis(articles: list[dict]) -> dict:
    print("🤖 Running Claude analysis...")
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    payload = build_article_payload(articles)
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

    prompt = f"""You are an editorial AI for a global news digest. Today is {today}.

Below are up to {len(articles)} articles pulled from trusted international sources across multiple regions and editorial perspectives.

Your task:

1. IDENTIFY THE TOP 5 MOST SIGNIFICANT STORIES of the day. Prioritize stories with broad global impact, multi-source coverage, or that directly affect US-EU geopolitics, economics, or policy. Avoid redundancy — if 4 sources covered the same event, that counts as ONE story.

2. For each top story:
   - Write a HEADLINE (sharp, informative, not clickbait)
   - Write 3-4 BULLET POINTS summarizing what actually happened (facts only, no framing)
   - List which SOURCES covered it and note any meaningful differences in framing between them
   - Assign a CATEGORY tag from: [Geopolitics, Economics, Policy, Climate, Technology, Conflict, Society]

3. ADDITIONAL STORIES: List 5-10 more noteworthy stories as single-sentence summaries with source attribution.

4. DAILY BRIEF: Write a single paragraph (150-200 words) synthesizing the day's most important developments. Use inline citations like [Reuters], [BBC], [Le Monde] etc. Tone: clear, calm, analytical. Not sensationalist.

Return your response as a JSON object with this exact structure:
{{
  "date": "{today}",
  "daily_brief": "string",
  "top_stories": [
    {{
      "headline": "string",
      "category": "string",
      "bullets": ["string", "string", "string"],
      "sources": [{{"name": "string", "lean": "string", "framing_note": "string or null"}}]
    }}
  ],
  "additional_stories": [
    {{
      "headline": "string",
      "source": "string",
      "url": "string"
    }}
  ]
}}

Return ONLY the JSON object, no preamble, no markdown fences.

ARTICLES:
{payload}
"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    # Strip any accidental markdown fences
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    data = json.loads(raw)
    print(f"  ✓ Top stories: {len(data.get('top_stories', []))}")
    print(f"  ✓ Additional stories: {len(data.get('additional_stories', []))}\n")
    return data


# ─────────────────────────────────────────────
# STEP 4: ENRICH WITH ARTICLE URLs
# ─────────────────────────────────────────────
def enrich_with_urls(digest: dict, articles: list[dict]) -> dict:
    """Match top stories back to source URLs for linking."""
    url_map = {a["source"]: a["url"] for a in articles}
    for story in digest.get("top_stories", []):
        for source in story.get("sources", []):
            source["url"] = url_map.get(source["name"], "#")
    return digest


# ─────────────────────────────────────────────
# STEP 5: BUILD STATIC HTML
# ─────────────────────────────────────────────
CATEGORY_COLORS = {
    "Geopolitics": "#C17D3C",
    "Economics": "#4A9E8E",
    "Policy": "#7B6FB0",
    "Climate": "#5A9E5A",
    "Technology": "#4A7EB0",
    "Conflict": "#B05A5A",
    "Society": "#9E7B4A",
}

def build_html(digest: dict) -> str:
    date_str = digest.get("date", datetime.now(timezone.utc).strftime("%A, %B %d, %Y"))
    daily_brief = digest.get("daily_brief", "")
    top_stories = digest.get("top_stories", [])
    additional = digest.get("additional_stories", [])

    # Build top story cards
    story_cards = ""
    for i, story in enumerate(top_stories):
        cat = story.get("category", "World")
        cat_color = CATEGORY_COLORS.get(cat, "#C17D3C")
        bullets_html = "".join(f"<li>{b}</li>" for b in story.get("bullets", []))

        sources_html = ""
        for s in story.get("sources", []):
            framing = f'<span class="framing-note">{s["framing_note"]}</span>' if s.get("framing_note") else ""
            lean_class = s.get("lean", "Center").lower().replace("-", "").replace(" ", "")
            url = s.get("url", "#")
            sources_html += f'''
            <a href="{url}" target="_blank" rel="noopener" class="source-chip lean-{lean_class}">
                {s["name"]}{framing}
            </a>'''

        story_cards += f'''
        <article class="story-card" style="--cat-color: {cat_color}">
            <div class="story-meta">
                <span class="story-number">0{i+1}</span>
                <span class="category-tag" style="color: {cat_color}; border-color: {cat_color}40">{cat}</span>
            </div>
            <h2 class="story-headline">{story["headline"]}</h2>
            <ul class="story-bullets">{bullets_html}</ul>
            <div class="source-coverage">
                <span class="coverage-label">Coverage</span>
                <div class="source-chips">{sources_html}</div>
            </div>
        </article>'''

    # Build additional stories
    additional_html = ""
    for item in additional:
        url = item.get("url", "#")
        additional_html += f'''
        <li class="additional-item">
            <a href="{url}" target="_blank" rel="noopener">{item["headline"]}</a>
            <span class="additional-source">{item.get("source", "")}</span>
        </li>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>aiashley — Global Digest</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400&family=IBM+Plex+Mono:wght@300;400&family=IBM+Plex+Sans:wght@300;400;500&display=swap" rel="stylesheet">
    <style>
        /* ─── TOKENS ─── */
        :root {{
            --bg:          #0D0D0D;
            --bg-card:     #141414;
            --bg-surface:  #1A1A1A;
            --border:      #2A2A2A;
            --border-subtle: #1E1E1E;
            --text-primary:   #F0EDE8;
            --text-secondary: #8A8480;
            --text-tertiary:  #5A5550;
            --gold:        #C17D3C;
            --gold-dim:    #C17D3C30;
            --font-display: 'Playfair Display', Georgia, serif;
            --font-body:    'IBM Plex Sans', sans-serif;
            --font-mono:    'IBM Plex Mono', monospace;
        }}

        /* ─── RESET ─── */
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
        html {{ scroll-behavior: smooth; }}

        body {{
            background: var(--bg);
            color: var(--text-primary);
            font-family: var(--font-body);
            font-weight: 300;
            line-height: 1.7;
            min-height: 100vh;
        }}

        /* ─── GRAIN OVERLAY ─── */
        body::before {{
            content: '';
            position: fixed;
            inset: 0;
            background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.04'/%3E%3C/svg%3E");
            pointer-events: none;
            z-index: 0;
            opacity: 0.4;
        }}

        /* ─── LAYOUT ─── */
        .container {{
            max-width: 860px;
            margin: 0 auto;
            padding: 0 24px;
            position: relative;
            z-index: 1;
        }}

        /* ─── HEADER ─── */
        header {{
            padding: 64px 0 48px;
            border-bottom: 1px solid var(--border);
        }}

        .masthead {{
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            margin-bottom: 32px;
        }}

        .wordmark {{
            font-family: var(--font-mono);
            font-weight: 300;
            font-size: 13px;
            letter-spacing: 0.2em;
            text-transform: uppercase;
            color: var(--gold);
        }}

        .edition-info {{
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-tertiary);
            letter-spacing: 0.08em;
        }}

        .header-title {{
            font-family: var(--font-display);
            font-size: clamp(36px, 6vw, 58px);
            font-weight: 700;
            line-height: 1.1;
            letter-spacing: -0.02em;
            color: var(--text-primary);
            margin-bottom: 8px;
        }}

        .header-subtitle {{
            font-family: var(--font-mono);
            font-size: 12px;
            color: var(--text-tertiary);
            letter-spacing: 0.12em;
            text-transform: uppercase;
        }}

        /* ─── DAILY BRIEF ─── */
        .brief-section {{
            padding: 40px 0;
            border-bottom: 1px solid var(--border-subtle);
        }}

        .section-label {{
            font-family: var(--font-mono);
            font-size: 10px;
            letter-spacing: 0.25em;
            text-transform: uppercase;
            color: var(--gold);
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .section-label::after {{
            content: '';
            flex: 1;
            height: 1px;
            background: var(--border);
        }}

        .daily-brief-text {{
            font-family: var(--font-display);
            font-size: 17px;
            font-weight: 400;
            line-height: 1.75;
            color: var(--text-primary);
            max-width: 720px;
        }}

        .daily-brief-text cite {{
            font-style: normal;
            color: var(--gold);
            font-size: 0.85em;
            font-family: var(--font-mono);
            vertical-align: super;
            font-size: 10px;
            letter-spacing: 0.05em;
        }}

        /* ─── TOP STORIES ─── */
        .stories-section {{
            padding: 40px 0;
        }}

        .story-card {{
            padding: 32px 0;
            border-bottom: 1px solid var(--border-subtle);
            position: relative;
        }}

        .story-card::before {{
            content: '';
            position: absolute;
            left: -24px;
            top: 0;
            bottom: 0;
            width: 2px;
            background: var(--cat-color, var(--gold));
            opacity: 0;
            transition: opacity 0.2s;
        }}

        .story-card:hover::before {{
            opacity: 1;
        }}

        .story-meta {{
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 12px;
        }}

        .story-number {{
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-tertiary);
            letter-spacing: 0.08em;
        }}

        .category-tag {{
            font-family: var(--font-mono);
            font-size: 10px;
            letter-spacing: 0.18em;
            text-transform: uppercase;
            padding: 3px 8px;
            border: 1px solid;
            border-radius: 2px;
        }}

        .story-headline {{
            font-family: var(--font-display);
            font-size: clamp(20px, 3vw, 26px);
            font-weight: 600;
            line-height: 1.3;
            color: var(--text-primary);
            margin-bottom: 16px;
            letter-spacing: -0.01em;
        }}

        .story-bullets {{
            list-style: none;
            margin-bottom: 20px;
        }}

        .story-bullets li {{
            font-size: 14px;
            color: var(--text-secondary);
            line-height: 1.65;
            padding: 4px 0 4px 16px;
            position: relative;
            font-weight: 300;
        }}

        .story-bullets li::before {{
            content: '—';
            position: absolute;
            left: 0;
            color: var(--text-tertiary);
            font-size: 12px;
        }}

        .source-coverage {{
            display: flex;
            align-items: flex-start;
            gap: 12px;
            flex-wrap: wrap;
        }}

        .coverage-label {{
            font-family: var(--font-mono);
            font-size: 10px;
            letter-spacing: 0.15em;
            text-transform: uppercase;
            color: var(--text-tertiary);
            padding-top: 5px;
            white-space: nowrap;
        }}

        .source-chips {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}

        .source-chip {{
            font-family: var(--font-mono);
            font-size: 11px;
            padding: 4px 10px;
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 2px;
            color: var(--text-secondary);
            text-decoration: none;
            transition: all 0.15s;
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}

        .source-chip:hover {{
            border-color: var(--gold);
            color: var(--text-primary);
            background: var(--gold-dim);
        }}

        .framing-note {{
            font-size: 9px;
            color: var(--text-tertiary);
            letter-spacing: 0.05em;
            font-style: italic;
        }}

        /* ─── ADDITIONAL STORIES ─── */
        .additional-section {{
            padding: 40px 0 80px;
        }}

        .additional-list {{
            list-style: none;
        }}

        .additional-item {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            gap: 16px;
            padding: 12px 0;
            border-bottom: 1px solid var(--border-subtle);
        }}

        .additional-item a {{
            font-size: 14px;
            color: var(--text-secondary);
            text-decoration: none;
            line-height: 1.5;
            transition: color 0.15s;
            flex: 1;
        }}

        .additional-item a:hover {{
            color: var(--text-primary);
        }}

        .additional-source {{
            font-family: var(--font-mono);
            font-size: 10px;
            color: var(--text-tertiary);
            letter-spacing: 0.08em;
            white-space: nowrap;
        }}

        /* ─── FOOTER ─── */
        footer {{
            border-top: 1px solid var(--border);
            padding: 32px 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .footer-brand {{
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-tertiary);
            letter-spacing: 0.15em;
        }}

        .footer-brand span {{
            color: var(--gold);
        }}

        .footer-meta {{
            font-family: var(--font-mono);
            font-size: 10px;
            color: var(--text-tertiary);
            letter-spacing: 0.08em;
        }}

        /* ─── RESPONSIVE ─── */
        @media (max-width: 600px) {{
            .masthead {{ flex-direction: column; gap: 8px; }}
            .additional-item {{ flex-direction: column; gap: 4px; }}
            footer {{ flex-direction: column; gap: 12px; text-align: center; }}
        }}

        /* ─── FADE IN ─── */
        @keyframes fadeUp {{
            from {{ opacity: 0; transform: translateY(12px); }}
            to   {{ opacity: 1; transform: translateY(0); }}
        }}

        .story-card, .brief-section, .additional-section {{
            animation: fadeUp 0.4s ease both;
        }}

        .story-card:nth-child(1) {{ animation-delay: 0.05s; }}
        .story-card:nth-child(2) {{ animation-delay: 0.10s; }}
        .story-card:nth-child(3) {{ animation-delay: 0.15s; }}
        .story-card:nth-child(4) {{ animation-delay: 0.20s; }}
        .story-card:nth-child(5) {{ animation-delay: 0.25s; }}
    </style>
</head>
<body>
    <div class="container">

        <header>
            <div class="masthead">
                <span class="wordmark">aiashley — global digest</span>
                <span class="edition-info">Generated {datetime.now(timezone.utc).strftime("%H:%M UTC")}</span>
            </div>
            <h1 class="header-title">{date_str}</h1>
            <p class="header-subtitle">International News · 16 Sources · 4 Regions</p>
        </header>

        <section class="brief-section">
            <div class="section-label">Daily Brief</div>
            <p class="daily-brief-text">{daily_brief}</p>
        </section>

        <section class="stories-section">
            <div class="section-label">Top Stories</div>
            {story_cards}
        </section>

        <section class="additional-section">
            <div class="section-label">Also Today</div>
            <ul class="additional-list">
                {additional_html}
            </ul>
        </section>

        <footer>
            <span class="footer-brand"><span>aiashley</span> · global digest</span>
            <span class="footer-meta">Built with Reuters · BBC · Le Monde · Al Jazeera + 12 more</span>
        </footer>

    </div>
</body>
</html>'''


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print("\n╔══════════════════════════════════════╗")
    print("║  aiashley — Global News Digest       ║")
    print(f"║  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}                    ║")
    print("╚══════════════════════════════════════╝\n")

    # 1. Fetch
    articles = fetch_all_feeds()

    # 2. Extract
    articles = extract_all_bodies(articles)

    # 3. Analyze
    digest = run_claude_analysis(articles)

    # 4. Enrich
    digest = enrich_with_urls(digest, articles)

    # 5. Build HTML
    print("🎨 Building HTML...")
    html = build_html(digest)

    # 6. Write output
    out_dir = Path("dist")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")

    # Also save the raw digest JSON for debugging
    json_path = out_dir / "digest.json"
    json_path.write_text(json.dumps(digest, indent=2), encoding="utf-8")

    print(f"  ✓ Written to {out_path}")
    print(f"  ✓ Digest JSON at {json_path}")
    print("\n✅ Done.\n")


if __name__ == "__main__":
    main()
