# pip install -r requirements.txt
# Feedaroo — Piastri-positive news + McLaren slander detector

import feedparser
import requests
import time
import hashlib
import json
import os
import re
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

# Core config
WEBHOOK        = os.getenv("WEBHOOK", "").strip()
FEEDS          = get_list_env("FEEDS", [])
KEYWORDS       = [k.lower() for k in get_list_env("KEYWORDS", [])]           # Oscar filter (title)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "15")) * 60
BOT_NAME       = os.getenv("BOT_NAME", "Feedaroo 🦘")
SENT_DB        = os.getenv("SENT_DB", "sent_feedaroo.json")
POS_THRESHOLD  = float(os.getenv("POS_THRESHOLD", "0.15"))
DEBUG          = os.getenv("DEBUG", "0") == "1"
LOG_FILE       = os.getenv("LOG_FILE", "feedaroo_debug.log")

# New classification hints (configurable via Secrets / env.json)
NEGATIVE_HINTS = [s.lower() for s in get_list_env("NEGATIVE_HINTS", [])]
SLANDER_HINTS  = [s.lower() for s in get_list_env("SLANDER_HINTS", [])]
MCLAREN_TERMS  = [s.lower() for s in get_list_env("MCLAREN_TERMS", ["mclaren","norris","lando norris","stella","zak brown","woking"])]

# Oscar terms (fallback if KEYWORDS is empty)
OSCAR_TERMS = [k for k in KEYWORDS if k] or ["oscar", "piastri", "oscar piastri"]

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
            # allow legacy list format
            if isinstance(data, list):
                migrated = {uid_val: datetime.now().isoformat() for uid_val in data}
                print(f"📦 Migrated {len(migrated)} entries from legacy format")
                return migrated
            print(f"📦 Loaded {len(data)} entries from {SENT_DB}")
            return data
    except FileNotFoundError:
        print(f"📦 No existing {SENT_DB}, starting fresh")
        return {}
    except Exception as e:
        print(f"⚠️ Error loading {SENT_DB}: {e}, starting fresh")
        return {}

def save_sent(sent):
    try:
        with open(SENT_DB, "w", encoding="utf-8") as f:
            json.dump(sent, f, indent=2)
        print(f"💾 Saved {len(sent)} entries to {SENT_DB}")
        dbg(f"Saved {len(sent)} entries to DB")
    except Exception as e:
        dbg(f"Failed to save sent DB: {e}")
        print(f"❌ Failed to save sent DB: {e}")

def cleanup_sent(sent: dict) -> dict:
    if not sent:
        return sent
    cutoff = datetime.now() - timedelta(days=SENT_EXPIRY_DAYS)
    cleaned = {}
    removed = 0
    for uid_, ts in sent.items():
        try:
            d = datetime.fromisoformat(ts)
            if d > cutoff:
                cleaned[uid_] = ts
            else:
                removed += 1
        except Exception:
            # Keep entries with bad timestamps
            cleaned[uid_] = datetime.now().isoformat()
            dbg(f"Fixed bad timestamp for {uid_[:16]}...")
    
    if removed > 0:
        print(f"🧹 Removed {removed} expired entries (older than {SENT_EXPIRY_DAYS} days)")
    
    if len(cleaned) > MAX_SENT_ENTRIES:
        before = len(cleaned)
        cleaned = dict(sorted(cleaned.items(), key=lambda x: x[1], reverse=True)[:MAX_SENT_ENTRIES])
        print(f"🧹 Trimmed sent DB from {before} to {MAX_SENT_ENTRIES} entries")
        dbg(f"Trimmed sent DB to {MAX_SENT_ENTRIES}")
    
    return cleaned

# ============ Helpers ============

def uid(entry):
    """Generate unique ID based on link (most reliable) with fallback."""
    link = getattr(entry, "link", "").strip()
    
    if link:
        # Remove query parameters and fragments for consistent hashing
        clean_link = re.sub(r"[?#].*", "", link)
        entry_id = hashlib.sha256(clean_link.encode("utf-8", "ignore")).hexdigest()
        dbg(f"Generated UID from link: {clean_link[:80]} -> {entry_id[:16]}...")
        return entry_id
    
    # Fallback to entry.id if no link
    entry_id_field = getattr(entry, "id", "").strip()
    if entry_id_field:
        entry_id = hashlib.sha256(entry_id_field.encode("utf-8", "ignore")).hexdigest()
        dbg(f"Generated UID from entry.id: {entry_id_field[:80]} -> {entry_id[:16]}...")
        return entry_id
    
    # Last resort: use title
    title = getattr(entry, "title", "").strip()
    entry_id = hashlib.sha256(title.encode("utf-8", "ignore")).hexdigest()
    dbg(f"Generated UID from title: {title[:80]} -> {entry_id[:16]}...")
    return entry_id

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
    link_l = (link or "").lower()
    for key, emoji in SOURCE_EMOJIS.items():
        if key in link_l:
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

