"""
Topic Radar v2

Luong chay:
  1. Tin tuc: Google Trends + Google News + tin khoa hoc + Hacker News
  2. Tu khoa: bo tu khoa goc (config) + Gemini rut them tu khoa moi tu tin tuc
  3. Nhanh co ban (mien phi):
       - YouTube: tim video theo tu khoa -> cham outlier (views / subscriber)
         + quet cac kenh "tu hoc" (kenh nao outlier >= 2 lan thi tu theo doi)
       - Reddit: tim toan Reddit theo tu khoa
       - Goi y tim kiem YouTube/Google (nguoi ta dang go tim gi)
  4. Nhanh vidIQ (goi 1): hoi vidIQ chu de/xu huong dang len trong ngach
  5. Gemini gop 2 nhanh -> top 5
  6. vidIQ kiem tra lan cuoi (goi 2): cham search volume / canh tranh cho ca 5 chu de
  7. Gui Telegram

Chay:  python radar.py            (chay that)
       python radar.py --dry-run  (in ra man hinh, khong gui Telegram, khong luu lich su)

Repo public -> log GitHub Actions ai cung xem duoc: script CHI in so luong.
"""
import base64
import calendar
import hashlib
import html
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import yaml
from cryptography.fernet import Fernet, InvalidToken

import vidiq

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "state.enc")
LOCAL_CONFIG = os.path.join(ROOT, "RADAR_CONFIG.yaml")
UA = "TopicRadar/2.0 (personal research script)"
NOW = datetime.now(timezone.utc)
DRY_RUN = "--dry-run" in sys.argv
HEALTH = {}  # ten nguon -> ghi chu (chi gui vao Telegram, khong in log)


RUN_LOG_FILE = os.path.join(ROOT, "run_log.txt")
RUNLOG = []  # nhat ky moi lan chay -> run_log.txt (repo public: CHI so lieu, khong tu khoa/chu de)
T0 = time.time()


def log(msg):
    line = f"[{time.time() - T0:5.0f}s] {msg}"
    RUNLOG.append(line)
    print(f"[radar] {line}", flush=True)


def write_run_log(status):
    if DRY_RUN:
        return
    local = NOW + timedelta(hours=7)
    health = " · ".join(f"{k}: {v}" for k, v in HEALTH.items())
    health = re.sub(r"@\S+", "@***", health)  # an handle kenh
    block = [f"=== {local:%Y-%m-%d %H:%M} (gio VN) · {os.environ.get('GITHUB_EVENT_NAME', 'local')} · {status} ==="]
    block += RUNLOG + ([f"nguon: {health}"] if health else []) + [""]
    old = []
    if os.path.exists(RUN_LOG_FILE):
        with open(RUN_LOG_FILE, encoding="utf-8") as f:
            old = f.read().splitlines()
    lines = (old + block)[-600:]  # giu khoang 30 lan chay gan nhat
    with open(RUN_LOG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ================================================================ config / state
DEFAULT_CONFIG = {
    "report_title": "Topic Radar",
    "lookback_days": 4,
    "history_days": 14,
    "top_n": 5,
    "gemini_models": ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"],
    "channel_profile": "Faceless animated explainer channel, 25-30 min videos, US audience.",
    "own_channel": "",
    "seed_keywords": [],
    "dynamic_keywords": 10,
    "youtube": {
        "search_days": 14, "max_searches": 20, "per_search": 25,
        "min_seconds": 480, "min_views": 20000, "min_ratio_subs": 1.0,
        "learn_min_hits": 2, "learn_window_days": 60, "watch_max": 40,
        "watch_window_days": 21, "watch_min_ratio": 2.0, "channels": [],
    },
    "reddit": {"search_keywords": 10, "per_keyword": 5, "min_score": 100},
    "suggest": {"keywords": 10},
    "vidiq": {
        "enabled": True, "max_calls": 2,
        "niche": "",  # lay tu RADAR_CONFIG
        # goi 1: tu khoa dang tang tim kiem trong ngach (search growth)
        "discover": {"tool": "vidiq_keyword_research",
                     "arguments": {"mode": "rising", "niche": "{niche}", "period": "week", "language": "en", "limit": 60}},
        # goi 2: do search volume / canh tranh cho tu khoa cua chu de so 1 (+ tu khoa lien quan)
        "check": {"tool": "vidiq_keyword_research",
                  "arguments": {"mode": "research", "keyword": "{keyword}", "country": "US"}},
    },
    "google_news": [
        "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/headlines/section/topic/SCIENCE?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=economy+OR+inflation+OR+%22federal+reserve%22+when:4d&hl=en-US&gl=US&ceid=US:en",
    ],
    "google_trends_geo": "US",
    "science_feeds": [
        "https://www.sciencedaily.com/rss/top/science.xml",
        "https://phys.org/rss-feed/breaking/",
        "https://www.nasa.gov/news-release/feed/",
        "https://api.quantamagazine.org/feed/",
    ],
    "hacker_news_min_points": 250,
    "per_feed": 12,
}


def deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config():
    raw = os.environ.get("RADAR_CONFIG", "").strip()
    if not raw and os.path.exists(LOCAL_CONFIG):
        with open(LOCAL_CONFIG, encoding="utf-8") as f:
            raw = f.read()
    if not raw:
        log("WARNING: khong co RADAR_CONFIG, dung cau hinh mac dinh")
    return deep_merge(DEFAULT_CONFIG, yaml.safe_load(raw) if raw else {})


def fernet():
    key = os.environ.get("STATE_KEY", "")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())) if key else None


