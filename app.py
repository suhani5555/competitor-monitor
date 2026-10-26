import os, re, hashlib, sqlite3
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, render_template_string

DB = os.environ.get("DB_PATH", "monitor.db")
UA = {"User-Agent": "Mozilla/5.0 (compatible; CompetitorMonitor/1.0)"}
BLOG = re.compile(r"/(blog|post|posts|article|articles|resources|news|insights|learn|stories)/[^/]+", re.I)
SKIP = re.compile(r"/(page|tag|tags|category|categories|author)/", re.I)
MAX_NEW = 15  # newest unseen posts we fully enrich per run
app = Flask(__name__)


class FetchError(Exception):
    pass


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS posts(url TEXT PRIMARY KEY, domain TEXT, title TEXT,
        published TEXT, description TEXT, keyword TEXT, volume TEXT, first_seen TEXT);
    CREATE TABLE IF NOT EXISTS pricing(domain TEXT PRIMARY KEY, hash TEXT, checked TEXT);""")
    return c


def get(url):
    try:
        r = requests.get(url, headers=UA, timeout=10)
    except requests.RequestException as e:
        raise FetchError(f"Could not reach {url} ({type(e).__name__})")
    if r.status_code in (401, 403, 429):
        raise FetchError(f"The site blocked our bot (HTTP {r.status_code})")
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} for {url}")
    return r


def norm(d):
    d = d.strip().lower()
    if not d.startswith("http"):
        d = "https://" + d
    p = urlparse(d)
    return f"{p.scheme}://{p.netloc}"


def is_post(u):
    path = urlparse(u).path
    return bool(BLOG.search(path)) and not SKIP.search(path)


def sitemap_urls(base):
    found, queue, seen = [], [], set()
    try:
        for line in get(base + "/robots.txt").text.splitlines():
            if line.lower().startswith("sitemap:"):
                queue.append(line.split(":", 1)[1].strip())
    except FetchError:
        pass
    queue += [base + "/sitemap.xml", base + "/sitemap_index.xml"]
    while queue and len(seen) < 15:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            soup = BeautifulSoup(get(sm).content, "xml")
        except FetchError:
            continue
        queue += [s.loc.text.strip() for s in soup.find_all("sitemap") if s.loc]
        queue.sort(key=lambda s: not re.search("post|blog|article|news", s))
        for u in soup.find_all("url"):
            if u.loc:
                found.append((u.loc.text.strip(), u.lastmod.text.strip() if u.lastmod else ""))
    return found


def crawl_links(base):
    out, host = [], urlparse(base).netloc.replace("www.", "")
    for p in ["", "/blog", "/resources", "/news", "/articles", "/insights"]:
        try:
            soup = BeautifulSoup(get(base + p).text, "html.parser")
        except FetchError:
            continue
        for a in soup.find_all("a", href=True):
            u = urljoin(base + p + "/", a["href"]).split("#")[0].split("?")[0]
            if urlparse(u).netloc.replace("www.", "") == host:
                out.append((u, ""))
    return out


def discover(base):
    get(base)  # raises FetchError if unreachable / blocked
    cands = sitemap_urls(base) + crawl_links(base)
    d = {}
    for u, lm in cands:
        if is_post(u):
            d.setdefault(u.rstrip("/"), lm)
    return sorted(d.items(), key=lambda x: x[1], reverse=True)  # newest lastmod first


def short_kw(t):
    t = re.sub(r"^(how to|how do you|how can|what is|what are|why|the)\s+", "", t.lower()).strip(" ?:,.")
    return " ".join(t.split()[:6])


def parse(url):
    try:
        soup = BeautifulSoup(get(url).text, "html.parser")
    except FetchError:
        return None

    def meta(*names):
        for n in names:
            t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if t and t.get("content"):
                return t["content"].strip()
        return ""

    title = meta("og:title") or (soup.title.text.strip() if soup.title else "")
    date = meta("article:published_time", "datePublished", "date")
    if not date:
        for s in soup.find_all("script", type="application/ld+json"):
            m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', s.string or "")
            if m:
                date = m.group(1)
                break
    if not date:
        t = soup.find("time", attrs={"datetime": True})
        date = t["datetime"] if t else ""
    kw = meta("keywords").split(",")[0].strip()
    if not kw:  # fallback: use the title minus the site-name suffix
        kw = short_kw(re.split(r"\s[|–—-]\s", title)[0])
    return dict(url=url, title=title, published=date[:10], description=meta("og:description", "description"),
                keyword=kw[:80])


def volume(kw):
    login, pw = os.environ.get("DFS_LOGIN"), os.environ.get("DFS_PASSWORD")
    if not login or not pw:
        return "n/a (no API key set)"
    try:
        r = requests.post("https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live",
                          auth=(login, pw), timeout=30,
                          json=[{"keywords": [kw], "location_code": 2840, "language_code": "en"}])
        if r.status_code in (401, 403):
            return "n/a (invalid API credentials)"
        task = r.json()["tasks"][0]
        if task.get("status_code") != 20000:
            return f"n/a ({task.get('status_message')})"
        res = (task.get("result") or [{}])[0]
        v = res.get("search_volume")
        return v if v is not None else "n/a (no data)"
    except Exception:
        return "n/a (API error)"


def check_pricing(base, c):
    for p in ("/pricing", "/plans", "/price"):
        try:
            txt = BeautifulSoup(get(base + p).text, "html.parser").get_text(" ", strip=True)
        except FetchError:
            continue
        h = hashlib.md5(txt.encode()).hexdigest()
        row = c.execute("SELECT hash FROM pricing WHERE domain=?", (base,)).fetchone()
        c.execute("INSERT OR REPLACE INTO pricing VALUES(?,?,datetime('now'))", (base, h))
        if row is None:
            return "Pricing page found; baseline saved."
        return "PRICING PAGE CHANGED since last check." if row["hash"] != h else "Pricing unchanged."
    return "No pricing page found."


def slack(base, posts, pricing):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return "Slack not configured (set SLACK_WEBHOOK_URL)"
    if not posts and "CHANGED" not in pricing:
        return "Nothing new, no alert sent"
    host = urlparse(base).netloc
    lines = [f"*{host}* published {len(posts)} new post(s)"]
    lines += [f"• <{p['url']}|{p['title'] or p['url']}> | keyword: {p['keyword']} | volume: {p['volume']}" for p in posts]
    if "CHANGED" in pricing:
        lines.append(f":warning: {pricing}")
    try:
        r = requests.post(url, json={"text": "\n".join(lines)}, timeout=10)
        return "Slack alert sent" if r.ok else f"Slack error (HTTP {r.status_code})"
    except requests.RequestException:
        return "Slack error (could not connect)"


def run(domain):
    base = norm(domain)
    c = db()
    try:
        cands = discover(base)
    except FetchError as e:
        return dict(domain=base, error=str(e))
    if not cands:
        return dict(domain=base, error="No blog posts found. The site may have no blog, or loads posts with JavaScript.")
    known = {r[0] for r in c.execute("SELECT url FROM posts WHERE domain=?", (base,))}
    fresh = [u for u, _ in cands if u not in known]
    top, rest = fresh[:MAX_NEW], fresh[MAX_NEW:]
    with ThreadPoolExecutor(8) as ex:
        posts = [p for p in ex.map(parse, top) if p]
    dates = dict(cands)
    for p in posts:
        if not p["published"]:
            p["published"] = dates.get(p["url"], "")[:10]
        p["volume"] = volume(p["keyword"])
        c.execute("INSERT OR IGNORE INTO posts VALUES(?,?,?,?,?,?,?,datetime('now'))",
                  (p["url"], base, p["title"], p["published"], p["description"], p["keyword"], str(p["volume"])))
    for u in rest:  # remember older URLs so they are never reported as "new" later
        c.execute("INSERT OR IGNORE INTO posts VALUES(?,?,?,?,?,?,?,datetime('now'))",
                  (u, base, "(baseline)", "", "", "", "", ))
    pricing = check_pricing(base, c)
    status = slack(base, posts, pricing)
    c.commit()
    return dict(domain=base, posts=posts, total=len(cands), pricing=pricing, slack=status)


PAGE = """<!doctype html><title>Competitor Monitor</title>
<style>body{font-family:system-ui;max-width:820px;margin:40px auto;padding:0 16px}
input{padding:8px;width:60%}button{padding:8px 14px}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}
.err{color:#b00020}table{border-collapse:collapse;width:100%}</style>
<h1>Competitor Monitor</h1>
<form method=post><input name=domain placeholder="competitor.com" value="{{ q or '' }}" required>
<button>Check now</button></form>
{% if r %}<h2>{{ r.domain }}</h2>
{% if r.error %}<p class=err>{{ r.error }}</p>
{% else %}<p>{{ r.total }} blog URLs found &middot; <b>{{ r.posts|length }} new</b> &middot; {{ r.pricing }} &middot; {{ r.slack }}</p>
{% if r.posts %}<table><tr><th>Title</th><th>Date</th><th>Keyword</th><th>Volume</th></tr>
{% for p in r.posts %}<tr><td><a href="{{ p.url }}">{{ p.title or p.url }}</a><br><small>{{ p.description }}</small></td>
<td>{{ p.published }}</td><td>{{ p.keyword }}</td><td>{{ p.volume }}</td></tr>{% endfor %}</table>
{% else %}<p>No new posts since the last check.</p>{% endif %}{% endif %}{% endif %}"""


@app.route("/", methods=["GET", "POST"])
def index():
    q = request.values.get("domain")
    return render_template_string(PAGE, q=q, r=run(q) if q else None)


if __name__ == "__main__":
    app.run(debug=True)
