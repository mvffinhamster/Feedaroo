# pip install -r requirements.txt
# Feedaroo — Oscar-positive news only (telemetry v2 edition)

import feedparser, requests, time, hashlib, json, os, re, traceback
from datetime import datetime, timedelta
from urllib.parse import urlparse
from textblob import TextBlob
from transformers import pipeline

# ============ Constants ============
USER_AGENT = {"User-Agent": "Feedaroo/2.0 (+https://github.com/feedaroo)"}
EMBED_COLOR = 0xFF9900
MAX_DESC_LENGTH = 300
MAX_SENT_ENTRIES = 10000
SENT_EXPIRY_DAYS = 30
DISCORD_RATE_LIMIT_DELAY = 2

SOURCE_EMOJIS = {
    "speedcafe.com": "🟢",
    "motorsport.com": "🟡",
    "news.com.au": "🔵",
    "foxsports.com.au": "🔴",
    "abc.net.au": "⚪️",
    "theage.com.au": "🟣",
    "smh.com.au": "⚫️"
}

# ============ Config / env ============
def load_env():
    try:
        with open("env.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in data.items():
                os.environ[k] = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
            print("✅ env.json loaded.")
    except FileNotFoundError:
        print("ℹ️ No env.json found, using OS environment variables.")

def get_list_env(name, default=None):
    raw = os.getenv(name)
    if not raw:
        return default or []
    try:
        return json.loads(raw) if raw.strip().startswith("[") else [s.strip() for s in raw.split(",") if s.strip()]
    except Exception:
        return default or []

load_env()

WEBHOOK        = os.getenv("WEBHOOK", "").strip()
LOG_WEBHOOK    = os.getenv("LOG_WEBHOOK", "").strip()
FEEDS          = get_list_env("FEEDS", [])
KEYWORDS       = [k.lower() for k in get_list_env("KEYWORDS", [])]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "15")) * 60
BOT_NAME       = os.getenv("BOT_NAME", "Feedaroo 🦘")
SENT_DB        = os.getenv("SENT_DB", "sent_feedaroo.json")
NEW_SENT_DB    = os.getenv("NEW_SENT_DB", "sent_feedaroo.json")
POS_THRESHOLD  = float(os.getenv("POS_THRESHOLD", "0.15"))
DEBUG          = os.getenv("DEBUG", "0") == "1"
LOG_FILE       = os.getenv("LOG_FILE", "feedaroo_debug.log")
NEGATIVE_HINTS = [s.lower() for s in get_list_env("NEGATIVE_HINTS", [])]
OSCAR_TERMS    = [k for k in KEYWORDS if k] or ["oscar", "piastri", "oscar piastri"]

# ============ Debug ============
def dbg(msg):
    if not DEBUG:
        return
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