def load_state():
    empty = {"history": [], "channels": {}}
    f = fernet()
    if not f or not os.path.exists(STATE_FILE):
        return empty
    try:
        with open(STATE_FILE, "rb") as fh:
            return deep_merge(empty, json.loads(f.decrypt(fh.read())))
    except (InvalidToken, ValueError):
        log("WARNING: khong giai ma duoc state.enc (sai STATE_KEY?) -> bat dau lai")
        return empty


def save_state(state):
    f = fernet()
    if not f:
        log("WARNING: thieu STATE_KEY -> khong luu lich su")
        return
    with open(STATE_FILE, "wb") as fh:
        fh.write(f.encrypt(json.dumps(state, ensure_ascii=False).encode()))


# ================================================================ helpers
def http_get(url, **kw):
    kw.setdefault("timeout", 25)
    headers = kw.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    return requests.get(url, headers=headers, **kw)


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime.fromtimestamp(calendar.timegm(t), timezone.utc) if t else None


def clean(text, n=220):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:n]


def fmt_num(n):
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(int(n))


def iso_seconds(d):
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    dd, h, mi, s = (int(x or 0) for x in m.groups())
    return dd * 86400 + h * 3600 + mi * 60 + s


def esc(s):
    return html.escape(str(s or ""), quote=False)


# ================================================================ Gemini
def ask_gemini(prompt, models, temperature=0.4):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("thieu GEMINI_API_KEY")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": temperature}}
    last = None
    for model in models:
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"}, json=body, timeout=180)
                if r.status_code in (429, 500, 503):
                    last = f"{model}: HTTP {r.status_code}"
                    time.sleep(10 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    last = f"{model}: HTTP {r.status_code}"
                    break
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
                text = re.sub(r"^```(?:json)?|```$", "", text).strip()
                return json.loads(text), model
            except Exception as e:
                last = f"{model}: {type(e).__name__}"
                time.sleep(3)
    raise RuntimeError(last or "Gemini loi")


# ================================================================ 1. tin tuc (RSS / Trends / HN)
def read_feed(url):
    r = http_get(url)
    r.raise_for_status()
    return feedparser.parse(r.content)


def collect_feeds(urls, source, cfg):
    cutoff = NOW - timedelta(days=cfg["lookback_days"])
    cands, failed = [], 0
    for url in urls:
        try:
            feed = read_feed(url)
        except Exception:
            failed += 1
            continue
        n = 0
        for e in feed.entries:
            t = entry_time(e)
            if t and t < cutoff:
                continue
            src = e.get("source", {}).get("title") if isinstance(e.get("source"), dict) else None
            cands.append({"source": source, "title": clean(e.get("title"), 200), "url": e.get("link", ""),
                          "metric": src or feed.feed.get("title", ""), "snippet": clean(e.get("summary"), 160),
                          "label": (src or feed.feed.get("title", source))[:30], "weight": 0})
            n += 1
            if n >= cfg["per_feed"]:
                break
    HEALTH[source] = f"{len(cands)} tin" + (f", lỗi {failed}/{len(urls)} feed" if failed else "")
    return cands


def collect_trends(cfg):
    geo = cfg["google_trends_geo"]
    try:
        feed = read_feed(f"https://trends.google.com/trending/rss?geo={geo}")
    except Exception:
        HEALTH["Google Trends"] = "lỗi"
        return []
    cands = [{"source": "Google Trends", "title": clean(e.get("title"), 120),
              "url": e.get("ht_news_item_url") or f"https://trends.google.com/trending?geo={geo}",
              "metric": f"~{e.get('ht_approx_traffic', '?')} searches",
              "snippet": clean(e.get("ht_news_item_title", ""), 160), "label": "Google Trends", "weight": 0}
             for e in feed.entries[:25]]
    HEALTH["Google Trends"] = f"{len(cands)} từ khóa"
    return cands


def collect_hn(cfg):
    since = int((NOW - timedelta(days=cfg["lookback_days"])).timestamp())
    try:
        r = http_get("https://hn.algolia.com/api/v1/search", params={
            "tags": "story", "hitsPerPage": 40,
            "numericFilters": f"created_at_i>{since},points>{cfg['hacker_news_min_points']}"})
        r.raise_for_status()
        hits = r.json()["hits"]
    except Exception:
        HEALTH["Hacker News"] = "lỗi"
        return []
    HEALTH["Hacker News"] = f"{len(hits)} bài"
    return [{"source": "Hacker News", "title": h["title"],
             "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
             "metric": f"{h['points']} points, {h.get('num_comments', 0)} comments",
             "label": "Hacker News", "weight": h["points"]} for h in hits]


# ================================================================ 2. tu khoa
KEYWORD_PROMPT = """You pick YouTube search keywords for this channel:
{profile}

Fixed seed keywords (already used, do not repeat): {seeds}

Below are this week's headlines (news, Google Trends, science). Use them only as inspiration:
turn them into {n} NEW, BROAD, EVERGREEN curiosity keywords (2-6 words, the way a US viewer would type
them into YouTube, ideally "why / how / what if" questions) that will still be searched years from now.
Example: headline "Fed holds rates at 5%" -> keyword "why do interest rates matter", NOT "fed rate decision".
Spread across the channel's lanes (economics, science & space, nature & psychology).
Skip sports, celebrities, crime, gossip, and anything only relevant this week.
Return JSON only: {{"keywords": ["...", "..."]}}

HEADLINES:
{headlines}
"""


def build_keywords(cfg, news):
    seeds = [k.strip() for k in cfg["seed_keywords"] if k.strip()]
    n_seed, n_dyn = len(seeds), 0
    dyn = []
    if news and cfg["dynamic_keywords"] > 0:
        try:
            data, _ = ask_gemini(KEYWORD_PROMPT.format(
                profile=cfg["channel_profile"], seeds=", ".join(seeds) or "(none)", n=cfg["dynamic_keywords"],
                headlines="\n".join(f"- {c['title']}" for c in news[:150])), cfg["gemini_models"], 0.3)
            dyn = [k.strip() for k in data.get("keywords", []) if isinstance(k, str) and k.strip()]
            n_dyn = min(len(dyn), cfg["dynamic_keywords"])
        except Exception:
            HEALTH["Từ khóa AI"] = "lỗi"
    out, seen = [], set()
    dyn = dyn[: cfg["dynamic_keywords"]]
    mixed = []  # xen ke: 2 tu khoa goc (dai han) roi 1 tu khoa AI
    while seeds or dyn:
        mixed += seeds[:2] + dyn[:1]
        seeds, dyn = seeds[2:], dyn[1:]
    for k in mixed:
        if k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    HEALTH["Từ khóa"] = f"{n_seed} gốc + {n_dyn} AI"
    return out


# ================================================================ 3a. YouTube
def yt(path, **params):
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    r = http_get(f"https://www.googleapis.com/youtube/v3/{path}", params=params)
    r.raise_for_status()
    return r.json()


def yt_videos(ids):
    out = []
    for i in range(0, len(ids), 50):
        out += yt("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50])).get("items", [])
    return out


def yt_channels(cids):
    out = {}
    for i in range(0, len(cids), 50):
        for c in yt("channels", part="snippet,statistics", id=",".join(cids[i:i + 50])).get("items", []):
            st = c.get("statistics", {})
            out[c["id"]] = {"title": c["snippet"]["title"],
                            "subs": 0 if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))}
    return out


