# pip install -r requirements.txt
# Feedaroo — Oscar-positive news only (telemetry v2 edition)

import feedparser, requests, time, hashlib, json, os, re, traceback
from datetime import datetime, timedelta
from urllib.parse import urlparse
from textblob import TextBlob
from transformers import pipeline
from huggingface_hub import login
from newspaper import Article, Config

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

BLACKLIST = ["full credit to the noise", "crash"]

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
OSCAR_TERMS    = ["piastri", "oscar piastri", "jack doohan"]
HUGGINGFACE    = os.getenv("HUGGINGFACE", "").strip()

print(FEEDS)
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

def is_positive(url, sentiment_analyzer):
    full_text = get_article_text_with_user_agent(url)
    warning = False
    if not full_text:
        return 0, False, False
    try:
        result_osc = sentiment_analyzer(full_text, text_pair="Oscar Piastri")[0]
        label_osc, prob_osc = result_osc["label"], result_osc["score"]
        
        print(label_osc, prob_osc)
        if label_osc == "Positive":
            result_ln = sentiment_analyzer(full_text, text_pair="Lando Norris")[0]
            label_ln, prob_ln = result_ln["label"], result_ln["score"]
            print(label_ln, prob_ln)
            if (label_ln == "Positive") and (prob_ln > prob_osc):
                print("warning: LN favor")
                warning = True
            return prob_osc, True, warning

        result_jack = sentiment_analyzer(full_text, text_pair="Jack Doohan")[0]
        label_jack, prob_jack = result_jack["label"], result_jack["score"]
        if label_jack == "Positive":
            return prob_jack, True, warning
            
        return 0, False, warning
    except:
        return 0, False, False

def contains_any(blob, terms):
    return any(t in blob for t in terms if t)

def classify_article(title, desc):
    blob = f"{title} {desc}".lower()
    return contains_any(blob, BLACKLIST)

def get_article_text_with_user_agent(url):
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
    config = Config()
    config.browser_user_agent = user_agent
    config.request_timeout = 7
    article = Article(url, config=config)
    try:
        article.download()
        article.parse()
        return article.text
    except Exception as e:
        print(f"An error occurred: {e}")
        return None

# ============ Send ============
def send_to_discord(title, link, desc=None, img=None, emoji="🦘", warning=False):
    clean_link = re.sub(r"\?.*", "", link)
    desc_text = f"{clean_desc(desc)}\n\n🔗 {clean_link}"

    if warning == False:
        content = None
    else:
        content = "⚠️ May Contain Norris Favouritism ⚠️"
        
    embed = {
        "title": f"{emoji} {title}"[:256],
        "url": link,
        "description": desc_text[:4096],
        "color": EMBED_COLOR,
        "timestamp": datetime.utcnow().isoformat()
    }

    if img:
        embed["image"] = {"url": img}
    print(title[:256], "\n", desc_text[:600])
    requests.post(WEBHOOK, json={"username": BOT_NAME, "content":content, "embeds": [embed]}, timeout=10)

# ============ Process ============
def process_feed(url, sent, stats, sentiment_analyzer):
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
            
        if OSCAR_TERMS and not any(k in title.lower() for k in OSCAR_TERMS):
            stats["keyword_miss"] += 1
            continue
        print('hit: ',title)
            
        if classify_article(title, desc):
            stats["blacklist"] += 1
            print('blacklist')
            continue
            
        prob, is_pos, warning = is_positive(link, sentiment_analyzer)
        if warning:
            stats["LN_bias"] += 1
            
        if not is_pos:
            stats["oscar_negative"] += 1
            sent[entry_id] = datetime.now().isoformat()
            continue

        if prob<0.6:
            stats["oscar_not_pos"] += 1
            sent[entry_id] = datetime.now().isoformat()
            continue
        
        send_to_discord(title, link, desc, None, emoji, warning)
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
        oscar_negative = stats["oscar_negative"]
        dupes = stats["dupes"]
        keyword_miss = stats["keyword_miss"]
        blacklist = stats["blacklist"]
        LN_bias = stats["LN_bias"]
        oscar_not_pos = stats["oscar_not_pos"]

        dupes_line = f"☑️ Duplicates: {dupes}" if dupes > 0 else "☑️ No duplicates found"
        blacklist_line = f"🚨 Blacklisted: {blacklist}" if blacklist > 0 else "🚨 No blacklisted terms found"
        neg_line = f"🚫 Oscar Negative: {oscar_negative}" if oscar_negative > 0 else "🚫 No negative articles found"
        iffy_line = f"❗Not Postive Enough: {oscar_not_pos}" if oscar_not_pos > 0 else "❗No less positive articles found"
        bias_line = f"⚠️ Lando Bias: {LN_bias}" if LN_bias > 0 else "⚠️ No articles favouring Lando found"
        mem_line = (
            f"🧠 Memory updated — **{posted} new** entries saved (Articles in memory: {memory_count})"
            if posted > 0 else
            f"🧠 Memory clean. No new articles. ({memory_count} already saved.)"
        )

        msg = (
            f"🕒 **Telemetry Report: Feedaroo ({run_type} Run)**\n"
            f"Feeds checked: {stats['feeds']}\n"
            f"Total entries checked: {stats['entries']}\n"
            f"✅ Posted: {posted}\n"
            f"{dupes_line}\n"
            f"#️⃣ No Keyword Match: {keyword_miss}\n"
            f"{blacklist_line}\n"
            f"{neg_line}\n"
            f"{iffy_line}\n"
            f"{bias_line}\n"
            f"{mem_line}\n\n"
            "*Copy that, Feedaroo. Telemetry clean, keep going.*"
        )
        msg += "\n_  _"

    requests.post(webhook, json={"content": msg}, timeout=10)

# ============ Run ============
def single_check():
    sent = cleanup_sent(load_sent())
    login(token=HUGGINGFACE)
    # print("sent", sent)
    stats = {"feeds": len(FEEDS), "entries": 0, "posted": 0, "oscar_negative": 0, "dupes": 0, "keyword_miss": 0,"blacklist":0, "LN_bias": 0, "oscar_not_pos": 0}
    run_type = "Manual" if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" else "Scheduled"

    sentiment_analyzer = pipeline("text-classification", model="yangheng/deberta-v3-large-absa-v1.1")
    try:
        for feed_url in FEEDS:
            sent = process_feed(feed_url, sent, stats, sentiment_analyzer)
        # print("returned sent", sent)
        save_sent(sent)
        memory_count = len(sent)
        status = "success" if stats["posted"] > 0 else "idle"
        send_telemetry(stats, run_type, memory_count, status)
        
        sent = cleanup_sent(load_sent())
        # print("final sent", sent)
    except Exception as e:
        err_text = str(e).split("\n")[0]
        send_telemetry(stats, run_type, len(sent), status="fail", error=err_text)
        traceback.print_exc()

if __name__ == "__main__":
    single_check()
