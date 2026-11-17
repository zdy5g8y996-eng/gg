"""
Бот для GitHub Actions: каждые 15 минут ищет новые клипы стримеров
и публикует ссылки на них в Telegram-канал.

Зависимостей нет — только стандартный Python.
Настройки берутся из секретов репозитория (Settings → Secrets and variables → Actions):
  BOT_TOKEN, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TARGET_CHAT
"""

import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ------------------------------------------------------------- настройки
STREAMERS = [
    # (платформа, логин, как подписывать)
    ("twitch", "marlon", "Marlon"),
    ("twitch", "lacy", "Lacy"),
    ("kick", "n3on", "N3on"),
    ("twitch", "jasontheween", "JasonTheWeen"),
    ("twitch", "stableronaldo", "Stable Ronaldo"),
]

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TARGET_CHAT = os.environ.get("TARGET_CHAT", "")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
MIN_VIEWS = int(os.environ.get("MIN_VIEWS") or 100)      # порог просмотров
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS") or 12)
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN") or 10)   # не спамить за один запуск

STATE_FILE = Path(__file__).parent / "state" / "sent.json"
KEEP_DAYS = 7

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def log(*args):
    print(*args, flush=True)


# ------------------------------------------------------------- http
def http_json(url, *, method="GET", params=None, data=None, headers=None, timeout=20):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    body = None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------- Twitch
class Twitch:
    def __init__(self):
        self.token = None

    def _auth(self):
        if self.token is None:
            r = http_json(
                "https://id.twitch.tv/oauth2/token",
                method="POST",
                params={
                    "client_id": TWITCH_CLIENT_ID,
                    "client_secret": TWITCH_CLIENT_SECRET,
                    "grant_type": "client_credentials",
                },
            )
            self.token = r["access_token"]
        return {"Client-Id": TWITCH_CLIENT_ID, "Authorization": f"Bearer {self.token}"}

    def user_ids(self, logins):
        r = http_json(
            "https://api.twitch.tv/helix/users",
            params=[("login", l) for l in logins],
            headers=self._auth(),
        )
        return {u["login"].lower(): u["id"] for u in r.get("data", [])}

    def clips(self, broadcaster_id, since):
        r = http_json(
            "https://api.twitch.tv/helix/clips",
            params={
                "broadcaster_id": broadcaster_id,
                "started_at": iso(since),
                "ended_at": iso(datetime.now(timezone.utc)),
                "first": 50,
            },
            headers=self._auth(),
        )
        return r.get("data", [])


def twitch_clips(since):
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        log("⚠️ Нет TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET — Twitch пропущен")
        return []
    tw = Twitch()
    wanted = [(login, name) for p, login, name in STREAMERS if p == "twitch"]
    ids = tw.user_ids([l for l, _ in wanted])
    result = []
    for login, name in wanted:
        if login not in ids:
            log(f"⚠️ Twitch-канал {login} не найден")
            continue
        try:
            for c in tw.clips(ids[login], since):
                result.append({
                    "id": f"tw:{c['id']}",
                    "streamer": name,
                    "platform": "Twitch",
                    "title": c.get("title") or "",
                    "url": c["url"],
                    "views": int(c.get("view_count", 0)),
                    "created": c["created_at"],
                    "duration": float(c.get("duration") or 0),
                    "clipper": c.get("creator_name") or "",
                })
        except Exception as e:
            log(f"⚠️ Twitch {login}: {e}")
    return result


# ------------------------------------------------------------- Kick
def kick_clips(since):
    """Официального API клипов у Kick нет; публичный эндпоинт может отвечать 403."""
    result = []
    for platform, login, name in STREAMERS:
        if platform != "kick":
            continue
        try:
            r = http_json(
                f"https://kick.com/api/v2/channels/{login}/clips",
                params={"sort": "view", "time": "day"},
            )