def parse_video(v):
    return {"id": v["id"], "title": v["snippet"]["title"], "cid": v["snippet"]["channelId"],
            "channel": v["snippet"].get("channelTitle", ""),
            "published": datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00")),
            "views": int(v["statistics"].get("viewCount", 0)),
            "seconds": iso_seconds(v["contentDetails"].get("duration"))}


def resolve_handles(handles):
    cids = []
    for h in handles:
        h = h if h.startswith("@") else "@" + h
        try:
            items = yt("channels", part="id", forHandle=h).get("items", [])
            if items:
                cids.append(items[0]["id"])
        except Exception:
            pass
    return cids


def youtube_search(cfg, keywords, state):
    ycfg = cfg["youtube"]
    after = (NOW - timedelta(days=ycfg["search_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids, kw_of, fails = [], {}, 0
    for kw in keywords[: ycfg["max_searches"]]:
        try:
            res = yt("search", part="id", q=kw, type="video", order="viewCount", publishedAfter=after,
                     regionCode="US", relevanceLanguage="en", maxResults=ycfg["per_search"])
        except Exception:
            fails += 1
            continue
        for it in res.get("items", []):
            vid = it["id"].get("videoId")
            if vid and vid not in kw_of:
                ids.append(vid)
                kw_of[vid] = kw
    vids = [parse_video(v) for v in yt_videos(ids)] if ids else []
    vids = [v for v in vids if v["seconds"] >= ycfg["min_seconds"] and v["views"] >= ycfg["min_views"]]
    chans = yt_channels(list({v["cid"] for v in vids})) if vids else {}
    cands, today = [], NOW.date().isoformat()
    for v in vids:
        subs = chans.get(v["cid"], {}).get("subs", 0)
        if subs <= 0:
            continue
        ratio = v["views"] / subs
        if ratio < ycfg["min_ratio_subs"]:
            continue
        age = max((NOW - v["published"]).days, 0)
        cands.append({"source": "YouTube", "title": v["title"], "url": f"https://youtu.be/{v['id']}",
                      "metric": f"{v['channel']} ({fmt_num(subs)} subs): {fmt_num(v['views'])} views = "
                                f"{ratio:.1f}x subs, {age}d old, keyword '{kw_of[v['id']]}'",
                      "label": f"{v['channel'][:22]} {ratio:.1f}x sub", "weight": ratio, "cid": v["cid"]})
        # tu hoc: ghi nhan kenh co outlier
        ch = state["channels"].setdefault(v["cid"], {"t": v["channel"], "hits": []})
        if today not in ch["hits"]:
            ch["hits"].append(today)
    note = f"{len(ids)} video tìm được → {len(cands)} outlier"
    if fails:
        note += f", lỗi {fails} lượt tìm"
    return cands, note


def watchlist(cfg, state):
    ycfg = cfg["youtube"]
    cut = (NOW - timedelta(days=ycfg["learn_window_days"])).date().isoformat()
    for cid in list(state["channels"]):
        ch = state["channels"][cid]
        ch["hits"] = [d for d in ch["hits"] if d >= cut]
        if not ch["hits"] and not ch.get("manual"):
            del state["channels"][cid]
    learned = [cid for cid, ch in state["channels"].items() if len(ch["hits"]) >= ycfg["learn_min_hits"]]
    learned.sort(key=lambda c: (len(state["channels"][c]["hits"]), max(state["channels"][c]["hits"])), reverse=True)
    return learned[: ycfg["watch_max"]]


def scan_channels(cfg, cids, skip_urls):
    """Kenh dang theo doi: video moi co views >= N lan trung vi cua chinh kenh."""
    ycfg = cfg["youtube"]
    cands = []
    for cid in cids:
        try:
            items = yt("playlistItems", part="contentDetails", playlistId="UU" + cid[2:], maxResults=50).get("items", [])
            vids = [parse_video(v) for v in yt_videos([i["contentDetails"]["videoId"] for i in items])]
        except Exception:
            continue
        longs = [v for v in vids if v["seconds"] >= ycfg["min_seconds"]]
        mature = [v["views"] for v in longs if (NOW - v["published"]).days >= 3]
        if len(mature) < 5:
            continue
        median = statistics.median(mature) or 1
        for v in longs:
            age, ratio = (NOW - v["published"]).days, v["views"] / median
            url = f"https://youtu.be/{v['id']}"
            if age <= ycfg["watch_window_days"] and ratio >= ycfg["watch_min_ratio"] and url not in skip_urls:
                cands.append({"source": "YouTube", "title": v["title"], "url": url,
                              "metric": f"{v['channel']} (watchlist): {fmt_num(v['views'])} views, "
                                        f"{ratio:.1f}x channel median, {age}d old",
                              "label": f"{v['channel'][:22]} {ratio:.1f}x TB", "weight": ratio})
    return cands


def collect_youtube(cfg, keywords, state):
    if not os.environ.get("YOUTUBE_API_KEY"):
        HEALTH["YouTube"] = "bỏ qua (thiếu key)"
        return []
    try:
        found, note = youtube_search(cfg, keywords, state)
    except Exception:
        found, note = [], "lỗi tìm kiếm"
    manual = resolve_handles(cfg["youtube"]["channels"]) if cfg["youtube"]["channels"] else []
    learned = watchlist(cfg, state)
    cids = list(dict.fromkeys(manual + learned))
    watch = scan_channels(cfg, cids, {c["url"] for c in found}) if cids else []
    HEALTH["YouTube"] = f"{note}; theo dõi {len(cids)} kênh ({len(learned)} tự học) → {len(watch)} outlier"
    cands = sorted(found, key=lambda c: -c["weight"])[:35] + sorted(watch, key=lambda c: -c["weight"])[:15]
    return cands


def own_channel_titles(cfg):
    if not cfg.get("own_channel") or not os.environ.get("YOUTUBE_API_KEY"):
        return []
    try:
        cids = resolve_handles([cfg["own_channel"]])
        items = yt("playlistItems", part="snippet", playlistId="UU" + cids[0][2:], maxResults=50).get("items", [])
        return [i["snippet"]["title"] for i in items]
    except Exception:
        return []


# ================================================================ 3b. Reddit (tim toan Reddit)
def reddit_token():
    cid, sec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and sec):
        return None
    r = requests.post("https://www.reddit.com/api/v1/access_token", auth=(cid, sec),
                      data={"grant_type": "client_credentials"}, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def collect_reddit(cfg, keywords):
    rcfg = cfg["reddit"]
    try:
        token = reddit_token()
    except Exception:
        token = None
    base = "https://oauth.reddit.com/search" if token else "https://www.reddit.com/search.json"
    headers = {"Authorization": f"bearer {token}"} if token else {}
    cutoff = NOW - timedelta(days=7)
    cands, failed, seen = [], 0, set()
    kws = keywords[: rcfg["search_keywords"]]
    for kw in kws:
        try:
            r = http_get(base, params={"q": kw, "sort": "top", "t": "week", "limit": 25, "type": "link"},
                         headers=dict(headers))
            r.raise_for_status()
            posts = [p["data"] for p in r.json()["data"]["children"]]
        except Exception:
            failed += 1
            continue
        n = 0
        for p in posts:
            if p.get("over_18") or p.get("score", 0) < rcfg["min_score"] or p["permalink"] in seen:
                continue
            if datetime.fromtimestamp(p["created_utc"], timezone.utc) < cutoff:
                continue
            seen.add(p["permalink"])
            cands.append({"source": "Reddit", "title": p["title"], "url": "https://www.reddit.com" + p["permalink"],
                          "metric": f"r/{p.get('subreddit', '?')}: {fmt_num(p['score'])} upvotes, "
                                    f"{fmt_num(p.get('num_comments', 0))} comments",
                          "snippet": clean(p.get("selftext"), 160), "label": f"r/{p.get('subreddit', '?')}",
                          "weight": p["score"]})
            n += 1
            if n >= rcfg["per_keyword"]:
                break
    HEALTH["Reddit"] = f"{len(cands)} bài ({'OAuth' if token else 'không OAuth'})" + \
                       (f", lỗi {failed}/{len(kws)} lượt tìm" if failed else "")
    return cands


# ================================================================ 3c. goi y tim kiem
def suggestions(q, youtube):
    params = {"client": "firefox", "q": q, "hl": "en", "gl": "us"}
    if youtube:
        params["ds"] = "yt"
    r = http_get("https://suggestqueries.google.com/complete/search", params=params, timeout=15)
    r.raise_for_status()
    data = json.loads(r.content.decode("utf-8", "ignore"))
    return [s for s in data[1] if isinstance(s, str)]


def collect_suggest(cfg, keywords):
    kws = keywords[: cfg["suggest"]["keywords"]]
    cands, failed = [], 0
    for kw in kws:
        try:
            yt_s = suggestions(kw, True)
            q = kw if re.match(r"(why|how|what|when|where|who|is|are|do|does|can)\b", kw, re.I) else f"why {kw}"
            g_s = suggestions(q, False)
        except Exception:
            failed += 1
            continue
        if yt_s or g_s:
            cands.append({"source": "Search suggest", "title": f"People search: {kw}",
                          "url": f"https://www.youtube.com/results?search_query={requests.utils.quote(kw)}",
                          "metric": "YouTube: " + "; ".join(yt_s[:8]) + " | Google: " + "; ".join(g_s[:5]),
                          "label": f"gợi ý '{kw[:20]}'", "weight": 0})
    HEALTH["Gợi ý tìm kiếm"] = f"{len(cands)} từ khóa" + (f", lỗi {failed}" if failed else "")
    return cands


# ================================================================ 4 + 6. vidIQ
BLOCK_TOOL = re.compile(r"create|update|edit|delete|upload|generat|publish|post|watch|reel|voice|feedback|"
                        r"async|job|track|thumbnail|image|video_generation|write", re.I)

TOOL_PROMPT = """You control a vidIQ research API with a strict credit budget: exactly ONE tool call.
Goal: {goal}

Rules: pick a READ-ONLY research tool. Never pick tools that create, edit, upload, generate media or
"watch" a video. Fill arguments exactly per the tool's input schema.

AVAILABLE TOOLS (name, description, input schema):
{tools}

Return JSON only: {{"tool": "<name>", "arguments": {{...}}}}
"""


def tool_catalog(tools):
    safe = [t for t in tools if not BLOCK_TOOL.search(t["name"])]
    lines = []
    for t in safe:
        schema = json.dumps(t.get("inputSchema", {}), ensure_ascii=False)[:600]
        lines.append(f"- {t['name']}: {clean(t.get('description', ''), 300)} | schema: {schema}")
    return safe, "\n".join(lines)


class VidIQBranch:
    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state
        self.client, self.tools, self.safe, self.catalog = None, [], [], ""
        self.note = ""
        self.discover_text = ""

    def connect(self):
        vcfg = self.cfg["vidiq"]
        if not vcfg.get("enabled"):
            self.note = "tắt"
            return False
        blobs = []
        if self.state.get("vidiq_auth"):
            blobs.append(self.state["vidiq_auth"])
        raw = os.environ.get("VIDIQ_AUTH", "").strip()
        if raw:
            try:
                blobs.append(json.loads(base64.b64decode(raw)))
            except Exception:
                pass
        if not blobs:
            self.note = "chưa đăng nhập (thiếu VIDIQ_AUTH)"
            return False
        blobs.sort(key=lambda b: b.get("updated", 0), reverse=True)
        for blob in blobs:
            try:
                new = vidiq.refresh_access(blob) if blob.get("refresh_token") else blob
                client = vidiq.VidIQ(new["access_token"])
                client.start()
                self.tools = client.list_tools()
                self.client = client
                self.state["vidiq_auth"] = new  # luu refresh_token moi (da ma hoa)
                self.safe, self.catalog = tool_catalog(self.tools)
                return True
            except Exception:
                continue
        self.note = "đăng nhập hết hạn → chạy lại vidiq_login.py"
        return False

    def budget_left(self):
        return self.client and self.client.calls < self.cfg["vidiq"]["max_calls"]

    def run_tool(self, goal, override, kws):
        """override (tu config, khi da biet ten cong cu): {tool: ten, arguments: {...}}
        trong arguments: "{keywords}" -> danh sach tu khoa, "{keyword}" -> tu khoa dau tien."""
        if not self.budget_left():
            return None
        if override and override.get("tool"):
            def fill(v):
                if v == "{keywords}":
                    return kws
                if v == "{keyword}":
                    return kws[0] if kws else ""
                if v == "{niche}":
                    return self.cfg["vidiq"].get("niche") or clean(self.cfg["channel_profile"], 300)
                if isinstance(v, dict):
                    return {k: fill(x) for k, x in v.items()}
                return v
            choice = {"tool": override["tool"], "arguments": fill(override.get("arguments") or {})}
        else:
            choice, _ = ask_gemini(TOOL_PROMPT.format(goal=goal, tools=self.catalog), self.cfg["gemini_models"], 0.1)
        name = choice.get("tool")
        if name not in {t["name"] for t in self.safe}:
            raise vidiq.VidIQError("công cụ không hợp lệ")
        text = self.client.call(name, choice.get("arguments") or {})
        return name, text[:9000]

    def discover(self, keywords):
        goal = (f"Find video ideas / topics that are trending or rising RIGHT NOW on YouTube in this niche: "
                f"{clean(self.cfg['channel_profile'], 400)}. Related keywords: {', '.join(keywords[:12])}. "
                f"English, US audience, long-form videos.")
        try:
            got = self.run_tool(goal, self.cfg["vidiq"].get("discover"), keywords[:12])
            self.discover_text = got[1] if got else ""
            return got
        except Exception as e:
            self.note += f" khám phá lỗi ({clean(str(e), 60)});"
            return None

    def check(self, topics):
        kws = [t.get("keyword") or t.get("topic") for t in topics]
        goal = ("Check YouTube search demand (search volume) and competition for this keyword: " + kws[0])
        try:
            return self.run_tool(goal, self.cfg["vidiq"].get("check"), kws)
        except Exception as e:
            self.note += f" kiểm tra lỗi ({clean(str(e), 60)});"
            return None

    def credits(self):
        for t in self.tools:
            if re.search(r"credit|balance", t["name"], re.I) and not t.get("inputSchema", {}).get("required"):
                try:
                    txt = self.client.call(t["name"], {}, count=False)
                    m = re.search(r'"?totalCredits"?\D{0,5}(\d+)', txt) or re.search(r"(\d[\d,]*)", txt)
                    return m.group(1) if m else None
                except Exception:
                    return None
        return None


# ================================================================ 5. xep hang
RANK_PROMPT = """You are the content strategist for a YouTube channel.

CHANNEL PROFILE:
{profile}

VIDEOS THE CHANNEL ALREADY MADE (do NOT suggest these again):
{own}

TOPICS ALREADY SENT IN THE LAST {hdays} DAYS (do NOT repeat or re-word these):
{history}

Two research branches were collected in the last few days.

BRANCH A - raw signals, each with an ID:
- "YouTube" = videos far above normal (views vs subscribers, or vs the channel's median) -> proven demand.
- "Reddit" = what US communities discuss / ask.
- "Search suggest" = what people type into YouTube/Google search.
- "Google News" / "Google Trends" = current events. "Science" / "Hacker News" = new discoveries.
{signals}

BRANCH B - vidIQ research output (cite it with source id "VIDIQ"):
{vidiq}

TASK: Pick the {top} best VIDEO TOPICS for this channel. Rules:
1. A topic must sustain a 25-30 minute data-driven explainer for a US audience. Skip celebrity gossip,
   sports results, crime, short-lived political fights.
2. Prefer topics backed by several signals, especially across both branches. Merge signals about the same subject.
3. Strongly prefer EVERGREEN curiosity questions (still searched in 2-3 years) that are getting proven attention.
   News is only a hook: a timely event qualifies only if it opens a durable question
   (e.g. "why do interest rates matter", not "what the Fed did this week").
4. Each topic = ONE clear question a viewer has wondered about ("Why does...?", "What would happen if...?").
5. Balance the channel's lanes (economics / science & space / nature & psychology): try to cover at least 2 lanes, quality first.
6. "why_hot" and "angle" in Vietnamese (1-2 short sentences each). "titles" in English, catchy, under 70 chars.
7. "keyword": the main 2-5 word YouTube search keyword for this topic (English).
8. "source_ids": 1-4 supporting IDs, most important first (may include "VIDIQ").

Return JSON only:
{{"topics":[{{"topic":"short English topic name","category":"economics|science & space|nature & psychology","why_hot":"...","angle":"...","titles":["...","..."],"keyword":"...","hotness":1-10,"fit":1-10,"evergreen":"cao|trung bình|thấp","source_ids":["S1"]}}]}}
"""

CHECK_PROMPT = """Here are {n} YouTube video topics (numbered) and raw vidIQ data: a keyword check for topic 1
(with related keywords) and this week's rising keywords in the niche (search growth).
For each topic, write a very short Vietnamese note (max 15 words) on search demand / growth + competition based ONLY
on the vidIQ data (match related or rising keywords to other topics when they clearly fit) (e.g. "Search volume cao (~40K/tháng), cạnh tranh trung bình"). If vidIQ has no data
for a topic, use "". Then give the best final order (most promising first) using topic quality AND vidIQ data.

TOPICS:
{topics}

VIDIQ KEYWORD CHECK (topic 1):
{data}

VIDIQ RISING KEYWORDS:
{rising}

Return JSON only: {{"notes": {{"1": "...", "2": "..."}}, "order": [1,2,3,4,5]}}
"""


# ================================================================ Telegram
def build_messages(topics, by_id, header, footer):
    blocks = [header]
    for i, t in enumerate(topics, 1):
        links = []
        for sid in (t.get("source_ids") or [])[:4]:
            sid = str(sid).strip()
            if sid.upper() == "VIDIQ":
                links.append("vidIQ")
                continue
            c = by_id.get(sid)
            if c and c["url"]:
                links.append(f'<a href="{html.escape(c["url"])}">{esc(c["source"])}: {esc(c["label"])}</a>')
        titles = "\n".join(f"   • {esc(x)}" for x in (t.get("titles") or [])[:2])
        vq = f"🔎 <b>vidIQ:</b> {esc(t['vidiq_note'])}\n" if t.get("vidiq_note") else ""
        blocks.append(
            f"<b>{i}. {esc(t.get('topic'))}</b>\n"
            f"🏷 {esc(t.get('category'))} · 🔥 {esc(t.get('hotness'))}/10 · 🎯 hợp kênh {esc(t.get('fit'))}/10 · "
            f"⏳ evergreen: {esc(t.get('evergreen'))}\n"
            f"📈 <b>Vì sao hot:</b> {esc(t.get('why_hot'))}\n"
            f"🎬 <b>Góc khai thác:</b> {esc(t.get('angle'))}\n"
            f"{vq}"
            f"💡 <b>Gợi ý tiêu đề:</b>\n{titles}\n"
            f"🔗 " + (" | ".join(links) if links else "(không có link)"))
    blocks.append(footer)
    msgs, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 2 > 3900:
            msgs.append(cur)
            cur = ""
        cur += b + "\n\n"
    if cur.strip():
        msgs.append(cur)
    return msgs


def send_telegram(msgs):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if DRY_RUN:
        for m in msgs:
            print("\n" + "=" * 60 + "\n" + m)
        return
    if not token or not chat:
        log("LOI TELEGRAM: thieu Secret " + ("TELEGRAM_BOT_TOKEN" if not token else "TELEGRAM_CHAT_ID"))
        raise SystemExit(1)
    ok = 0
    for m in msgs:
        for payload in ({"chat_id": chat, "text": m, "parse_mode": "HTML", "disable_web_page_preview": True},
                        {"chat_id": chat, "text": re.sub(r"<[^>]+>", "", m), "disable_web_page_preview": True}):
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, json=payload)
            if r.status_code == 200:
                ok += 1
                break
            try:
                desc = r.json().get("description", "")
            except ValueError:
                desc = ""
            log(f"LOI TELEGRAM: HTTP {r.status_code} {desc}")
        time.sleep(1)
    log(f"da gui {ok}/{len(msgs)} tin Telegram")
    if ok == 0:
        raise SystemExit(1)


# ================================================================ main
def main():
    cfg = load_config()
    state = load_state()
    hist_cut = (NOW - timedelta(days=cfg["history_days"])).date().isoformat()
    state["history"] = [h for h in state["history"] if h["date"] >= hist_cut]
    sent_urls = {u for h in state["history"] for u in h.get("urls", [])}
    sent_topics = [t for h in state["history"] for t in h.get("topics", [])]

    # 1. tin tuc
    news = collect_trends(cfg)
    news += collect_feeds(cfg["google_news"], "Google News", cfg)
    news += collect_feeds(cfg["science_feeds"], "Science", cfg)
    news += collect_hn(cfg)
    # 2. tu khoa
    log(f"tin tuc: {len(news)} tin")
    keywords = build_keywords(cfg, news)
    log(f"tu khoa: {len(keywords)}")
    # 3. nhanh co ban
    yt_c = collect_youtube(cfg, keywords, state)
    log(f"youtube: {len(yt_c)} outlier")
    rd_c = collect_reddit(cfg, keywords)
    log(f"reddit: {len(rd_c)} bai")
    sg_c = collect_suggest(cfg, keywords)
    log(f"goi y tim kiem: {len(sg_c)}")
    cands = yt_c + rd_c + sg_c + news
    # 4. nhanh vidIQ
    vq = VidIQBranch(cfg, state)
    vq_text = "(vidIQ not available this run)"
    if vq.connect():
        log(f"vidiq: ket noi OK, {len(vq.tools)} cong cu")
        got = vq.discover(keywords)
        if got:
            vq_text = f"[tool {got[0]}]\n{got[1]}"
        log(f"vidiq kham pha: {'OK ' + str(len(got[1])) + ' ky tu' if got else 'khong co ket qua'}")
    else:
        log(f"vidiq: khong ket noi ({vq.note})")

    seen, uniq = set(), []
    for c in cands:
        k = c["url"] or c["title"].lower()
        if not c["title"] or k in seen or c["url"] in sent_urls:
            continue
        seen.add(k)
        uniq.append(c)
    by_id = {}
    for i, c in enumerate(uniq, 1):
        c["id"] = f"S{i}"
        by_id[c["id"]] = c
    log(f"tong tin hieu sau loc trung: {len(uniq)}")

    weekday = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "CN"]
    local = NOW + timedelta(hours=7)
    header = (f"🛰 <b>{esc(cfg['report_title'])} · Top {cfg['top_n']} chủ đề</b>\n"
              f"{weekday[local.weekday()]}, {local:%d/%m/%Y} · dữ liệu {cfg['lookback_days']} ngày gần nhất")

    def footer(model):
        if vq.client:
            cr = vq.credits()
            HEALTH["vidIQ"] = f"{vq.client.calls} lượt gọi" + (f", còn {cr} credit" if cr else "") + \
                              (f" ·{vq.note}" if vq.note else "")
        else:
            HEALTH["vidIQ"] = vq.note or "không dùng"
        return "📊 <i>" + esc(" · ".join(f"{k}: {v}" for k, v in HEALTH.items())) + "</i>" + \
               (f"\n🤖 <i>{esc(model)}</i>" if model else "")

    if not uniq and not vq.client:
        send_telegram([header + "\n\n⚠️ Không lấy được dữ liệu nào.\n\n" + footer(None)])
        return

    signals = "\n".join(
        f"[{c['id']}] ({c['source']}) {c['title']} | {c['metric']}" + (f" | {c['snippet']}" if c.get("snippet") else "")
        for c in uniq)
    prompt = RANK_PROMPT.format(
        profile=cfg["channel_profile"], own="\n".join(f"- {t}" for t in own_channel_titles(cfg)) or "(none)",
        hdays=cfg["history_days"], history="\n".join(f"- {t}" for t in sent_topics) or "(none)",
        signals=signals or "(none)", vidiq=vq_text, top=cfg["top_n"])

    topics = []
    try:
        data, model = ask_gemini(prompt, cfg["gemini_models"])
        topics = data["topics"][: cfg["top_n"]]
        log(f"gemini ({model}) chon {len(topics)} chu de")
        # 6. vidIQ kiem tra lan cuoi
        if vq.client and topics:
            got = vq.check(topics)
            log(f"vidiq kiem tra: {'OK' if got else 'khong co ket qua'}")
            if got:
                try:
                    listing = "\n".join(f"{i}. {t.get('topic')} (keyword: {t.get('keyword')})" for i, t in enumerate(topics, 1))
                    chk, _ = ask_gemini(CHECK_PROMPT.format(n=len(topics), topics=listing, data=got[1],
                                                             rising=vq.discover_text[:5000] or "(none)"),
                                        cfg["gemini_models"], 0.2)
                    for k, note in (chk.get("notes") or {}).items():
                        if str(k).isdigit() and 1 <= int(k) <= len(topics):
                            topics[int(k) - 1]["vidiq_note"] = note
                    order = [int(x) for x in chk.get("order", []) if str(x).isdigit() and 1 <= int(x) <= len(topics)]
                    if sorted(order) == list(range(1, len(topics) + 1)):
                        topics = [topics[i - 1] for i in order]
                except Exception:
                    vq.note += " không đọc được kết quả kiểm tra;"
        msgs = build_messages(topics, by_id, header, footer(model))
    except Exception as e:
        log(f"gemini LOI ({clean(str(e), 80)}) -> gui danh sach tho")
        top_yt = sorted([c for c in uniq if c["source"] == "YouTube"], key=lambda c: -c["weight"])[:6]
        top_rd = sorted([c for c in uniq if c["source"] == "Reddit"], key=lambda c: -c["weight"])[:4]
        lines = [f'• <a href="{html.escape(c["url"])}">{esc(c["title"])}</a> — {esc(c["metric"])}' for c in top_yt + top_rd]
        msgs = [header + f"\n\n⚠️ AI lỗi ({esc(e)}), đây là tín hiệu mạnh nhất chưa lọc:\n\n" + "\n".join(lines)
                + "\n\n" + footer(None)]
        topics = []

    send_telegram(msgs)
    if not DRY_RUN:
        if topics:
            urls = [by_id[s]["url"] for t in topics for s in (t.get("source_ids") or []) if s in by_id]
            state["history"].append({"date": NOW.date().isoformat(),
                                     "topics": [t.get("topic", "") for t in topics], "urls": urls})
        save_state(state)  # luon luu: kenh tu hoc + token vidIQ moi
        log(f"da luu state · kenh tu hoc dang theo doi: {len(watchlist(cfg, state))} · vidiq: {vq.client.calls if vq.client else 0} luot goi")


if __name__ == "__main__":
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code or 0
    except Exception as e:  # khong in traceback (repo public)
        log(f"LOI: {type(e).__name__} o {e.__traceback__.tb_next.tb_frame.f_code.co_name if e.__traceback__.tb_next else '?'}")
        code = 1
    log(f"ket thuc sau {time.time() - T0:.0f}s")
    write_run_log("OK" if code == 0 else "LOI")
    sys.exit(code)
