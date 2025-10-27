# pip install -r requirements.txt
import feedparser, requests, time, hashlib, json, os, re
from textblob import TextBlob

# ============ env.json / env vars ============
def load_env():
    try:
        with open("env.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in data.items():
                os.environ[k] = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
            print("✅ env.json loaded for Feedaroo.")
    except Exception as e:
        print("ℹ️ No/invalid env.json, falling back to OS env vars.", e)

load_env()

def get_list_env(name, default=None):
    raw = os.getenv(name)
    if not raw:
        return default or []
    try:
        return json.loads(raw) if raw.strip().startswith("[") else [s.strip() for s in raw.split(",") if s.strip()]
    except Exception:
        return default or []

# -------- config --------
WEBHOOK        = os.getenv("WEBHOOK", "").strip()
FEEDS          = get_list_env("FEEDS", [])
KEYWORDS       = [k.lower() for k in get_list_env("KEYWORDS", [])]   # címben keres
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "15")) * 60
BOT_NAME       = os.getenv("BOT_NAME", "Feedaroo 🦘")
SENT_DB        = os.getenv("SENT_DB", "sent_feedaroo.json")
POS_THRESHOLD  = float(os.getenv("POS_THRESHOLD", "0.15"))           # -1..+1

USER_AGENT = {"User-Agent": "Feedaroo/2.0 (+discord)"}
EMBED_COLOR = 0xFF9900  # narancs Aussie vibe

# --- debug / fake konzol a logfájlba (artifactként fel tudod szedni) ---
DEBUG    = os.getenv("DEBUG", "0") == "1"
LOG_FILE = os.getenv("LOG_FILE", "feedaroo_debug.log")
def dbg(msg: str):
    if not DEBUG:
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass

# AU bias emojik
SOURCE_EMOJIS = {
    "speedcafe.com":   "🟢",
    "motorsport.com":  "🟡",
    "news.com.au":     "🔵",
    "foxsports.com.au":"🔴",
    "abc.net.au":      "⚪️",
    "theage.com.au":   "🟣",
    "smh.com.au":      "⚫️"
}

# --- helpers ---
def load_sent():
    try:
        with open(SENT_DB, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_sent(s):
    try:
        with open(SENT_DB, "w", encoding="utf-8") as f:
            json.dump(list(s), f)
    except Exception:
        pass

def uid(entry):
    base = getattr(entry, "id", "") or (getattr(entry, "link", "") + getattr(entry, "title", ""))
    return hashlib.sha256(base.encode("utf-8", "ignore")).hexdigest()

def match_title(title: str) -> bool:
    if not KEYWORDS:
        return True
    t = (title or "").lower()
    return any(k in t for k in KEYWORDS)

def extract_image(entry):
    if hasattr(entry, "media_content"):
        for m in entry.media_content:
            if m.get("url", "").startswith("http"):
                return m["url"]
    if hasattr(entry, "media_thumbnail"):
        for m in entry.media_thumbnail:
            if m.get("url", "").startswith("http"):
                return m["url"]
    if hasattr(entry, "links"):
        for l in entry.links:
            if getattr(l, "rel", "") == "enclosure" and str(getattr(l, "type", "")).startswith("image"):
                if getattr(l, "href", "").startswith("http"):
                    return l.href
    html = getattr(entry, "summary", "") or getattr(entry, "description", "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html or "", flags=re.I)
    if m:
        return m.group(1)
    return None

def clean_desc(text):
    text = re.sub("<[^<]+?>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 300:
        text = text[:300].rsplit(" ", 1)[0] + "..."
    return text

def find_source_emoji(url):
    for key, emoji in SOURCE_EMOJIS.items():
        if key in url:
            return emoji
    return "🦘"

def is_positive(text: str, threshold: float) -> tuple[bool, float]:
    if not text:
        return False, 0.0
    try:
        pol = TextBlob(text).sentiment.polarity  # -1..+1
        return pol >= threshold, pol
    except Exception:
        return False, 0.0

def send(title, link, desc=None, img=None, emoji="🦘"):
    embed = {"title": f"{emoji} {title}", "url": link, "color": EMBED_COLOR}
    if desc: embed["description"] = clean_desc(desc)
    if img:  embed["image"] = {"url": img}
    data = {"username": BOT_NAME, "embeds": [embed]}
    if not WEBHOOK:
        print("⚠️ No WEBHOOK set, printing instead:\n", data)
        return
    try:
        requests.post(WEBHOOK, json=data, timeout=10)
    except Exception as e:
        print("Webhook error:", e)

# --- main ---
def loop():
    # induláskor ürítsük a debug logot futásonként
    if DEBUG:
        try: open(LOG_FILE, "w", encoding="utf-8").close()
        except Exception: pass

    sent = load_sent()
    if not FEEDS:
        print("⚠️ FEEDS is empty. Add feeds in env.json / Secrets.")
        dbg("FEEDS empty – nothing to do.")
    while True:
        new = 0
        for url in FEEDS:
            try:
                feed = feedparser.parse(url, request_headers=USER_AGENT)
                emoji = find_source_emoji(url)
                for e in feed.entries:
                    title = getattr(e, "title", "") or ""
                    link  = getattr(e, "link", "") or ""
                    if not title or not link:
                        continue
                    if not match_title(title):
                        dbg(f"skip (keyword): '{title[:80]}'")
                        continue

                    desc = getattr(e, "summary", "") or getattr(e, "description", "")
                    ok, pol = is_positive(desc or title, POS_THRESHOLD)
                    dbg(f"check: '{title[:80]}' → polarity={pol:.2f}")
                    if not ok:
                        dbg(f"❌ skipped (neg/neutral): '{title[:80]}'")
                        continue

                    u = uid(e)
                    if u in sent:
                        dbg(f"skip (dupe): '{title[:80]}'")
                        continue

                    img = extract_image(e)
                    send(title, link, desc, img, emoji)
                    dbg(f"✅ posted: '{title[:80]}' [{emoji}] {link}")
                    sent.add(u)
                    new += 1
            except Exception as ex:
                print(f"Error fetching {url}: {ex}")
                dbg(f"error fetching {url}: {ex}")
        if new:
            save_sent(sent)
            print(f"{BOT_NAME}: {new} new positive biased post(s) 🦘")
            dbg(f"round done → new={new}")
        else:
            print(f"{BOT_NAME}: nothing new")
            dbg("round done → new=0")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    loop()