def classify_article(title: str, desc: str) -> dict:
    """
    Returns flags:
      - neg_oscar: negative Oscar-related (skip)
      - slander: McLaren/Norris slander (post with ⚠️ regardless of positivity/keywords)
    """
    blob = f"{title} {desc}".lower()

    neg_hit = contains_any(blob, NEGATIVE_HINTS) if NEGATIVE_HINTS else False
    oscar_hit = contains_any(blob, OSCAR_TERMS)
    slander_hit = contains_any(blob, SLANDER_HINTS) if SLANDER_HINTS else False
    mclaren_hit = contains_any(blob, MCLAREN_TERMS)

    neg_oscar = neg_hit and oscar_hit
    slander = slander_hit and mclaren_hit

    return {"neg_oscar": neg_oscar, "slander": slander}

# ============ Discord send ============

def send_to_discord(title, link, desc=None, img=None, emoji="🦘"):
    clean_link = re.sub(r"\?.*", "", link)
    desc_text = clean_desc(desc or "")
    if desc_text:
        desc_text += f"\n\n🔗 {clean_link}"
    else:
        desc_text = f"🔗 {clean_link}"

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

    if not WEBHOOK:
        print("⚠️ No WEBHOOK set, would have sent:", title)
        dbg(f"No webhook for {title}")
        return False

    try:
        r = requests.post(WEBHOOK, json=data, timeout=10)
        r.raise_for_status()
        print(f"✅ Sent: {title[:60]}")
        dbg(f"✅ Posted: {title}")
        return True
    except Exception as e:
        dbg(f"Discord error: {e}")
        print(f"❌ Discord error: {e}")
        return False

# ============ Feed processing ============

def process_feed(url, sent):
    """Process a single feed and return count of new posts."""
    new_posts = 0
    try:
        dbg(f"Fetching feed: {url}")
        feed = feedparser.parse(url, request_headers=USER_AGENT)
        if getattr(feed, "bozo", 0):
            dbg(f"⚠️ Feed parse warning: {getattr(feed, 'bozo_exception', '')}")

        entries = getattr(feed, "entries", [])
        dbg(f"Found {len(entries)} entries in feed")

        for entry in entries:
            title = (getattr(entry, "title", "") or "").strip()
            link  = (getattr(entry, "link", "") or "").strip()
            if not title or not link:
                dbg("❌ Skipped entry with missing title/link")
                continue

            src   = get_source_name(link)
            emoji = find_source_emoji(link)
            desc  = getattr(entry, "summary", "") or getattr(entry, "description", "")

            # Generate unique ID
            entry_id = uid(entry)
            
            # Duplicate check FIRST (most important)
            if entry_id in sent:
                dbg(f"☑️ [{src}] DUPLICATE (already sent): '{title[:80]}' [ID: {entry_id[:16]}...]")
                continue
            
            dbg(f"🆕 [{src}] NEW entry: '{title[:80]}' [ID: {entry_id[:16]}...]")

            # Classify
            flags = classify_article(title, desc)

            # Oscar-negative -> hard skip
            if flags["neg_oscar"]:
                dbg(f"🚫 [{src}] Oscar negative: '{title[:80]}'")
                continue

            # McLaren/Norris slander -> force post (no sentiment/keyword gating)
            if flags["slander"]:
                out_title = f"‼️ MCLAREN SLANDER ‼️ — {title}"
                img = None
                # try media_content for image (simple)
                if hasattr(entry, "media_content"):
                    for m in entry.media_content:
                        if m.get("url", "").startswith("http"):
                            img = m["url"]
                            break
                ok = send_to_discord(out_title, link, desc, img, emoji)
                if ok:
                    sent[entry_id] = datetime.now().isoformat()
                    save_sent(sent)  # Save immediately after each post
                    new_posts += 1
                    dbg(f"😈 [{src}] Slander posted: '{title[:80]}' - DB saved")
                    time.sleep(DISCORD_RATE_LIMIT_DELAY)
                continue

            # Normal path → must match KEYWORDS (title) + positive sentiment
            if KEYWORDS and not any(k in title.lower() for k in KEYWORDS):
                dbg(f"⏭️ [{src}] Skipped (no keyword): '{title[:80]}'")
                continue

            ok_pol, pol = is_positive(desc or title, POS_THRESHOLD)
            dbg(f"🔍 [{src}] '{title[:80]}' → polarity={pol:.2f} (thr={POS_THRESHOLD})")
            if not ok_pol:
                dbg(f"❌ [{src}] Skipped (negative/neutral): '{title[:80]}'")
                continue
            else:
                dbg(f"✅ [{src}] Positive: '{title[:80]}'")

            # Try simple image extraction from media_content
            img = None
            if hasattr(entry, "media_content"):
                for m in entry.media_content:
                    if m.get("url", "").startswith("http"):
                        img = m["url"]
                        break

            if send_to_discord(title, link, desc, img, emoji):
                sent[entry_id] = datetime.now().isoformat()
                new_posts += 1
                time.sleep(DISCORD_RATE_LIMIT_DELAY)

    except Exception as e:
        dbg(f"💥 Error processing feed {url}: {e}")
        print(f"❌ Error processing feed {url}: {e}")

    dbg(f"Round done for {url} → {new_posts} new post(s)")
    return new_posts