# ============ Sent DB ============
def load_sent():
    try:
        with open(SENT_DB, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {uid_val: datetime.now().isoformat() for uid_val in data}
            return data
    except Exception:
        return {}

def save_sent(sent):
    with open(SENT_DB, "w", encoding="utf-8") as f:
        json.dump(sent, f, indent=2)
        f.close()
    print("saved")
    time.sleep(DISCORD_RATE_LIMIT_DELAY)
    return

def cleanup_sent(sent):
    cutoff = datetime.now() - timedelta(days=SENT_EXPIRY_DAYS)
    return {k: v for k, v in sent.items() if datetime.fromisoformat(v) > cutoff}

# ============ Helpers ============
def uid(entry):
    link = getattr(entry, "link", "").strip()
    if link:
        link = re.sub(r"[?#].*", "", link)
        return hashlib.sha256(link.encode("utf-8")).hexdigest()
    return hashlib.sha256(getattr(entry, "title", "").encode("utf-8")).hexdigest()

def clean_desc(text):
    text = re.sub("<[^<]+?>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300] + "..." if len(text) > 300 else text

def get_source_name(link):
    try:
        host = urlparse(link).netloc
        return host.replace("www.", "")
    except:
        return "unknown"

def find_source_emoji(link):
    for k, e in SOURCE_EMOJIS.items():
        if k in link:
            return e
    return "🦘"

def is_positive(text):
    if not text:
        return False, 0.0
    try:
        sentiment_analyzer = pipeline("text-classification", model="srimeenakshiks/aspect-based-sentiment-analyzer-using-bert")
        result = sentiment_analyzer(text, aspect="Oscar")
        print(result)
        pol = TextBlob(text).sentiment.polarity
        return pol >= POS_THRESHOLD, pol
    except:
        return False, 0.0

def contains_any(blob, terms):
    return any(t in blob for t in terms if t)

def classify_article(title, desc):
    blob = f"{title} {desc}".lower()
    return contains_any(blob, NEGATIVE_HINTS) and contains_any(blob, OSCAR_TERMS)

# ============ Send ============
def send_to_discord(title, link, desc=None, img=None, emoji="🦘"):
    clean_link = re.sub(r"\?.*", "", link)
    desc_text = f"{clean_desc(desc)}\n\n🔗 {clean_link}"

    embed = {
        "title": f"{emoji} {title}"[:256],
        "url": link,
        "description": desc_text[:4096],
        "color": EMBED_COLOR,
        "timestamp": datetime.utcnow().isoformat()
    }

    if img:
        embed["image"] = {"url": img}

    #requests.post(WEBHOOK, json={"username": BOT_NAME, "embeds": [embed]}, timeout=10)

# ============ Process ============
def process_feed(url, sent, stats):
    # print("process_feed sent", sent)
    feed = feedparser.parse(url, request_headers=USER_AGENT)
    for entry in getattr(feed, "entries", []):
        stats["entries"] += 1
        title = (getattr(entry, "title", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()
        desc = getattr(entry, "summary", "") or getattr(entry, "description", "")
        src = get_source_name(link)
        emoji = find_source_emoji(link)
        entry_id = uid(entry)
        
        
        if not title or not link:
            continue
        if entry_id in sent:
            stats["dupes"] += 1
            continue
        if classify_article(title, desc):
            stats["negatives"] += 1
            continue

        ok_pol, _ = is_positive(desc or title)
        if not ok_pol:
            stats["skipped"] += 1
            continue

        if KEYWORDS and not any(k in title.lower() for k in KEYWORDS):
            stats["keyword_miss"] += 1
            continue

        send_to_discord(title, link, desc, None, emoji)
        sent[entry_id] = datetime.now().isoformat()
        stats["posted"] += 1
        time.sleep(DISCORD_RATE_LIMIT_DELAY)
    # print("end proccess_ sent", sent)
    return sent
        
# ============ Telemetry ============
def send_telemetry(stats, run_type, memory_count, status="success", error=None):
    webhook = LOG_WEBHOOK
    if not webhook:
        return

    if status == "fail":
        msg = (
            f"🕒 **Telemetry Report: Feedaroo ({run_type} Run)**\n"
            f"⚠️ Feedaroo encountered an error during execution.\n"
            f"💥 Error: `{error}`\n"
            f"🧠 Memory state preserved. No data loss detected.\n\n"
            "*Copy that, Feedaroo. Standing by for next run.*"
        )
        msg += "\n_  _"
    else:
        posted = stats["posted"]
        skipped = stats["skipped"]
        dupes = stats["dupes"]
        keyword_miss = stats["keyword_miss"]
        negatives = stats["negatives"]

        dupes_line = f"☑️ Duplicates: {dupes}" if dupes > 0 else "☑️ No duplicates found"
        neg_line = f"🚫 Oscar Negative: {negatives}" if negatives > 0 else "🚫 No negative articles found"
        mem_line = (
            f"🧠 Memory updated — **{posted} new** entries saved (Articles in memory: {memory_count})"
            if posted > 0 else
            f"🧠 Memory clean. No new articles. (**{memory_count} already saved.**)"
        )

        msg = (
            f"🕒 **Telemetry Report: Feedaroo ({run_type} Run)**\n"
            f"Feeds checked: {stats['feeds']}\n"
            f"Total entries checked: {stats['entries']}\n"
            f"✅ Posted: {posted}\n"
            f"❌ Skipped: {skipped}\n"
            f"{dupes_line}\n"
            f"#️⃣ No Keyword Match: {keyword_miss}\n"
            f"{neg_line}\n"
            f"{mem_line}\n\n"
            "*Copy that, Feedaroo. Telemetry clean, keep going.*"
        )
        msg += "\n_  _"

    requests.post(webhook, json={"content": msg}, timeout=10)

# ============ Run ============
def single_check():
    sent = cleanup_sent(load_sent())
    print("sent", sent)
    stats = {"feeds": len(FEEDS), "entries": 0, "posted": 0, "skipped": 0, "dupes": 0, "keyword_miss": 0, "negatives": 0}
    run_type = "Manual" if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" else "Scheduled"
    try:
        for feed_url in FEEDS:
            sent = process_feed(feed_url, sent, stats)
        # print("returned sent", sent)
        save_sent(sent)
        memory_count = len(sent)
        status = "success" if stats["posted"] > 0 else "idle"
        send_telemetry(stats, run_type, memory_count, status)
        
        sent = cleanup_sent(load_sent())
        print("final sent", sent)
    except Exception as e:
        err_text = str(e).split("\n")[0]
        send_telemetry(stats, run_type, len(sent), status="fail", error=err_text)
        traceback.print_exc()

if __name__ == "__main__":
    single_check()
