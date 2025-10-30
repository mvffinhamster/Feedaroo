# pip install -r requirements.txt
# Feedaroo — Oscar-positive news only (clean + detailed log edition + Discord telemetry summary)

import feedparser, requests, time, hashlib, json, os, re
from datetime import datetime, timedelta
from urllib.parse import urlparse
from textblob import TextBlob

# ============ Constants ============
USER_AGENT = {"User-Agent": "Feedaroo/2.0 (+https://github.com/feedaroo)"}
EMBED_COLOR = 0xFF9900
MAX_DESC_LENGTH = 300
MAX_SENT_ENTRIES = 10000
SENT_EXPIRY_DAYS = 30
DISCORD_RATE_LIMIT_DELAY = 2  # seconds between posts

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
            print("✅ env.json loaded for Feedaroo.")
    except FileNotFoundError:
        print("ℹ️ No env.json found, using OS environment variables.")
    except json.JSONDecodeError as e:
        print(f"⚠️ Invalid env.json format: {e}")
    except Exception as e:
        print(f"⚠️ Error loading env.json: {e}")

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
POS_THRESHOLD  = float(os.getenv("POS_THRESHOLD", "0.15"))
DEBUG          = os.getenv("DEBUG", "0") == "1"
LOG_FILE       = os.getenv("LOG_FILE", "feedaroo_debug.log")

NEGATIVE_HINTS = [s.lower() for s in get_list_env("NEGATIVE_HINTS", [])]
OSCAR_TERMS    = [k for k in KEYWORDS if k] or ["oscar", "piastri", "oscar piastri"]

# ============ Debug log ============
def dbg(msg: str):
    if not DEBUG:
        return
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg.rstrip()}\n")
    except Exception as e:
        print(f"⚠️ Debug log error: {e}")

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
    try:
        with open(SENT_DB, "w", encoding="utf-8") as f:
            json.dump(sent, f, indent=2)
    except Exception as e:
        dbg(f"Failed to save sent DB: {e}")

def cleanup_sent(sent: dict) -> dict:
    if not sent:
        return sent
    cutoff = datetime.now() - timedelta(days=SENT_EXPIRY_DAYS)
    cleaned = {k: v for k, v in sent.items() if datetime.fromisoformat(v) > cutoff}
    if len(cleaned) > MAX_SENT_ENTRIES:
        cleaned = dict(sorted(cleaned.items(), key=lambda x: x[1], reverse=True)[:MAX_SENT_ENTRIES])
    return cleaned

# ============ Helpers ============
def uid(entry):
    link = getattr(entry, "link", "").strip()
    if link:
        link = re.sub(r"[?#].*", "", link)
        return hashlib.sha256(link.encode("utf-8", "ignore")).hexdigest()
    title = getattr(entry, "title", "").strip()
    return hashlib.sha256(title.encode("utf-8", "ignore")).hexdigest()

