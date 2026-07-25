# pip install -r requirements.txt
# Feedaroo — Oscar-positive news only (telemetry v2 edition)

import feedparser, requests, time, hashlib, json, os, re, traceback
from datetime import datetime, timedelta

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


# ============ Send ============
def send_to_discord():
    content = "Caution: 🦘 Feedaroo had an issue with his cache...\nHe may need help, idk"
    requests.post(WEBHOOK, json={"username": BOT_NAME, "content":content}, timeout=10)
    time.sleep(DISCORD_RATE_LIMIT_DELAY)

# ============ Telemetry ============
def send_telemetry():
    webhook = LOG_WEBHOOK
    if not webhook:
        return
    msg = (
            f"🕒 **Telemetry Report: Feedaroo ({run_type} Run)**\n"
            f"⚠️ Feedaroo encountered an error loading cache.\n\n"
            "*Copy that, Feedaroo. Standing by for next run.*"
        )
    msg += "\n_  _"
    requests.post(webhook, json={"content": msg}, timeout=10)

if __name__ == "__main__":
    send_to_discord()
    send_telemetry()
