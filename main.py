# pip install -r requirements.txt

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
from typing import Set, List, Optional, Tuple

# ============ Constants ============

USER_AGENT = {“User-Agent”: “Feedaroo/2.0 (+https://github.com/feedaroo)”}
EMBED_COLOR = 0xFF9900
MAX_DESC_LENGTH = 300
MAX_SENT_ENTRIES = 10000
SENT_EXPIRY_DAYS = 30
DISCORD_RATE_LIMIT_DELAY = 2  # seconds between posts

SOURCE_EMOJIS = {
“speedcafe.com”: “🟢”,
“motorsport.com”: “🟡”,
“news.com.au”: “🔵”,
“foxsports.com.au”: “🔴”,
“abc.net.au”: “⚪️”,
“theage.com.au”: “🟣”,
“smh.com.au”: “⚫️”
}

# ============ Configuration ============

def load_env():
“”“Load environment variables from env.json if available.”””
try:
with open(“env.json”, “r”, encoding=“utf-8”) as f:
data = json.load(f)
for k, v in data.items():
os.environ[k] = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
print(“✅ env.json loaded for Feedaroo.”)
except FileNotFoundError:
print(“ℹ️ No env.json found, using OS environment variables.”)
except json.JSONDecodeError as e:
print(f”⚠️ Invalid env.json format: {e}”)
except Exception as e:
print(f”⚠️ Error loading env.json: {e}”)

def get_list_env(name: str, default: Optional[List] = None) -> List[str]:
“”“Parse environment variable as list.”””
raw = os.getenv(name)
if not raw:
return default or []
try:
return json.loads(raw) if raw.strip().startswith(”[”) else [s.strip() for s in raw.split(”,”) if s.strip()]
except json.JSONDecodeError:
print(f”⚠️ Failed to parse {name} as JSON, trying CSV”)
return [s.strip() for s in raw.split(”,”) if s.strip()]

load_env()

# Configuration

WEBHOOK = os.getenv(“WEBHOOK”, “”).strip()
FEEDS = get_list_env(“FEEDS”, [])
KEYWORDS = [k.lower() for k in get_list_env(“KEYWORDS”, [])]
CHECK_INTERVAL = int(os.getenv(“CHECK_INTERVAL_MINUTES”, “15”)) * 60
BOT_NAME = os.getenv(“BOT_NAME”, “Feedaroo 🦘”)
SENT_DB = os.getenv(“SENT_DB”, “sent_feedaroo.json”)
POS_THRESHOLD = float(os.getenv(“POS_THRESHOLD”, “0.15”))
DEBUG = os.getenv(“DEBUG”, “0”) == “1”
LOG_FILE = os.getenv(“LOG_FILE”, “feedaroo_debug.log”)

# ============ Debug Logging ============

def dbg(msg: str):
“”“Write debug message to log file.”””
if not DEBUG:
return
try:
timestamp = datetime.now().strftime(”%Y-%m-%d %H:%M:%S”)
with open(LOG_FILE, “a”, encoding=“utf-8”) as f:
f.write(f”[{timestamp}] {msg.rstrip()}\n”)
except Exception as e:
print(f”⚠️ Debug log error: {e}”)

# ============ Database Functions ============

def load_sent() -> dict:
“”“Load sent entries database with timestamps.”””
try:
with open(SENT_DB, “r”, encoding=“utf-8”) as f:
data = json.load(f)
# Support old format (list) and new format (dict)
if isinstance(data, list):
# Convert old format to new format
return {uid: datetime.now().isoformat() for uid in data}
return data
except FileNotFoundError:
dbg(“No sent database found, starting fresh”)
return {}
except json.JSONDecodeError as e:
print(f”⚠️ Corrupted sent database: {e}”)
dbg(f”Corrupted sent database: {e}”)
return {}

def save_sent(sent: dict):
“”“Save sent entries database.”””
try:
with open(SENT_DB, “w”, encoding=“utf-8”) as f:
json.dump(sent, f, indent=2)
except Exception as e:
print(f”⚠️ Failed to save sent database: {e}”)
dbg(f”Failed to save sent database: {e}”)

def cleanup_sent(sent: dict) -> dict:
“”“Remove old entries and limit database size.”””
if not sent:
return sent

```
cutoff = datetime.now() - timedelta(days=SENT_EXPIRY_DAYS)
cleaned = {}

for uid, timestamp in sent.items():
    try:
        entry_date = datetime.fromisoformat(timestamp)
        if entry_date > cutoff:
            cleaned[uid] = timestamp
    except (ValueError, TypeError):
        # Keep entries with invalid timestamps (don't lose data)
        cleaned[uid] = datetime.now().isoformat()

# If still too large, keep only most recent entries
if len(cleaned) > MAX_SENT_ENTRIES:
    sorted_entries = sorted(cleaned.items(), key=lambda x: x[1], reverse=True)
    cleaned = dict(sorted_entries[:MAX_SENT_ENTRIES])
    dbg(f"Trimmed sent database to {MAX_SENT_ENTRIES} entries")

if len(cleaned) < len(sent):
    dbg(f"Cleaned {len(sent) - len(cleaned)} old entries from database")

return cleaned
```

# ============ Entry Processing ============

def uid(entry) -> str:
“”“Generate unique ID for feed entry.”””
base = getattr(entry, “id”, “”) or (getattr(entry, “link”, “”) + getattr(entry, “title”, “”))
return hashlib.sha256(base.encode(“utf-8”, “ignore”)).hexdigest()

def match_title(title: str) -> bool:
“”“Check if title matches any keywords.”””
if not KEYWORDS:
return True
t = (title or “”).lower()
return any(k in t for k in KEYWORDS)

def extract_image(entry) -> Optional[str]:
“”“Extract image URL from feed entry.”””
# Try media_content
if hasattr(entry, “media_content”):
for m in entry.media_content:
url = m.get(“url”, “”)
if url.startswith(“http”):
return url

```
# Try media_thumbnail
if hasattr(entry, "media_thumbnail"):
    for m in entry.media_thumbnail:
        url = m.get("url", "")
        if url.startswith("http"):
            return url

# Try enclosure links
if hasattr(entry, "links"):
    for link in entry.links:
        if getattr(link, "rel", "") == "enclosure" and str(getattr(link, "type", "")).startswith("image"):
            url = getattr(link, "href", "")
            if url.startswith("http"):
                return url

# Try parsing HTML for img tags
html = getattr(entry, "summary", "") or getattr(entry, "description", "")
if html:
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)
    if match:
        return match.group(1)

return None
```

def clean_desc(text: str) -> str:
“”“Clean and truncate description text.”””
if not text:
return “”

```
# Remove HTML tags
text = re.sub("<[^<]+?>", "", text)
# Normalize whitespace
text = re.sub(r"\s+", " ", text).strip()

# Truncate if too long
if len(text) > MAX_DESC_LENGTH:
    text = text[:MAX_DESC_LENGTH].rsplit(" ", 1)[0] + "..."

return text
```

def get_source_name(link: str) -> str:
“”“Extract source name from URL.”””
try:
host = urlparse(link).netloc
return host.replace(“www.”, “”)
except Exception:
return “unknown”

def find_source_emoji(link: str) -> str:
“”“Find emoji for source based on URL.”””
link_lower = link.lower()
for key, emoji in SOURCE_EMOJIS.items():
if key in link_lower:
return emoji
return “🦘”

def is_positive(text: str, threshold: float) -> Tuple[bool, float]:
“”“Check if text has positive sentiment.”””
if not text:
return False, 0.0
try:
pol = TextBlob(text).sentiment.polarity  # -1..+1
return pol >= threshold, pol
except Exception as e:
dbg(f”Sentiment analysis error: {e}”)
return False, 0.0

# ============ Discord Webhook ============

def send_to_discord(title: str, link: str, desc: Optional[str] = None, img: Optional[str] = None, emoji: str = “🦘”):
“”“Send article to Discord webhook.”””
# Clean link (remove query params)
clean_link = re.sub(r”?.*”, “”, link)

```
# Build description
desc_text = clean_desc(desc or "")
if desc_text:
    desc_text += f"\n\n🔗 {clean_link}"
else:
    desc_text = f"🔗 {clean_link}"

# Build embed
embed = {
    "title": f"{emoji} {title}"[:256],  # Discord title limit
    "url": link,
    "description": desc_text[:4096],  # Discord description limit
    "color": EMBED_COLOR,
    "timestamp": datetime.utcnow().isoformat()
}

if img:
    embed["image"] = {"url": img}

data = {
    "username": BOT_NAME,
    "embeds": [embed]
}

if not WEBHOOK:
    print("⚠️ No WEBHOOK set, would have sent:")
    print(f"   {emoji} {title[:60]}")
    dbg(f"No webhook - would send: {title}")
    return False

try:
    r = requests.post(WEBHOOK, json=data, timeout=10)
    r.raise_for_status()
    print(f"✅ Sent: {title[:60]}")
    dbg(f"✅ Posted: {title}")
    return True
except requests.exceptions.HTTPError as e:
    if e.response.status_code == 429:
        print("⚠️ Discord rate limit hit, slowing down...")
        dbg("Rate limit hit")
        time.sleep(10)
    else:
        print(f"❌ Webhook HTTP error: {e.response.status_code}")
        dbg(f"Webhook error: {e}")
    return False
except Exception as e:
    print(f"❌ Webhook error: {e}")
    dbg(f"Webhook error: {e}")
    return False
```

# ============ Main Loop ============

def process_feed(url: str, sent: dict) -> int:
“”“Process a single feed and return count of new posts.”””
new_posts = 0

```
try:
    dbg(f"Fetching feed: {url}")
    feed = feedparser.parse(url, request_headers=USER_AGENT)
    
    if feed.bozo:
        dbg(f"Feed parse warning for {url}: {feed.bozo_exception}")
    
    if not hasattr(feed, 'entries'):
        print(f"⚠️ No entries found in feed: {url}")
        dbg(f"No entries in feed: {url}")
        return 0
    
    dbg(f"Found {len(feed.entries)} entries in {url}")
    
    for entry in feed.entries:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        
        if not title or not link:
            dbg("Skipping entry with missing title or link")
            continue
        
        src = get_source_name(link)
        emoji = find_source_emoji(link)
        
        # Check keywords
        if not match_title(title):
            dbg(f"#️⃣ [{src}] Keyword skip: '{title[:80]}'")
            continue
        
        # Check sentiment
        desc = getattr(entry, "summary", "") or getattr(entry, "description", "")
        ok, pol = is_positive(desc or title, POS_THRESHOLD)
        dbg(f"🔍 [{src}] '{title[:80]}' → polarity={pol:.2f}")
        
        if not ok:
            dbg(f"❌ [{src}] Negative/neutral: '{title[:80]}'")
            continue
        
        # Check if already sent
        entry_uid = uid(entry)
        if entry_uid in sent:
            dbg(f"☑️ [{src}] Duplicate: '{title[:80]}'")
            continue
        
        # Send to Discord
        img = extract_image(entry)
        if send_to_discord(title, link, desc, img, emoji):
            sent[entry_uid] = datetime.now().isoformat()
            new_posts += 1
            time.sleep(DISCORD_RATE_LIMIT_DELAY)  # Rate limit protection
    
except Exception as e:
    print(f"❌ Error processing feed {url}: {e}")
    dbg(f"Error processing feed {url}: {e}")

return new_posts
```

def loop():
“”“Main loop - continuously check feeds.”””
# Initialize debug log
if DEBUG:
try:
with open(LOG_FILE, “w”, encoding=“utf-8”) as f:
f.write(f”=== Feedaroo Started at {datetime.now()} ===\n”)
except Exception:
pass

```
# Validate configuration
if not FEEDS:
    print("⚠️ FEEDS is empty. Add feeds in env.json or environment variables.")
    print("   Example: FEEDS=['https://example.com/feed.xml']")
    dbg("FEEDS empty - exiting")
    return

print(f"🦘 {BOT_NAME} starting...")
print(f"   Monitoring {len(FEEDS)} feed(s)")
print(f"   Keywords: {KEYWORDS if KEYWORDS else 'ALL'}")
print(f"   Positive threshold: {POS_THRESHOLD}")
print(f"   Check interval: {CHECK_INTERVAL // 60} minutes")
if DEBUG:
    print(f"   Debug logging: {LOG_FILE}")

dbg(f"Config: {len(FEEDS)} feeds, keywords={KEYWORDS}, threshold={POS_THRESHOLD}")

sent = load_sent()
sent = cleanup_sent(sent)
save_sent(sent)

print(f"   Loaded {len(sent)} previously sent entries\n")

iteration = 0
while True:
    iteration += 1
    dbg(f"\n=== Iteration {iteration} at {datetime.now()} ===")
    
    total_new = 0
    for feed_url in FEEDS:
        new = process_feed(feed_url, sent)
        total_new += new
    
    # Save database after each round
    if total_new > 0:
        save_sent(sent)
        print(f"🦘 {BOT_NAME}: {total_new} new positive post(s) sent!\n")
        dbg(f"Round complete: {total_new} new posts")
    else:
        print(f"🦘 {BOT_NAME}: No new posts this round\n")
        dbg("Round complete: no new posts")
    
    # Periodic cleanup
    if iteration % 10 == 0:
        sent = cleanup_sent(sent)
        save_sent(sent)
    
    # Wait before next check
    dbg(f"Sleeping for {CHECK_INTERVAL} seconds")
    time.sleep(CHECK_INTERVAL)
```

# ============ Entry Point ============

if **name** == “**main**”:
try:
loop()
except KeyboardInterrupt:
print(”\n🛑 Feedaroo stopped by user”)
dbg(“Stopped by user”)
except Exception as e:
print(f”\n💥 Fatal error: {e}”)
dbg(f”Fatal error: {e}”)
raise