# ============ Main loop ============

def single_check():
    """Run a single check cycle and exit (for cron/GitHub Actions)."""
    # init debug file per run
    if DEBUG:
        try:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"=== Feedaroo Debug Log Started at {datetime.now().isoformat()} ===\n")
            print(f"🐛 Debug mode enabled, logging to {LOG_FILE}")
        except Exception:
            pass

    sent = cleanup_sent(load_sent())
    
    print(f"🦘 {BOT_NAME} started (single check mode).")
    print(f"📊 Tracking {len(sent)} previously sent entries")
    dbg(f"Feeds={len(FEEDS)}, thr={POS_THRESHOLD}, keywords={KEYWORDS}, neg_hints={NEGATIVE_HINTS}, slander_hints={SLANDER_HINTS}")

    total_new = 0
    dbg(f"\n=== Starting feed check cycle at {datetime.now().isoformat()} ===")
    
    for feed_url in FEEDS:
        total_new += process_feed(feed_url, sent)

    # Always save after processing all feeds
    save_sent(sent)
    
    if total_new > 0:
        print(f"🦘 {BOT_NAME}: {total_new} new post(s)!")
        dbg(f"Check complete → {total_new} new")
    else:
        print(f"🦘 {BOT_NAME}: No new posts.")
        dbg("Check complete → 0 new")
    
    print("✅ Single check complete, exiting.")

def loop():
    """Continuous loop mode for local/server deployment."""
    # init debug file per run
    if DEBUG:
        try:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"=== Feedaroo Debug Log Started at {datetime.now().isoformat()} ===\n")
            print(f"🐛 Debug mode enabled, logging to {LOG_FILE}")
        except Exception:
            pass

    sent = cleanup_sent(load_sent())
    
    print(f"🦘 {BOT_NAME} started (continuous mode). Monitoring {len(FEEDS)} feeds.")
    print(f"📊 Tracking {len(sent)} previously sent entries")
    dbg(f"Feeds={len(FEEDS)}, thr={POS_THRESHOLD}, keywords={KEYWORDS}, neg_hints={NEGATIVE_HINTS}, slander_hints={SLANDER_HINTS}")

    while True:
        total_new = 0
        dbg(f"\n=== Starting feed check cycle at {datetime.now().isoformat()} ===")
        for feed_url in FEEDS:
            total_new += process_feed(feed_url, sent)

        # Always save after each cycle
        save_sent(sent)
        
        if total_new > 0:
            print(f"🦘 {BOT_NAME}: {total_new} new post(s)!")
            dbg(f"Round complete → {total_new} new")
        else:
            print(f"🦘 {BOT_NAME}: No new posts.")
            dbg("Round complete → 0 new")

        print(f"💤 Sleeping for {CHECK_INTERVAL // 60} minutes...")
        time.sleep(CHECK_INTERVAL)

# ============ Entry point ============

if __name__ == "__main__":
    try:
        # Check if we should run in single-check mode (for cron/GitHub Actions)
        # If CHECK_INTERVAL is very large, assume single-check mode
        if CHECK_INTERVAL > 100000:
            single_check()
        else:
            loop()
    except KeyboardInterrupt:
        print("\n🛑 Feedaroo stopped.")
        dbg("Stopped manually")
    except Exception as e:
        print(f"\n💥 Feedaroo crashed: {e}")
        dbg(f"Crashed: {e}")
        raise