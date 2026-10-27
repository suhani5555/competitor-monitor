# Competitor Monitor

A small web tool: enter a competitor's domain, and it finds their blog posts, detects which are new since the last check, looks up keyword search volume, and sends a Slack alert.

**Live demo:** https://competitor-monitor-ba6i.onrender.com

## How it works
1. **Scraper (no scraping API):** reads `robots.txt` and sitemaps, then crawls the homepage and common listing pages (`/blog`, `/resources`, `/news`...) with `requests` + BeautifulSoup.
2. **New-content detection:** extracts URL, title, publish date (meta tags, JSON-LD, `<time>`, or sitemap `lastmod`) and meta description. Everything is stored in SQLite, so a second run only reports URLs not seen before. On a first run, older posts are saved as a baseline and only the newest 15 are enriched and alerted.
3. **Keyword + volume:** target keyword = meta keywords tag, else a cleaned version of the title. Volume comes from the DataForSEO Google Ads search volume API, with all of a run's keywords sent in one batched request to keep cost low.

   > **Volume data note (edit before submitting):** DELETE ONE OF THESE TWO LINES. (A) Volumes in the demo are real, from the live DataForSEO API. (B) My free trial credit ran out, so the demo uses the DataForSEO *sandbox* (set `DFS_SANDBOX=1`), which returns dummy numbers; the live integration is the same code without that flag.
4. **Alert:** posts a summary (titles, URLs, keyword, volume) to a Slack incoming webhook.
5. **Pricing tracking:** hashes the `/pricing` page and flags changes.

## Failure handling
- Site has no blog: clear "no posts found" message.
- Site blocks bots (403/429) or is unreachable: clear error message.
- Invalid or missing API key: posts still appear, volume shows the error.
- Slack not configured or failing: reported on the page, app keeps working.

## Run locally
```
pip install -r requirements.txt
set DFS_LOGIN=your_api_login
set DFS_PASSWORD=your_api_password
set SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
python app.py
```
(Use `export` instead of `set` on Mac/Linux.) Then open http://127.0.0.1:5000.

## Known limitations
- Sites that load posts only with JavaScript, or that block bots, may return nothing.
- Keywords are guessed from titles, so some will have no volume data.
- Pricing detection hashes page text, so dynamic content can cause false alerts.
- On free hosting, the SQLite file may reset when the server restarts or sleeps (15 min idle), so a repeat run can show posts as new again. A persistent disk or Postgres would fix this.
- The free 512 MB instance can struggle with very large sites (e.g. hubspot.com has ~590 blog URLs); only the newest 15 new posts per run are fetched and enriched.
- Weekly schedule: point a free scheduler (e.g. cron-job.org) at `/?domain=competitor.com`.

## Stack
Python, Flask, SQLite, BeautifulSoup, DataForSEO API, Slack webhooks.