def clean_desc(text):
    text = re.sub("<[^<]+?>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_DESC_LENGTH:
        text = text[:MAX_DESC_LENGTH].rsplit(" ", 1)[0] + "..."
    return text

def get_source_name(link):
    try:
        host = urlparse(link).netloc
        return host.replace("www.", "")
    except Exception:
        return "unknown"

def find_source_emoji(link):
    for key, emoji in SOURCE_EMOJIS.items():
        if key in link:
            return emoji
    return "🦘"

def is_positive(text, threshold):
    if not text:
        return False, 0.0
    try:
        pol = TextBlob(text).sentiment.polarity
        return pol >= threshold, pol
    except Exception as e:
        dbg(f"Sentiment error: {e}")
        return False, 0.0

def contains_any(blob: str, terms: list[str]) -> bool:
    return any(t in blob for t in terms if t)

def classify_article(title: str, desc: str) -> bool:
    blob = f"{title} {desc}".lower()
    neg_hit = contains_any(blob, NEGATIVE_HINTS)
    oscar_hit = contains_any(blob, OSCAR_TERMS)
    return neg_hit and oscar_hit

# ============ Discord send ============
def send_to_discord(title, link, desc=None, img=None, emoji="🦘"):
    clean_link = re.sub(r"\?.*", "", link)
    desc_text = clean_desc(desc or "")
    desc_text = (desc_text + f"\n\n🔗 {clean_link}").strip()

    embed = {
        "title": f"{emoji} {title}"[:256],
        "url": link,
        "description": desc_text[:4096],
        "color": EMBED_COLOR,
        "timestamp": datetime.utcnow().isoformat()
    }

    if img:
        embed["image"] = {"url": img}

    data = {"username": BOT_NAME, "embeds": [embed]}

    try:
        if not WEBHOOK:
            print("⚠️ No WEBHOOK set.")
            return False
        requests.post(WEBHOOK, json=data, timeout=10)
        print(f"✅ Sent: {title[:60]}")
        dbg(f"✅ Posted: {title}")
        return True
    except Exception as e:
        dbg(f"Discord error: {e}")
        print(f"❌ Discord error: {e}")
        return False

# ============ Feed processing ============
def process_feed(url, sent, stats):
    try:
        dbg(f"Fetching feed: {url}")
        feed = feedparser.parse(url, request_headers=USER_AGENT)

        for entry in getattr(feed, "entries", []):
            stats["entries"] += 1
            title = (getattr(entry, "title", "") or "").strip()
            link  = (getattr(entry, "link", "") or "").strip()
            desc  = getattr(entry, "summary", "") or getattr(entry, "description", "")
            src   = get_source_name(link)
            emoji = find_source_emoji(link)
            entry_id = uid(entry)

            if not title or not link:
                continue
            if entry_id in sent:
                dbg(f"☑️ [{src}] DUPLICATE: '{title[:80]}'")
                stats["dupes"] += 1
                continue

            if classify_article(title, desc):
                dbg(f"🚫 [{src}] Negative Oscar article: '{title[:80]}'")
                stats["skipped"] += 1
                continue

            ok_pol, pol = is_positive(desc or title, POS_THRESHOLD)
            sentiment_label = (
                "⭐️ POSITIVE" if pol >= POS_THRESHOLD
                else "⚪️ NEUTRAL" if pol > -0.05
                else "🔴 NEGATIVE"
            )
            dbg(f"🔍 [{src}] '{title[:80]}' → sentiment={pol:.2f} → {sentiment_label}")

            if not ok_pol:
                dbg(f"❌ [{src}] Skipped (below threshold {POS_THRESHOLD})")
                stats["skipped"] += 1
                continue

            if KEYWORDS and not any(k in title.lower() for k in KEYWORDS):
                dbg(f"⏭️ [{src}] No keyword match: '{title[:80]}'")
                stats["skipped"] += 1
                continue

            img = None
            if hasattr(entry, "media_content"):
                for m in entry.media_content:
                    if m.get("url", "").startswith("http"):
                        img = m["url"]
                        break

            if send_to_discord(title, link, desc, img, emoji):
                sent[entry_id] = datetime.now().isoformat()
                stats["posted"] += 1
                time.sleep(DISCORD_RATE_LIMIT_DELAY)

    except Exception as e:
        dbg(f"💥 Error processing feed {url}: {e}")

# ============ Discord Log Summary ============
def send_log_summary(stats, run_type="Scheduled"):
    webhook = LOG_WEBHOOK
    if not webhook:
        dbg("ℹ️ No LOG_WEBHOOK set, skipping log summary.")
        return

    bot_name = BOT_NAME or "Feedaroo 🦘"
    emoji = "🦘"
    msg = (
        f"🕒 **Telemetry Report: {bot_name} ({run_type} Run)**\n"
        f"Feeds checked: {stats.get('feeds', 0)}\n"
        f"Total entries parsed: {stats.get('entries', 0)}\n"
        f"✅ Posted: {stats.get('posted', 0)} | ❌ Skipped: {stats.get('skipped', 0)} | ☑️ Duplicates: {stats.get('dupes', 0)}\n"
        f"🧠 Memory updated — {stats.get('posted', 0)} new entries saved.\n\n"
        f"Copy that, Feedaroo. Telemetry clean, keep going."
    )

    try:
        requests.post(webhook, json={"content": msg}, timeout=10)
        dbg("✅ Log summary sent to Discord.")
    except Exception as e:
        dbg(f"❌ Failed to send log summary: {e}")

# ============ Modes ============
def single_check():
    sent = cleanup_sent(load_sent())
    stats = {"feeds": len(FEEDS), "entries": 0, "posted": 0, "skipped": 0, "dupes": 0}
    print(f"📦 Memory check: loaded {len(sent)} entries from cache.")
    print(f"🦘 {BOT_NAME} single run, {len(FEEDS)} feeds.")

    for feed_url in FEEDS:
        process_feed(feed_url, sent, stats)

    save_sent(sent)
    print(f"✅ Done, {stats['posted']} new posts.")

    run_type = "Manual" if os.getenv("GITHUB_EVENT_NAME", "") == "workflow_dispatch" else "Scheduled"
    send_log_summary(stats, run_type)

# ============ Entry point ============
if __name__ == "__main__":
    try:
        if CHECK_INTERVAL > 100000:
            single_check()
        else:
            single_check()  # we don’t loop for GitHub actions
    except KeyboardInterrupt:
        print("🛑 Stopped manually.")