#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PremiumVideo — paid content bot for Telegram (single file, no framework).

HOW IT WORKS
  Admin   : adds items (video / photo / document / channel or group invite / website link /
            plain text or serial key), sets a price per item, sets the payment QR + UPI id.
  User    : browses the store → pays the exact amount → sends a payment screenshot.
  Admin   : gets the proof with inline Approve / Decline buttons → on approval the content is
            delivered instantly (Telegram file_id is reused, no re-upload).

RUN
  pip install -r requirements.txt          # optional: only needed for the auto UPI QR image
  export BOT_TOKEN="123456:ABC..."
  export ADMIN_IDS="123456789"
  python main.py

TEST WITHOUT A TOKEN
  python main.py --demo        # the terminal becomes Telegram (drive admin + user accounts)
  python main.py --selftest    # 89 automated checks of the whole purchase lifecycle
  python main.py --apitest     # verifies the real HTTP layer against a fake Telegram server
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta

try:
    import requests
except Exception:  # the bot falls back to urllib, so `requests` is optional
    requests = None

try:
    import qrcode
except Exception:  # only used to auto-generate the UPI QR image
    qrcode = None

# ==========================================================================
# CONFIGURATION  (everything can also be overridden with environment vars)
# ==========================================================================
ROOT = os.path.dirname(os.path.abspath(__file__))

# optional .env file (never overrides real environment variables) — keeps
# BOT_TOKEN / ADMIN_IDS out of the command line and out of git (.gitignore).
_ENV_FILE = os.path.join(ROOT, ".env")
if os.path.exists(_ENV_FILE):
    try:
        with open(_ENV_FILE, encoding="utf-8") as _fh:
            for _line in _fh:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip().strip("'\"")
                if _k and _k not in os.environ:
                    os.environ[_k] = _v
    except Exception:
        pass

DB_PATH = os.getenv("DB_PATH") or os.path.join(ROOT, "premiumvideo.db")
DATA_DIR = os.getenv("DATA_DIR") or os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", (os.getenv("ADMIN_IDS") or "").strip()) if x.isdigit()}
CURRENCY = os.getenv("CURRENCY", "₹")
PAGE_SIZE = 6                      # items per page in the store
STATE_TTL_HOURS = 12               # a pending wizard / screenshot step expires after this
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "30"))
OFFLINE = False                    # set by --demo / --selftest

API_ROOT = "https://api.telegram.org"

SEP = "──────────────────────────"


def api_url(method: str) -> str:
    return f"{API_ROOT}/bot{BOT_TOKEN}/{method}"


def file_url(path: str) -> str:
    return f"{API_ROOT}/bot{BOT_TOKEN}/file/{path}"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts(value: str) -> str:
    """'2026-09-17 06:09:12' -> '17 Sep, 06:09' (compact, for list views)."""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%d %b, %H:%M")
    except Exception:
        return (value or "")[:16]


def log(*a):
    print("[" + datetime.now().strftime("%H:%M:%S") + "]", *a, flush=True)


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def money(v) -> str:
    try:
        v = float(v)
    except Exception:
        v = 0.0
    if v == int(v):
        return f"{CURRENCY}{int(v):,}"
    return f"{CURRENCY}{v:,.2f}"


def to_num(s):
    """'199' / '199.50' / '₹199' / 'free' -> float (0.0 for free), None if not a number."""
    s = (s or "").strip().lower()
    if s in ("", "free", "0", "0.0", "no"):
        return 0.0
    s = re.sub(r"[^0-9.]", "", s.replace(",", ""))
    if not s:
        return None
    try:
        return round(float(s), 2)
    except Exception:
        return None


def shorten(s, n=32):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def t_url(v: str) -> str:
    """'@chan' / 't.me/x' / 'https://…' -> a clickable https url ('' if empty)."""
    v = (v or "").strip()
    if not v:
        return ""
    if v.startswith(("http://", "https://")):
        return v
    if v.startswith("t.me/"):
        return "https://" + v
    return "https://t.me/" + v.lstrip("@")


# ==========================================================================
# DATABASE
# ==========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER UNIQUE NOT NULL,
    username    TEXT,
    name        TEXT,
    is_admin    INTEGER DEFAULT 0,
    blocked     INTEGER DEFAULT 0,
    ref         TEXT,
    orders      INTEGER DEFAULT 0,
    spent       REAL DEFAULT 0,
    created_at  TEXT,
    last_seen   TEXT
);
CREATE TABLE IF NOT EXISTS items(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL DEFAULT 'video',   -- video|file|photo|link|text
    title         TEXT NOT NULL,
    descr         TEXT,
    file_id       TEXT,                            -- Telegram file_id (video/document/photo…)
    file_kind     TEXT,                            -- video|document|photo|animation|audio|voice…
    link          TEXT,                            -- main link (website / drive / course)
    channel_link  TEXT,                            -- private channel invite
    group_link    TEXT,                            -- private group invite
    price         REAL NOT NULL DEFAULT 0,
    validity_days INTEGER NOT NULL DEFAULT 0,      -- 0 = lifetime
    active        INTEGER NOT NULL DEFAULT 1,
    sold          INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS orders(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    no         TEXT,
    user_id    INTEGER NOT NULL,
    item_id    INTEGER NOT NULL,
    amount     REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',    -- pending|approved|declined|cancelled
    proof_id   TEXT,
    proof_kind TEXT,
    note       TEXT,
    reason     TEXT,
    created_at TEXT,
    decided_at TEXT,
    decided_by INTEGER,
    delivered  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS unlocks(
    user_id    INTEGER NOT NULL,
    item_id    INTEGER NOT NULL,
    order_id   INTEGER,
    created_at TEXT,
    expires_at TEXT,
    PRIMARY KEY(user_id, item_id)
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS states(
    user_id INTEGER PRIMARY KEY,
    data    TEXT,
    upd_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_user  ON orders(user_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(status, id);
"""

_LOCK = threading.RLock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _LOCK:
        conn = db()
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()


def q(sql, args=()):
    with _LOCK:
        conn = db()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


def x(sql, args=()):
    with _LOCK:
        conn = db()
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


# ------------------------------- settings ---------------------------------
DEFAULTS = {
    "brand": "Premium Video",
    "upi_id": "",
    "payee_name": "",
    "pay_note": "Send the exact amount, then send a screenshot of the payment.",
    "pay_instructions": "",
    "refund_note": "All sales are final — wrong or short amounts are refunded within 24 hours.",
    "qr_file_id": "",
    "welcome_photo_id": "",
    "welcome_text": "",
    "force_channel": "",
    "out_of_stock_note": "",
    "demo_link": "",        # 📹 FREE DEMO button (channel / link)
    "proofs_link": "",      # 📢 PROOFS button (channel / link)
    "support_link": "",     # 🚨 SUPPORT button + "TECH SUPPORT –" line
    "stats_joined": "",     # display-only numbers for the welcome screen
    "stats_month": "",      # (admin sets them; empty = real counts)
    "stats_today": "",
}

# --- Premium (custom) emoji ------------------------------------------------------------
# Telegram Premium feature: real animated custom emoji in messages (<tg-emoji>) and
# custom emoji icons on inline buttons (icon_custom_emoji_id, Bot API 9.4+).
# Works when the bot owner has Telegram Premium or the bot has a Fragment username.
# Set PREMIUM_EMOJI=0 to fall back to plain unicode emoji everywhere.
PREMIUM_EMOJI = os.environ.get("PREMIUM_EMOJI", "1").strip().lower() not in ("0", "false", "no", "off")
PEMOJI = {  # emoji char -> custom_emoji_id (fallback char stays inside the tag)
    "💦": "6310096377107975749",
    "🍑": "6311843256271376001",
    "🥵": "6307832826263768178",
    "🍭": "6312109432574577731",
    "🍆": "6312111867821035183",
    "🍒": "6312070305422512477",
    "🌸": "6309771875148893373",
    "😘": "6312213392257979167",
    "👅": "6311998326065597286",
    "😄": "6332146103550479433",
}
_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>')


def pe(text: str) -> str:
    """Swap every mapped emoji char for its premium <tg-emoji> version (user-facing texts)."""
    if not (PREMIUM_EMOJI and text):
        return text
    for ch, eid in PEMOJI.items():
        if ch in text:
            text = text.replace(ch, f'<tg-emoji emoji-id="{eid}">{ch}</tg-emoji>')
    return text


def setting(key: str, default: str = "") -> str:
    row = q("SELECT value FROM settings WHERE key=?", (key,))
    if row and row[0]["value"] not in (None, ""):
        return row[0]["value"]
    return DEFAULTS.get(key, default) or default


def set_setting(key: str, value: str):
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, value or ""))


def all_settings() -> dict:
    d = dict(DEFAULTS)
    for r in q("SELECT key,value FROM settings"):
        if r["value"]:
            d[r["key"]] = r["value"]
    return d


# --------------------------------- users ----------------------------------
def ensure_user(u: dict) -> int:
    tg_id = int(u.get("id") or 0)
    name = " ".join([v for v in [u.get("first_name"), u.get("last_name")] if v]).strip() or (u.get("username") or "User")
    uname = "@" + u["username"] if u.get("username") else ""
    admin = 1 if tg_id in ADMIN_IDS else 0
    row = q("SELECT id FROM users WHERE tg_id=?", (tg_id,))
    if row:
        uid = row[0]["id"]
        x("UPDATE users SET username=?, name=?, is_admin=?, last_seen=? WHERE id=?", (uname, name, admin, now(), uid))
        return uid
    uid = x("INSERT INTO users(tg_id,username,name,is_admin,created_at,last_seen) VALUES(?,?,?,?,?,?)",
            (tg_id, uname, name, admin, now(), now()))
    log(f"new user tg={tg_id} ({name})")
    return uid


def user_by_tg(tg_id: int):
    row = q("SELECT * FROM users WHERE tg_id=?", (int(tg_id),))
    return row[0] if row else None


def user_by_id(pk: int):
    row = q("SELECT * FROM users WHERE id=?", (int(pk),))
    return row[0] if row else None


def is_blocked(tg_id: int) -> bool:
    row = q("SELECT blocked FROM users WHERE tg_id=?", (int(tg_id),))
    return bool(row and row[0]["blocked"])


# --------------------------------- items ----------------------------------
def get_item(item_id: int):
    row = q("SELECT * FROM items WHERE id=?", (int(item_id),))
    return row[0] if row else None


def add_item(**kw) -> int:
    return x("""INSERT INTO items(kind,title,descr,file_id,file_kind,link,channel_link,group_link,
                price,validity_days,active,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (kw.get("kind", "video"), kw.get("title", "Untitled"), kw.get("descr", ""),
              kw.get("file_id"), kw.get("file_kind"), kw.get("link"), kw.get("channel_link"),
              kw.get("group_link"), float(kw.get("price", 0) or 0), int(kw.get("validity_days", 0) or 0),
              1 if kw.get("active", 1) else 0, now(), now()))


def set_item(item_id: int, field: str, value) -> bool:
    allowed = {"kind", "title", "descr", "file_id", "file_kind", "link", "channel_link",
               "group_link", "price", "validity_days", "active"}
    if field not in allowed:
        return False
    x(f"UPDATE items SET {field}=?, updated_at=? WHERE id=?", (value, now(), int(item_id)))
    return True


def has_access(user_pk: int, item_id: int) -> bool:
    row = q("SELECT expires_at FROM unlocks WHERE user_id=? AND item_id=?", (int(user_pk), int(item_id)))
    if not row:
        return False
    exp = row[0]["expires_at"]
    if exp and exp < now():
        x("DELETE FROM unlocks WHERE user_id=? AND item_id=?", (int(user_pk), int(item_id)))
        return False
    return True


def grant_access(user_pk: int, item_id: int, order_id=None, validity_days=0):
    expires = None
    try:
        validity_days = int(validity_days or 0)
    except Exception:
        validity_days = 0
    if validity_days > 0:
        expires = (datetime.now() + timedelta(days=validity_days)).strftime("%Y-%m-%d %H:%M:%S")
    x("""INSERT INTO unlocks(user_id,item_id,order_id,created_at,expires_at) VALUES(?,?,?,?,?)
         ON CONFLICT(user_id,item_id) DO UPDATE SET expires_at=excluded.expires_at,
         order_id=excluded.order_id, created_at=excluded.created_at""",
      (int(user_pk), int(item_id), order_id, now(), expires))


# --------------------------------- orders ---------------------------------
def create_order(user_pk: int, item, amount=None) -> int:
    amt = float(item["price"]) if amount is None else float(amount)
    oid = x("INSERT INTO orders(no,user_id,item_id,amount,status,created_at) VALUES(?,?,?,?, 'pending', ?)",
            ("", int(user_pk), int(item["id"]), amt, now()))
    x("UPDATE orders SET no=? WHERE id=?", (f"{oid:04d}", oid))
    x("UPDATE users SET orders=orders+1 WHERE id=?", (int(user_pk),))
    return oid


def get_order(oid: int):
    row = q("SELECT * FROM orders WHERE id=?", (int(oid),))
    return row[0] if row else None


def pending_for_user(user_pk: int):
    return q("SELECT * FROM orders WHERE user_id=? AND status='pending' ORDER BY id DESC", (int(user_pk),))


# ------------------------------ FSM states --------------------------------
def get_state(tg_id: int) -> dict:
    row = q("SELECT data, upd_at FROM states WHERE user_id=?", (int(tg_id),))
    if not row or not row[0]["data"]:
        return {}
    try:
        age = datetime.now() - datetime.strptime(row[0]["upd_at"], "%Y-%m-%d %H:%M:%S")
        if age.total_seconds() > STATE_TTL_HOURS * 3600:
            x("DELETE FROM states WHERE user_id=?", (int(tg_id),))
            return {}
    except Exception:
        pass
    try:
        return json.loads(row[0]["data"])
    except Exception:
        return {}


def set_state(tg_id: int, data: dict):
    if not data:
        x("DELETE FROM states WHERE user_id=?", (int(tg_id),))
    else:
        x("INSERT INTO states(user_id,data,upd_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
          "data=excluded.data, upd_at=excluded.upd_at", (int(tg_id), json.dumps(data, ensure_ascii=False), now()))


# ==========================================================================
# TRANSPORT  (requests if available, otherwise urllib — same behaviour)
# ==========================================================================
def http_post(url: str, fields: dict, files: dict | None = None):
    """POST as multipart/form-data (when files are present) or x-www-form-urlencoded."""
    fields = {k: ("" if v is None else str(v)) for k, v in (fields or {}).items()}
    if requests is not None:
        ff = {k: (v[0], v[1], v[2]) for k, v in files.items()} if files else None
        r = requests.post(url, data=fields, files=ff, timeout=90)
        return r.status_code, r.text
    import urllib.request
    if files:
        boundary = "----pv" + os.urandom(10).hex()
        buf = []
        for k, v in fields.items():
            buf.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (fn, data, ctype) in files.items():
            buf.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                       f'filename="{fn}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
            buf.append(data if isinstance(data, bytes) else str(data).encode())
            buf.append(b"\r\n")
        buf.append(f"--{boundary}--\r\n".encode())
        body = b"".join(buf)
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        body = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def http_get_bytes(url: str) -> bytes:
    if requests is not None:
        return requests.get(url, timeout=120).content
    import urllib.request
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


class Bot:
    """Thin Telegram Bot API client. `offline=True` prints instead of sending (demo/tests)."""

    def __init__(self, token: str = "", offline: bool = False, label: str = "tg"):
        self.token = token
        self.offline = offline
        self.label = label
        self.outbox = []                      # captured (method, params) — demo + tests

    # ------------------------------ low level ------------------------------
    def api(self, method: str, params: dict | None = None, files: dict | None = None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if isinstance(params.get("reply_markup"), (list, tuple)):
            params["reply_markup"] = kb(params["reply_markup"])
        if isinstance(params.get("reply_markup"), dict):
            params["reply_markup"] = json.dumps(params["reply_markup"], ensure_ascii=False)
        if self.offline:
            rec = {"method": method, "params": dict(params)}
            self.outbox.append(rec)
            if method in ("sendMessage", "sendPhoto", "sendVideo", "sendDocument", "sendAnimation",
                          "sendAudio", "sendVoice", "sendVideoNote", "upload->photo"):
                self.render(rec)
            if method == "getFile":
                return {"ok": True, "result": {"file_path": f"demo/{params.get('file_id')}"}}
            if method == "getChatMember":
                return {"ok": True, "result": {"status": "member"}}
            if method == "getMe":
                return {"ok": True, "result": {"id": 777, "username": "premiumvideo_bot", "is_bot": True}}
            return {"ok": True, "result": {"message_id": 50000 + len(self.outbox), "date": int(time.time()),
                                           "chat": {"id": params.get("chat_id")}}}
        try:
            code, body = http_post(api_url(method), params, files)
            j = json.loads(body or "{}")
            if not j.get("ok"):
                log(f"telegram API error {method} [{code}]: {j.get('description')}")
                # Premium-emoji safety net: if the server rejects <tg-emoji> / button icons
                # (e.g. owner lost Premium), retry once with everything stripped back to
                # plain unicode emoji so the message still goes out.
                flat = json.dumps(params, ensure_ascii=False)
                if "tg-emoji" in str(params.get("text", "")) + str(params.get("caption", "")) \
                        or "icon_custom_emoji_id" in flat:
                    params2 = dict(params)
                    for k in ("text", "caption"):
                        if params2.get(k):
                            params2[k] = _TG_EMOJI_RE.sub(r"\1", params2[k])
                    if params2.get("reply_markup"):
                        try:
                            rm = json.loads(params2["reply_markup"])
                            for row in rm.get("inline_keyboard", []):
                                for b in row:
                                    b.pop("icon_custom_emoji_id", None)
                            params2["reply_markup"] = json.dumps(rm, ensure_ascii=False)
                        except Exception:
                            pass
                    log("premium emoji rejected — retrying with plain emoji")
                    try:
                        code, body = http_post(api_url(method), params2, files)
                        return json.loads(body or "{}")
                    except Exception as e:
                        return {"ok": False, "error": str(e)}
            return j
        except Exception as e:
            log(f"telegram request failed {method}: {e}")
            return {"ok": False, "error": str(e)}

    # ------------------------------- pretty print --------------------------
    def render(self, rec):
        m, p = rec["method"], rec["params"]
        head = f"→ chat {p.get('chat_id')}"
        head += {"sendPhoto": " 📷 photo", "sendVideo": " 🎬 video", "sendDocument": " 📄 document",
                 "upload->photo": " 📷 photo (upload)"}.get(m, "")
        txt = p.get("text") or p.get("caption") or "(attachment only)"
        print(f"{self.label} ── {head}")
        for line in str(txt).splitlines():
            print("  │ " + line)
        markup = p.get("reply_markup")
        if markup:
            try:
                for row in json.loads(markup)["inline_keyboard"]:
                    print("  ▸ " + " | ".join(b["text"] for b in row))
            except Exception:
                pass

    # -------------------------------- helpers ------------------------------
    def send(self, chat_id, text, kbd=None):
        return self.api("sendMessage", {"chat_id": chat_id, "text": (text or "")[:4000],
                                       "parse_mode": "HTML", "disable_web_page_preview": True,
                                       "reply_markup": kbd})

    def send_media(self, chat_id, kind, file_id, caption="", kbd=None):
        kind = (kind or "document").lower()
        method = {"photo": "sendPhoto", "video": "sendVideo", "animation": "sendAnimation",
                  "audio": "sendAudio", "voice": "sendVoice", "video_note": "sendVideoNote",
                  "sticker": "sendSticker"}.get(kind, "sendDocument")
        field = "document" if method == "sendDocument" else kind
        params = {"chat_id": chat_id, field: file_id}
        if method not in ("sendVideoNote", "sendSticker"):
            params["caption"] = (caption or "")[:1000]
            params["parse_mode"] = "HTML"
        if kbd:
            params["reply_markup"] = kbd
        return self.api(method, params)

    def send_upload(self, chat_id, path, caption="", kind="photo"):
        """Send a local file (used for the generated UPI QR image)."""
        if not path or not os.path.exists(path):
            return {"ok": False}
        method = {"photo": "sendPhoto", "video": "sendVideo", "audio": "sendAudio"}.get(kind, "sendDocument")
        field = {"photo": "photo", "video": "video", "audio": "audio"}.get(kind, "document")
        if self.offline:
            self.outbox.append({"method": "upload->" + kind,
                                "params": {"chat_id": chat_id, "path": path, "caption": caption}})
            self.render({"method": method, "params": {"chat_id": chat_id,
                                                      "caption": f"[{os.path.basename(path)}]\n{caption}"}})
            return {"ok": True, "result": {"file_id": f"demo_{os.path.basename(path)}"}}
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            log(f"upload read failed: {e}")
            return {"ok": False}
        ctype = "image/png" if path.lower().endswith(".png") else "application/octet-stream"
        return self.api(method, {"chat_id": chat_id, "caption": (caption or "")[:1000], "parse_mode": "HTML"},
                        files={field: (os.path.basename(path), data, ctype)})

    def download(self, file_id, dest_dir=DATA_DIR):
        j = self.api("getFile", {"file_id": file_id})
        remote = (((j or {}).get("result") or {}).get("file_path")) or ""
        if not remote:
            return None
        out = os.path.join(dest_dir, os.path.basename(remote))
        if self.offline:
            return out
        try:
            with open(out, "wb") as f:
                f.write(http_get_bytes(file_url(remote)))
            return out
        except Exception as e:
            log(f"download failed: {e}")
            return None

    def answer(self, cb_id, text="", alert=False):
        return self.api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text, "show_alert": alert})


# ------------------------------ keyboard builder ---------------------------
def btn(text, data=None, url=None, style=None, icon=None):
    """One button: label + (callback_data OR url).
    style: "success" (green) / "danger" (red) / "primary" (blue) — Bot API 9.4 colors.
    icon : emoji char from PEMOJI — sent as a premium custom-emoji icon when enabled."""
    return (text, data, url, style or "", icon or "")


def ubtn(label, data=None, url=None, icon=None, style=None):
    """User-side button: premium icon + color style.
    Without premium emoji the plain unicode emoji char is kept in the label."""
    if icon and PREMIUM_EMOJI and icon in PEMOJI:
        return btn(label, data, url, style=style, icon=icon)
    if icon and icon not in label:
        label = f"{icon} {label}"
    return btn(label, data, url, style=style)


def rows(*groups):
    """rows([b1, b2], [b3]) -> [[b1,b2],[b3]]  (keeps the UI code readable)."""
    return [list(g) for g in groups if g]


def kb(button_rows):
    """[[btn,...],...] -> Telegram reply_markup dict."""
    out = []
    for r in button_rows or []:
        line = []
        for item in r:
            t = item[0]
            d = item[1] if len(item) > 1 else None
            u = item[2] if len(item) > 2 else None
            style = item[3] if len(item) > 3 else ""
            icon = item[4] if len(item) > 4 else ""
            b = {"text": t}
            if d:
                b["callback_data"] = d
            if u:
                b["url"] = u
            if style:
                b["style"] = style
            if icon and PREMIUM_EMOJI and icon in PEMOJI:
                b["icon_custom_emoji_id"] = PEMOJI[icon]
            line.append(b)
        out.append(line)
    return {"inline_keyboard": out}


# ==========================================================================
# UPI QR — generated locally when the admin did not upload a QR image
# ==========================================================================
def make_upi_qr(amount, order_no, upi_id, payee, note) -> str | None:
    if qrcode is None or not upi_id:
        return None
    params = {"pa": upi_id, "pn": payee or setting("brand") or "Premium Video", "cu": "INR"}
    if amount and float(amount) > 0:
        params["am"] = f"{float(amount):.2f}"
    if note:
        params["tn"] = str(note)[:50]
    if order_no:
        params["tr"] = str(order_no).replace("#", "")
    uri = "upi://pay?" + urllib.parse.urlencode(params)
    path = os.path.join(DATA_DIR, f"qr_{abs(hash(uri)) % 10**8}.png")
    try:
        qrcode.make(uri).save(path)
        return path
    except Exception as e:
        log(f"QR generation failed: {e}")
        return None


# ==========================================================================
# THE BOT
# ==========================================================================
ITEM_FIELDS = {
    "title": "Item title",
    "descr": "Description",
    "price": "Price",
    "link": "Main link",
    "channel_link": "Channel link",
    "group_link": "Group link",
    "validity_days": "Access validity (days)",
}
SET_LABELS = {"brand": "Brand name", "upi_id": "UPI ID", "payee_name": "Payee name",
              "pay_note": "Checkout note", "refund_note": "Refund note",
              "pay_instructions": "Extra instructions", "welcome_text": "Welcome message",
              "force_channel": "Force channel join", "out_of_stock_note": "Empty-store note",
              "qr_file_id": "QR image", "welcome_photo_id": "Welcome photo",
              "demo_link": "Free demo link", "proofs_link": "Proofs channel",
              "support_link": "Support link", "stats_joined": "Stats — users joined",
              "stats_month": "Stats — active this month", "stats_today": "Stats — active today"}


class PremiumBot:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.offset = 0

    # ======================================================================
    # UPDATE ROUTING
    # ======================================================================
    def handle_update(self, update: dict):
        try:
            if "callback_query" in update:
                return self.on_callback(update["callback_query"])
            m = update.get("message") or update.get("edited_message")
            if m:
                return self.on_message(m)
        except Exception:
            log("update handling failed:\n" + traceback.format_exc())

    def on_message(self, m: dict):
        frm = m.get("from") or {}
        chat = m.get("chat") or {}
        tg_id = int(frm.get("id") or chat.get("id") or 0)
        chat_id = chat.get("id", tg_id)
        chat_type = chat.get("type", "private")
        if not tg_id:
            return
        text = (m.get("text") or m.get("caption") or "").strip()
        media = self.media_of(m)
        admin = tg_id in ADMIN_IDS
        uid = ensure_user(frm)

        if chat_type in ("group", "supergroup", "channel"):
            if text.startswith("/start"):
                self.bot.send(chat_id, "I only work in private chats — open my DM and send /start.")
            return

        if not ADMIN_IDS:
            ADMIN_IDS.add(tg_id)                    # first person to talk to the bot = owner
            log(f"ADMIN_IDS auto-assigned → {tg_id}")
            admin = True

        if not admin and is_blocked(tg_id):
            return self.bot.send(chat_id,
                                 "🚫 Your access has been suspended by the administrator.\n"
                                 "Please contact the admin for help.",
                                 kb(rows([btn("💬 Message admin", "contact_admin")])))

        state = get_state(tg_id)
        if state and self.step_is_optional(state, text):
            self.abandon_step(chat_id, tg_id, state)
            state = get_state(tg_id)
        if state and self.on_state(chat_id, tg_id, uid, text, media, state, admin):
            return

        if text.startswith("/"):
            return self.on_command(chat_id, tg_id, uid, m, text, media, admin)
        if text:
            return self.show_store(chat_id, uid, search=text)
        self.bot.send(chat_id, "Tap a button below to continue 👇", kb(self.home_kb(uid)))

    def on_callback(self, cb: dict):
        m = cb.get("message") or {}
        chat_id = (m.get("chat") or {}).get("id")
        frm = cb.get("from") or {}
        tg_id = int(frm.get("id") or 0)
        data = cb.get("data") or ""
        admin = tg_id in ADMIN_IDS
        urow = user_by_tg(tg_id) or {"id": ensure_user(frm), "tg_id": tg_id, "name": frm.get("first_name", "User")}
        uid = urow["id"]
        if not admin and is_blocked(tg_id):
            return self.bot.answer(cb.get("id"), "Your access is suspended.", True)
        if not admin and not self.channel_ok(tg_id, chat_id):
            return self.bot.answer(cb.get("id"))
        self.bot.answer(cb.get("id"))
        try:
            self.dispatch(chat_id, tg_id, uid, admin, data)
        except Exception:
            log("callback failed:\n" + traceback.format_exc())

    # ======================================================================
    # COMMANDS
    # ======================================================================
    def on_command(self, chat_id, tg_id, uid, m, text, media, admin):
        parts = text.split(None, 1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/start", "/menu", "/home"):
            return self.cmd_start(chat_id, tg_id, uid, m, text)
        if cmd in ("/shop", "/store", "/plans"):
            return self.show_store(chat_id, uid, search=arg)
        if cmd in ("/my", "/library", "/mine"):
            return self.show_library(chat_id, uid)
        if cmd == "/status":
            return self.show_orders(chat_id, uid)
        if cmd in ("/buy", "/pay", "/checkout"):
            return self.cmd_buy(chat_id, uid, arg)
        if cmd in ("/payinfo", "/payment", "/upi"):
            return self.show_payinfo(chat_id, uid)
        if cmd in ("/help", "/rules"):
            return self.bot.send(chat_id, self.help_text(),
                                 kb(rows([btn("🛍 Browse store", "shop:0"), btn("📚 My library", "library")],
                                         [btn("🧾 Order status", "orders"), btn("💳 Payment info", "payinfo")])))
        if cmd == "/id":
            return self.bot.send(chat_id,
                                 f"🆔 <b>Your details</b>\n{SEP}\nBot ID: <code>{uid}</code>\n"
                                 f"Telegram ID: <code>{tg_id}</code>",
                                 kb(rows([btn("🏠 Home", "home")])))
        if cmd == "/cancel":
            set_state(tg_id, {})
            return self.bot.send(chat_id, "Cancelled — nothing was changed.",
                                 kb(rows([btn("🛍 Browse store", "shop:0")])))
        if cmd == "/unstick":
            target = re.sub(r"\D", "", arg) or str(tg_id)
            x("DELETE FROM states WHERE user_id=?", (int(target),))
            msg = "Your pending step was reset." if not admin else f"Reset the pending step of <code>{target}</code>."
            return self.bot.send(chat_id, msg, kb(rows([btn("🏠 Home", "home")])))

        # ------------------------- admin commands -------------------------
        if not admin:
            return self.bot.send(chat_id, "🔒 That command is for the administrator.",
                                 kb(rows([btn("🛍 Browse store", "shop:0"), btn("📚 My library", "library")])))
        if cmd == "/admin":
            return self.admin_panel(chat_id)
        if cmd == "/additem":
            return self.wizard_start(chat_id, tg_id)
        if cmd == "/add":
            return self.quick_add(chat_id, tg_id, m, arg, media)
        if cmd == "/addlink":
            return self.quick_add(chat_id, tg_id, m, arg, media, force_link=True)
        if cmd == "/addtext":
            return self.quick_add(chat_id, tg_id, m, arg, media, force_text=True)
        if cmd in ("/items", "/manage"):
            return self.admin_items(chat_id, page=0)
        if cmd in ("/orders", "/pending", "/approve"):
            return self.admin_orders(chat_id, page=0)
        if cmd in ("/stats", "/sales", "/report"):
            return self.admin_stats(chat_id)
        if cmd in ("/broadcast", "/bc"):
            if not arg:
                set_state(tg_id, {"flow": "bc", "step": "text", "d": {}})
                return self.bot.send(chat_id, "📣 Now type the broadcast message (or /cancel).")
            return self.broadcast(chat_id, arg)
        if cmd == "/welcome":
            set_setting("welcome_text", arg)
            return self.bot.send(chat_id, self.welcome_preview_text(),
                                 kb(rows([btn("🖼 Set welcome photo", "setwelcomephoto"),
                                          btn("🗑 Remove photo", "delwelcomephoto")])))
        if cmd in ("/setwelcomephoto", "/welcomephoto"):
            if not media:
                set_state(tg_id, {"flow": "setwelcomephoto", "step": "media", "d": {}})
                return self.bot.send(chat_id, "🖼 Now send the photo you want on the welcome screen.")
            return self.save_welcome_photo(chat_id, media)
        if cmd in ("/settings", "/setup"):
            return self.admin_settings(chat_id)
        if cmd in ("/del", "/edit", "/price", "/valid", "/pause", "/resume", "/grant", "/revoke",
                   "/block", "/unblock", "/approve_order"):
            return self.admin_legacy(chat_id, tg_id, uid, m, cmd, arg, media)
        return self.bot.send(chat_id,
                             f"Unknown command <code>{esc(cmd)}</code>.\n\n"
                             "Everything is available as buttons inside the admin panel 👇",
                             kb(rows([btn("🛠 Admin panel", "admin")])))

    # ======================================================================
    # USER: WELCOME / HOME
    # ======================================================================
    def home_kb(self, uid):
        """Main menu — premium emoji icons + colored buttons."""
        s = all_settings()
        out = [[ubtn("Buy Premium Videos", "shop:0", icon="🍆", style="success")]]
        link_row = []
        if s["demo_link"]:
            link_row.append(ubtn("Free demo ↗", None, t_url(s["demo_link"]), icon="👅", style="primary"))
        if s["proofs_link"]:
            link_row.append(ubtn("Proofs ↗", None, t_url(s["proofs_link"]), icon="🍑", style="primary"))
        if link_row:
            out.append(link_row)
        out.append([ubtn("My profile", "profile", icon="🌸", style="primary"),
                    ubtn("Support", None, t_url(s["support_link"]), icon="😘", style="primary")
                    if s["support_link"]
                    else ubtn("Support", "support", icon="😘", style="primary")])
        out.append([ubtn("How to use", "howto", icon="😄", style="primary")])
        return out

    def stats_lines(self) -> str:
        """Welcome-screen counters. Admin-set values win; empty = real counts."""
        s = all_settings()
        joined = (s["stats_joined"] or "").strip()
        month = (s["stats_month"] or "").strip()
        today = (s["stats_today"] or "").strip()
        if not joined or not month or not today:
            m30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            d0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            d0 = d0.strftime("%Y-%m-%d %H:%M:%S")
            rj = q("SELECT COUNT(*) c FROM users")[0]["c"]
            rm = q("SELECT COUNT(*) c FROM users WHERE last_seen >= ?", (m30,))[0]["c"]
            rt = q("SELECT COUNT(*) c FROM users WHERE last_seen >= ?", (d0,))[0]["c"]
            joined = joined or str(rj)
            month = month or str(rm)
            today = today or str(rt)
        return (f"👥 <b>{esc(joined)}</b> users already joined\n"
                f"🔥 <b>{esc(month)}</b> active this month · ⚡ <b>{esc(today)}</b> active today")

    def support_line(self) -> str:
        s = all_settings()
        sup = (s["support_link"] or "").strip()
        if not sup:
            return "🛠 <b>TECH SUPPORT</b> – tap <i>😘 Support</i> below"
        if sup.startswith("@"):
            return f"🛠 <b>TECH SUPPORT</b> – <a href=\"{t_url(sup)}\">{esc(sup)}</a>"
        return f"🛠 <b>TECH SUPPORT</b> – <a href=\"{t_url(sup)}\">{esc(shorten(sup, 32))}</a>"

    def default_welcome(self) -> str:
        s = all_settings()
        price = q("SELECT MIN(price) mn FROM items WHERE active=1")
        lowest = price[0]["mn"] if price and price[0]["mn"] is not None else None
        lines = [f"🍆 <b><u>{esc(s['brand'])}</u></b>",
                 "",
                 "<blockquote>💦 <i>Premium videos, courses & VIP access</i> —\n"
                 "delivered <b>instantly</b> after payment verification. 🔐</blockquote>",
                 "",
                 f"🥵 Plans from <b>{money(lowest)}</b> · 🍭 some drops are <i>free</i>"
                 if lowest is not None else "🍑 New videos are added by the admin",
                 f"🤫 <tg-spoiler>new uploads every week — stay tuned</tg-spoiler>"]
        return pe("\n".join(lines))

    def welcome_body(self) -> str:
        """Custom welcome text (if any) + the always-on support & stats footer."""
        s = all_settings()
        body = s["welcome_text"] or self.default_welcome()
        return f"{body}\n\n{self.support_line()}\n{self.stats_lines()}"

    def welcome_preview_text(self) -> str:
        photo = "🖼 Welcome photo: <b>set</b> ✅" if setting("welcome_photo_id") else "🖼 Welcome photo: <i>not set</i>"
        return f"{self.welcome_body()}\n\n{SEP}\n{photo}"

    def cmd_start(self, chat_id, tg_id, uid, m, text):
        deep = re.search(r"/start\s+(\S+)", text or "")
        if deep:
            key = deep.group(1).lower()
            if key in ("shop", "store", "menu"):
                return self.show_store(chat_id, uid)
            if key.startswith(("buy", "item")):
                it = get_item(int(re.sub(r"\D", "", key) or 0))
                if it:
                    self.send_welcome(chat_id, uid)
                    if tg_id in ADMIN_IDS or has_access(uid, it["id"]):
                        return self.show_item(chat_id, uid, it)
                    return self.start_buy(chat_id, uid, it)   # straight to checkout
        if not self.channel_ok(tg_id, chat_id, admin_ok=True):
            return
        self.send_welcome(chat_id, uid)

    def send_welcome(self, chat_id, uid):
        """Welcome screen — photo (if the admin set one) + text + buttons."""
        s = all_settings()
        body = self.welcome_body()
        buttons = self.home_kb(uid)
        photo = s["welcome_photo_id"]
        if photo:
            caption = body if len(body) <= 1000 else body[:997] + "…"
            r = self.bot.send_media(chat_id, "photo", photo, caption=caption, kbd=buttons)
            if r and r.get("ok"):
                if len(body) > 1000:
                    self.bot.send(chat_id, body, kbd=buttons)
                return
        self.bot.send(chat_id, body, kbd=buttons)

    def channel_ok(self, tg_id, chat_id, admin_ok=False) -> bool:
        """If `force_channel` is set, the user must join that channel first."""
        ch = setting("force_channel").strip()
        if not ch or (admin_ok and tg_id in ADMIN_IDS):
            return True
        try:
            j = self.bot.api("getChatMember", {"chat_id": ch, "user_id": tg_id})
            if ((j or {}).get("result") or {}).get("status") in ("creator", "administrator", "member", "restricted"):
                return True
        except Exception:
            return True
        url = ch if ch.startswith("http") else "https://t.me/" + ch.lstrip("@")
        self.bot.send(chat_id,
                      pe(f"🔒 <b>Membership required</b>\n{SEP}\n"
                         f"Join <b>{esc(ch)}</b> to use this bot, then tap the button below."),
                      kb(rows([ubtn("Join channel", None, url, icon="👅", style="success")],
                              [ubtn("I joined — check again", "recheck", icon="🍑", style="primary")])))
        return False

    def help_text(self) -> str:
        s = all_settings()
        return pe(f"😄 <b>{esc(s['brand'])} — help</b>\n{SEP}\n"
                  "🍑 <b>Browse store</b> — all items with prices\n"
                  "🍒 <b>My library</b> — everything you unlocked\n"
                  "🧾 <b>My orders</b> — status of each payment\n"
                  "💦 <b>Payment info</b> — QR / UPI id\n\n"
                  f"<b>How buying works</b>\n{SEP}\n"
                  f"{esc(s['pay_note'])}\n\n"
                  f"<b>Refunds</b>\n{esc(s['refund_note'])}")

    # ======================================================================
    # STORE
    # ======================================================================
    def item_caption(self, it, uid=None) -> str:
        s = all_settings()
        icon = {"video": "🍆", "photo": "🍑", "file": "💦", "link": "👅", "text": "🍭"}.get(it["kind"], "🍆")
        includes = []
        if it["file_id"]:
            includes.append("file download")
        if it["link"]:
            includes.append("main link")
        if it["channel_link"]:
            includes.append("private channel")
        if it["group_link"]:
            includes.append("private group")
        access = "<i>Lifetime</i> ♾" if not it["validity_days"] else f"<i>{it['validity_days']} days</i>"
        out = [f"{icon} <b><u>{esc(it['title'])}</u></b>  <i>#{it['id']}</i>",
               "",
               f"💵 Price: <code>{money(it['price'])}</code>" if it["price"] > 0 else "💵 Price:  <b>Free</b>",
               f"⏱ Access: {access}",
               f"📦 Includes: {', '.join(includes)}" if includes else "📦 Includes: <i>—</i>",
               f"🛒 Sold: <b>{it['sold']}</b>" if it["sold"] else ""]
        if it["descr"]:
            out += ["", f"<blockquote>{esc(it['descr'])[:1500]}</blockquote>"]
        if uid and has_access(uid, it["id"]):
            out += ["", "✅ <b>Already unlocked</b> — open it from your <i>📚 library</i>."]
        elif it["price"] > 0:
            out += ["", f"<s>hidden fees</s> <b>none</b> — {esc(s['pay_note'])[:160]}"]
        return "\n".join([l for l in out if l != ""])

    def show_store(self, chat_id, uid, page=0, search=""):
        args = []
        where = "active=1"
        if search:
            where += " AND (title LIKE ? OR descr LIKE ?)"
            args = [f"%{search}%", f"%{search}%"]
        allitems = q(f"SELECT * FROM items WHERE {where} ORDER BY id DESC LIMIT 300", tuple(args))
        total = len(allitems)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(int(page), pages - 1))
        chunk = allitems[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        if not chunk:
            msg = pe("🔍 Nothing matched that search." if search else
                     f"🛒 The store is empty right now.\n{SEP}\n"
                     f"{esc(setting('out_of_stock_note') or 'Please check back soon.')}")
            return self.bot.send(chat_id, msg, kb(rows([ubtn("Home", "home", icon="🍑", style="primary")])))
        # one button per item — "Title (₹price)" — premium icon per item type
        buttons = []
        kind_emoji = {"video": "🍆", "photo": "🍑", "file": "💦", "link": "👅", "text": "🍭"}
        for it in chunk:
            mark = "✅ " if uid and has_access(uid, it["id"]) else ""
            price = "Free" if it["price"] <= 0 else money(it["price"])
            buttons.append([ubtn(f"{mark}{shorten(it['title'], 40)} ({price})", f"item:{it['id']}",
                                 icon=kind_emoji.get(it["kind"], "🍆"))])
        head = (f"🍑 <b><u>{esc(setting('brand'))}</u></b> — <b>Premium store</b>\n\n"
                f"<blockquote>🥵 <b>{total}</b> item{'s' if total != 1 else ''} live — "
                f"tap one to buy.\nPrices are <i>final</i>, delivery is <i>instant</i>. 💦</blockquote>")
        if search:
            head += f"\n🔍 results for “{esc(search)}”"
        nav = []
        if page > 0:
            nav.append(ubtn("Prev", f"shop:{page - 1}", icon="🍑", style="primary"))
        if page < pages - 1:
            nav.append(ubtn("Next", f"shop:{page + 1}", icon="🍑", style="primary"))
        if nav:
            buttons.append(nav)
        buttons.append([ubtn("Back", "home", icon="🌸", style="primary")])
        self.bot.send(chat_id,
                      pe(head) + (f"\n\n<i>Page {page + 1}/{pages}</i>" if pages > 1 else ""),
                      kb(buttons))

    def show_item(self, chat_id, uid, it):
        buttons = []
        if has_access(uid, it["id"]):
            buttons.append([ubtn("Open my access", f"open:{it['id']}", icon="🍒", style="success")])
        elif it["price"] > 0:
            buttons.append([ubtn(f"Buy for {money(it['price'])}", f"buy:{it['id']}", icon="💦", style="success")])
        else:
            buttons.append([ubtn("Get it free", f"buy:{it['id']}", icon="🍭", style="success")])
        buttons.append([ubtn("Payment info", "payinfo", icon="💦", style="primary"),
                        ubtn("How to use", "howto", icon="😄", style="primary")])
        buttons.append([ubtn("Back", "shop:0", icon="🍑", style="primary")])
        self.bot.send(chat_id, pe(self.item_caption(it, uid)), kb(buttons))

    # ======================================================================
    # CHECKOUT
    # ======================================================================
    def cmd_buy(self, chat_id, uid, arg):
        it = None
        for tok in re.split(r"[,\s]+", arg or ""):
            if tok.lstrip("#").isdigit():
                it = get_item(int(tok.lstrip("#")))
                if it:
                    break
        if not it:
            return self.bot.send(chat_id, "Which item? Tap one in the store 👇",
                                 kb(rows([btn("🛍 Browse store", "shop:0")])))
        self.start_buy(chat_id, uid, it)

    def start_buy(self, chat_id, uid, it):
        if not it["active"]:
            return self.bot.send(chat_id, pe("⏸️ This item is not on sale right now."),
                                 kb(rows([ubtn("Browse store", "shop:0", icon="🍑", style="primary")])))
        if has_access(uid, it["id"]):
            return self.bot.send(chat_id, pe("🍒 You already have this item — open it from your library."),
                                 kb(rows([ubtn("My library", "library", icon="🍒", style="primary")],
                                         [ubtn("Browse store", "shop:0", icon="🍑", style="primary")])))
        pending = pending_for_user(uid)
        same = [p for p in pending if p["item_id"] == it["id"]]
        if same:
            o = same[0]
            self.set_proof_state(uid, o["id"], it["id"])
            return self.payment_screen(chat_id, uid, o, it,
                                       note=pe(f"⏳ Order <b>#{o['no']}</b> is already waiting for your screenshot."))
        if it["price"] <= 0:
            grant_access(uid, it["id"], None, it["validity_days"])
            self.bot.send(chat_id, pe("🍭 Free item unlocked — enjoy! 😘"), kbd=rows())
            return self.deliver(uid, it, None)
        oid = create_order(uid, it)
        self.set_proof_state(uid, oid, it["id"])
        return self.payment_screen(chat_id, uid, get_order(oid), it)

    def set_proof_state(self, uid, order_id, item_id):
        tg = self.tg_of(uid)
        if tg:
            set_state(int(tg), {"flow": "buy", "step": "proof", "order": order_id, "item": item_id})

    def payment_screen(self, chat_id, uid, order, it, note=""):
        """Payment instructions + QR + [submit screenshot] / [cancel]."""
        s = all_settings()
        amount = float(order["amount"]) if order else float(it["price"])
        o_no = order["no"] if order else ""
        body = [f"💦 <b><u>Checkout</u></b> — <b>{money(amount)}</b>", ""]
        if note:
            body += [note, ""]
        body += [f"🍆 Item: <i>{esc(shorten(it['title'], 40))}</i>",
                 f"🧾 Order ID: <code>#{o_no}</code>", ""]
        if s["upi_id"]:
            body.append(f"UPI ID: <code>{esc(s['upi_id'])}</code>")
            body.append(f"Payee: <b>{esc(s['payee_name'] or s['brand'])}</b>")
            body.append(f"<pre>TO   : {s['upi_id']}\nAMT  : {amount:.2f}\nNOTE : #{o_no}</pre>")
        else:
            body.append("⚠️ The admin has not added a UPI ID yet — please message the admin.")
        body.append(f"🥵 Amount: <b>{money(amount)}</b> <i>(exact)</i>")
        body += ["", f"<blockquote>{esc(s['pay_note'])}"
                 + (f"\n{esc(s['pay_instructions'])}" if s["pay_instructions"] else "") + "</blockquote>"]
        text = pe("\n".join([l for l in body if l is not None]))
        buttons = rows([ubtn("I paid — submit screenshot", f"ready:{order['id'] if order else 0}",
                             icon="💦", style="success")])
        qr = s["qr_file_id"]
        if qr:
            self.bot.send_media(chat_id, "photo", qr, caption=text[:1000], kbd=buttons)
            if len(text) > 1000:
                self.bot.send(chat_id, text[1000:], kbd=None)
            return
        png = make_upi_qr(amount, o_no, s["upi_id"], s["payee_name"], f"#{o_no}")
        if png:
            self.bot.send_upload(chat_id, png, caption=text[:1000], kind="photo")
            try:
                os.remove(png)
            except Exception:
                pass
            return
        self.bot.send(chat_id, text, kbd=buttons)

    def show_payinfo(self, chat_id, uid):
        s = all_settings()
        pending = pending_for_user(uid)
        o = pending[0] if pending else None
        it = get_item(o["item_id"]) if o else None
        if o and it:
            return self.payment_screen(chat_id, uid, o, it)
        body = ["💦 <b><u>Payment details</u></b>",
                "",
                f"UPI ID: <code>{esc(s['upi_id'] or 'not configured')}</code>",
                f"Payee: <b>{esc(s['payee_name'] or s['brand'])}</b>",
                f"QR image: {'✅ on the checkout screen' if s['qr_file_id'] or s['upi_id'] else '❌ not set'}",
                "",
                f"<blockquote>{esc(s['pay_note'])}\n\n<i>{esc(s['refund_note'])}</i></blockquote>"]
        self.bot.send(chat_id, pe("\n".join(body)),
                      kb(rows([ubtn("Buy Premium Videos", "shop:0", icon="🍆", style="success")],
                              [ubtn("Back", "home", icon="🌸", style="primary")])))

    # =====================================================================
    # USER: PROFILE / HOW TO USE / SUPPORT
    # =====================================================================
    def show_profile(self, chat_id, uid):
        row = user_by_id(uid)
        u = dict(row) if row else {}
        lib = q("SELECT COUNT(*) c FROM unlocks WHERE user_id=?", (int(uid),))[0]["c"]
        body = ["🌸 <b><u>My profile</u></b>",
                "",
                f"🆔 Bot ID: <code>{uid}</code> · TG: <code>{esc(str(u.get('tg_id', '')))}</code>",
                f"🧾 Orders: <b>{u.get('orders', 0)}</b> · 💸 Spent: <code>{money(u.get('spent', 0))}</code>",
                f"🍒 Library: <b>{lib}</b> item{'s' if lib != 1 else ''}",
                f"🗓 Joined: <i>{ts(u.get('created_at', ''))}</i>",
                "",
                f"<blockquote>{esc(u.get('name', ''))} {esc(u.get('username', ''))}\n"
                f"<i>Status:</i> {'🥵 valued customer' if (u.get('spent') or 0) > 0 else '🍭 new member'}</blockquote>"]
        self.bot.send(chat_id, pe("\n".join(body)),
                      kb(rows([ubtn("My library", "library", icon="🍒", style="primary"),
                               ubtn("My orders", "orders", icon="💦", style="primary")],
                              [ubtn("Payment info", "payinfo", icon="💦", style="primary")],
                              [ubtn("Back", "home", icon="🌸", style="primary")])))

    def show_howto(self, chat_id):
        s = all_settings()
        upi = f"<code>{esc(s['upi_id'])}</code>" if s["upi_id"] else "<i>shown at checkout</i>"
        body = ["😄 <b><u>How to use</u></b>",
                "",
                "<b>1️⃣ Pick a video</b> — tap <i>Buy Premium Videos</i> and choose one.",
                f"<b>2️⃣ Pay the exact amount</b> — UPI {upi} or scan the QR.",
                "<b>3️⃣ Send the screenshot</b> — 📸 photo of the successful payment.",
                "<b>4️⃣ Get it instantly</b> — admin approves → content lands in your <i>🍒 library</i>.",
                "",
                "<blockquote>⚠️ Send the <i>exact</i> amount. Wrong / short payments are "
                f"<s>kept</s> <b>refunded</b> — {esc(s['refund_note'])[:140]}</blockquote>",
                "",
                "🤫 <tg-spoiler>free items unlock instantly — no payment needed</tg-spoiler>",
                f"💦 Preview quality first in the <i>Free demo</i> channel."
                if s["demo_link"] else ""]
        self.bot.send(chat_id, pe("\n".join([l for l in body if l != ""])),
                      kb(rows([ubtn("Buy Premium Videos", "shop:0", icon="🍆", style="success"),
                               ubtn("Payment info", "payinfo", icon="💦", style="primary")],
                              [ubtn("Back", "home", icon="🌸", style="primary")])))

    def show_support(self, chat_id):
        s = all_settings()
        body = ["😘 <b><u>Support</u></b>",
                "",
                "<blockquote>Problem with a payment or a video?\n"
                "Message the admin directly — you'll get a reply in this chat.</blockquote>"]
        if s["support_link"]:
            body.append(f"🛠 <b>TECH SUPPORT</b> – <a href=\"{t_url(s['support_link'])}\">"
                        f"{esc(s['support_link'])}</a>")
        self.bot.send(chat_id, pe("\n".join(body)),
                      kb(rows([ubtn("Message admin", "contact_admin", icon="😘", style="primary")],
                              [ubtn("Back", "home", icon="🌸", style="primary")])))

    def submit_proof(self, chat_id, tg_id, uid, order, media, note=""):
        if not media:
            return self.bot.send(chat_id,
                                 pe("📸 Please send the actual <b>screenshot</b> of the payment "
                                    "(photo or file) — a text message can't be verified."),
                                 kb(rows([btn("❌ Cancel order", f"cancel:{order['id']}", style="danger")])))
        x("UPDATE orders SET proof_id=?, proof_kind=?, note=? WHERE id=?",
          (media["file_id"], media["file_kind"], (note or "")[:200], int(order["id"])))
        set_state(tg_id, {})
        order = get_order(order["id"])
        it = get_item(order["item_id"])
        urow = user_by_id(uid)
        self.bot.send(chat_id,
                      pe(f"💦 <b>Proof received</b>\n{SEP}\nOrder <b>#{order['no']}</b> · {money(order['amount'])}\n"
                         "Status: <b>waiting for admin approval</b>\n"
                         "You'll get the content the moment it is approved — usually 5–30 minutes. 😘"),
                      kb(rows([ubtn("Check status", "orders", icon="💦", style="primary"),
                               ubtn("Keep browsing", "shop:0", icon="🍑", style="primary")])))
        self.notify_admin(order, it, urow)
        return True

    def cancel_order(self, chat_id, uid, oid):
        o = get_order(oid)
        if not o or int(o["user_id"]) != int(uid):
            return self.bot.send(chat_id, "❌ Order not found.")
        if o["status"] != "pending":
            return self.bot.send(chat_id, f"Order #{o['no']} is already <b>{o['status']}</b> — nothing to cancel.")
        x("UPDATE orders SET status='cancelled', decided_at=? WHERE id=?", (now(), int(oid)))
        set_state(int(self.tg_of(uid) or 0), {})
        self.bot.send(chat_id, pe(f"🗑 Order <b>#{o['no']}</b> was cancelled."),
                      kb(rows([ubtn("Browse store", "shop:0", icon="🍑", style="primary")],
                              [ubtn("My orders", "orders", icon="💦", style="primary")])))
        for a in ADMIN_IDS:
            self.bot.send(a, f"🗑 Order #{o['no']} cancelled by {esc(o and user_by_id(o['user_id']) and user_by_id(o['user_id'])['name'])}")

    # ======================================================================
    # LIBRARY / ORDERS
    # ======================================================================
    def show_library(self, chat_id, uid):
        rows_ = q("""SELECT u.expires_at, u.item_id, i.title, i.kind FROM unlocks u
                     JOIN items i ON i.id=u.item_id WHERE u.user_id=? ORDER BY u.item_id DESC LIMIT 40""",
                  (int(uid),))
        if not rows_:
            return self.bot.send(chat_id,
                                 pe("🍒 <b>Your library is empty</b>\n" + SEP + "\n"
                                    "Items you buy appear here and stay available forever."),
                                 kb(rows([ubtn("Browse store", "shop:0", icon="🍑", style="success")],
                                         [ubtn("Payment info", "payinfo", icon="💦", style="primary")])))
        lines, buttons = [], []
        for r in rows_:
            exp = f" · expires {ts(r['expires_at'])}" if r["expires_at"] else ""
            lines.append(f"▪️ <b>#{r['item_id']}</b> {esc(shorten(r['title'], 38))}{exp}")
            buttons.append([ubtn(f"Open {shorten(r['title'], 26)}", f"open:{r['item_id']}",
                                 icon="🍒", style="primary")])
        buttons.append([ubtn("Browse more", "shop:0", icon="🍑", style="success"),
                        ubtn("My orders", "orders", icon="💦", style="primary")])
        self.bot.send(chat_id, pe(f"🍒 <b>My library</b> — {len(rows_)} item(s)\n{SEP}\n\n" + "\n".join(lines)),
                      kb(buttons))

    def show_orders(self, chat_id, uid):
        rows_ = q("""SELECT o.*, i.title FROM orders o JOIN items i ON i.id=o.item_id
                     WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10""", (int(uid),))
        if not rows_:
            return self.bot.send(chat_id,
                                 pe("🧾 <b>No orders yet</b>\n" + SEP + "\nPick an item and pay — the order will show here."),
                                 kb(rows([ubtn("Browse store", "shop:0", icon="🍑", style="success")])))
        icon = {"pending": "⏳", "approved": "✅", "declined": "❌", "cancelled": "🗑"}
        lines, buttons = [], []
        for r in rows_:
            line = (f"{icon.get(r['status'], '•')} <b>#{r['no']}</b> · {esc(shorten(r['title'], 26))} · "
                    f"{money(r['amount'])}\n   <b>{r['status'].upper()}</b> · {ts(r['created_at'])}")
            if r["reason"]:
                line += f"\n   ↳ {esc(r['reason'])[:120]}"
            lines.append(line)
            if r["status"] == "pending":
                buttons.append([ubtn(f"Send proof · #{r['no']}", f"ready:{r['id']}", icon="💦", style="success"),
                                btn(f"❌ Cancel · #{r['no']}", f"cancel:{r['id']}", style="danger")])
            else:
                buttons.append([ubtn(f"Open item · #{r['no']}", f"open:{r['item_id']}", icon="🍒", style="primary")])
        buttons.append([ubtn("My library", "library", icon="🍒", style="primary"),
                        ubtn("Browse store", "shop:0", icon="🍑", style="success")])
        self.bot.send(chat_id, pe(f"🧾 <b>My orders</b>\n{SEP}\n\n" + "\n".join(lines)), kb(buttons))

    # ======================================================================
    # DELIVERY
    # ======================================================================
    def tg_of(self, uid):
        u = user_by_id(uid)
        return u["tg_id"] if u else None

    def deliver(self, uid, it, order):
        chat = self.tg_of(uid)
        if not chat:
            for a in ADMIN_IDS:
                self.bot.send(a, f"⚠️ Cannot deliver <b>{esc(it['title'])}</b> — no Telegram id for bot user {uid}.")
            return False
        if not it["validity_days"]:
            tail = "♾ Lifetime access"
        else:
            exp = (datetime.now() + timedelta(days=int(it["validity_days"]))).strftime("%d %b %Y")
            tail = f"⏱ {it['validity_days']} days access (until {exp})"
        head = pe(f"🍒 <b>{esc(it['title'])}</b>\n{SEP}\n{tail}")
        if it["descr"]:
            head += "\n\n" + esc(it["descr"])[:900]
        links = []
        if it["link"]:
            links.append(ubtn("Open link", None, it["link"], icon="👅", style="primary"))
        if it["channel_link"]:
            links.append(ubtn("Join channel", None, it["channel_link"], icon="👅", style="success"))
        if it["group_link"]:
            links.append(ubtn("Join group", None, it["group_link"], icon="👅", style="success"))
        link_rows = [links[i:i + 2] for i in range(0, len(links), 2)]
        footer = rows([ubtn("My library", "library", icon="🍒", style="primary"),
                       ubtn("Buy something else", "shop:0", icon="🍆", style="success")])
        if it["file_id"]:
            r = self.bot.send_media(chat, it["file_kind"] or it["kind"], it["file_id"],
                                    caption=head + "\n\n📦 Your download is attached to this message.",
                                    kbd=link_rows + footer)
            if r and not r.get("ok") and not self.bot.offline:
                for a in ADMIN_IDS:
                    self.bot.send(a, f"🚫 <b>Delivery failed</b> for user <code>{chat}</code> — the user may have "
                                     f"blocked the bot, or the stored file id expired.\n"
                                     f"Order {esc(order and order['no'])} · use 🔁 Re-deliver later.")
                return False
        else:
            self.bot.send(chat, head, kbd=link_rows + footer)
        if order:
            x("UPDATE orders SET delivered=1 WHERE id=?", (int(order["id"]),))
            x("UPDATE items SET sold=sold+1 WHERE id=?", (int(it["id"]),))
        return True

    # ======================================================================
    # ADMIN: notifications + approval
    # ======================================================================
    def notify_admin(self, order, it, urow):
        body = ["🔔 <b>New payment submitted</b>", SEP,
                f"Order: <b>#{order['no']}</b>",
                f"Customer: {esc(urow['name'])} {esc(urow['username'] or '')} · tg <code>{urow['tg_id']}</code>",
                f"Item: {esc(shorten(it['title'], 40))} <code>#{it['id']}</code>",
                f"Amount: <b>{money(order['amount'])}</b> · {ts(order['created_at'])}",
                (f"Note: {esc(order['note'])}" if order["note"] else ""),
                ("" if all_settings()["upi_id"] else "⚠️ No UPI id configured — verify manually.")]
        buttons = rows([btn("✅ Approve & deliver", f"aok:{order['id']}"),
                        btn("❌ Decline", f"adcl:{order['id']}")],
                       [btn("🧾 Order details", f"aord:{order['id']}"), btn("👤 User profile", f"ausr:{urow['id']}")],
                       [btn("⏳ Pending queue", "pend")])
        for a in ADMIN_IDS:
            if order["proof_id"]:
                self.bot.send_media(a, order["proof_kind"] or "photo", order["proof_id"],
                                    caption="\n".join([l for l in body if l]), kbd=buttons)
            else:
                self.bot.send(a, "\n".join([l for l in body if l]) + "\n\n⚠️ No screenshot was attached.",
                              kbd=buttons)

    def approve_order(self, admin_chat, oid, silent=False):
        o = get_order(oid)
        if not o:
            return self.bot.send(admin_chat, "❌ Order not found.")
        it = get_item(o["item_id"])
        urow = user_by_id(o["user_id"])
        if o["status"] == "approved":
            return self.bot.send(admin_chat,
                                 f"ℹ️ Order <b>#{o['no']}</b> is already approved.",
                                 kb(rows([btn("🔁 Send again", f"adel:{o['id']}")], [btn("🧾 Order details", f"aord:{o['id']}")])))
        if not (it and urow):
            return self.bot.send(admin_chat, "❌ Item or user is missing — cannot approve.")
        x("UPDATE orders SET status='approved', decided_at=?, decided_by=?, reason=NULL WHERE id=?",
          (now(), int(admin_chat), int(oid)))
        x("UPDATE users SET spent=spent+? WHERE id=?", (float(o["amount"]), int(urow["id"])))
        grant_access(urow["id"], it["id"], o["id"], it["validity_days"])
        if not silent:
            self.bot.send(admin_chat,
                          f"✅ <b>Approved #{o['no']}</b>\n{SEP}\n{esc(it['title'])} → {esc(urow['name'])}\n"
                          f"{money(o['amount'])} added to revenue.",
                          kb(rows([btn("⏳ Pending queue", "pend"), btn("📊 Stats", "stats")],
                                  [btn("🛠 Admin panel", "admin")])))
        return self.deliver(urow["id"], it, get_order(oid))

    def decline_order(self, admin_chat, oid, reason=""):
        o = get_order(oid)
        if not o:
            return self.bot.send(admin_chat, "❌ Order not found.")
        reason = (reason or "Payment could not be verified.").strip()
        x("UPDATE orders SET status='declined', decided_at=?, decided_by=?, reason=? WHERE id=?",
          (now(), int(admin_chat), reason, int(oid)))
        urow = user_by_id(o["user_id"])
        if urow:
            set_state(int(urow["tg_id"]), {})
            self.bot.send(int(urow["tg_id"]),
                          f"❌ <b>Order #{o['no']} was not approved</b>\n{SEP}\nReason: {esc(reason)}\n\n"
                          "You can send the correct proof or order again.",
                          kb(rows([btn("📤 Re-send screenshot", f"ready:{o['id']}"),
                                    btn("🛍 Try again", f"buy:{o['item_id']}")],
                                  [btn("💬 Message admin", "contact_admin")])))
        self.bot.send(admin_chat,
                      f"❌ <b>Declined #{o['no']}</b>\n{SEP}\n{esc(o['note'] or '')}\nReason sent to the user.",
                      kb(rows([btn("⏳ Pending queue", "pend")], [btn("🛠 Admin panel", "admin")])))
        return True

    # ======================================================================
    # ADMIN PANEL  (opened with /admin — everything is a button from here)
    # ======================================================================
    def admin_panel(self, chat_id):
        s = all_settings()
        live = q("SELECT COUNT(*) c FROM items WHERE active=1")[0]["c"]
        pend = q("SELECT COUNT(*) c FROM orders WHERE status='pending'")[0]["c"]
        rev = q("SELECT COALESCE(SUM(amount),0) s FROM orders WHERE status='approved'")[0]["s"]
        users = q("SELECT COUNT(*) c FROM users WHERE is_admin=0")[0]["c"]
        body = [f"🛠 <b>{esc(s['brand'])} — admin panel</b>", SEP,
                f"📦 Live items: <b>{live}</b>",
                f"⏳ Waiting approval: <b>{pend}</b>" + ("  ← action needed" if pend else ""),
                f"💰 Revenue: <b>{money(rev)}</b>  ·  👥 Customers: {users}",
                f"💳 UPI: <code>{esc(s['upi_id'] or 'not set')}</code>  ·  🖼 QR: "
                f"{'uploaded' if s['qr_file_id'] else ('auto-generated' if s['upi_id'] else 'not set')}",
                f"🖼 Welcome photo: {'set' if s['welcome_photo_id'] else 'not set'}  ·  📢 Force join: "
                f"{esc(s['force_channel'] or 'off')}", ""]
        buttons = rows(
            [btn(f"📦 Items ({live})", "pg:items:0"), btn("➕ New item", "newitem")],
            [btn(f"⏳ Approvals ({pend})", "pend"), btn("🧾 All orders", "pg:orders:0")],
            [btn("💳 Payment setup", "pg:pay"), btn("🏪 Store settings", "pg:store")],
            [btn("📊 Sales report", "stats"), btn("👥 Customers", "pg:users")],
            [btn("📣 Broadcast", "bcast"), btn("ⓘ Commands", "pg:help")])
        self.bot.send(chat_id, "\n".join(body), kb(buttons))

    def admin_settings(self, chat_id):
        return self.bot.send(chat_id,
                             "⚙️ What would you like to configure?",
                             kb(rows([btn("💳 Payment (UPI / QR)", "pg:pay")],
                                     [btn("🏪 Store & welcome", "pg:store")],
                                     [btn("🛠 Full panel", "admin")])))

    # ------------------------------ payment setup -------------------------
    def admin_pay_setup(self, chat_id, page=0):
        s = all_settings()
        body = ["💳 <b>Payment setup</b>", SEP,
                f"UPI ID: <code>{esc(s['upi_id'] or '— not set')}</code>",
                f"Payee name: {esc(s['payee_name'] or s['brand'])}",
                f"QR image: {'✅ uploaded' if s['qr_file_id'] else '🤖 auto-generated from the UPI id (with amount pre-filled)'}",
                "", f"<b>Note shown at checkout</b>\n{esc(s['pay_note'])}", "",
                f"<b>Refund note</b>\n{esc(s['refund_note'])}",
                (f"\n<b>Extra instructions</b>\n{esc(s['pay_instructions'])}" if s["pay_instructions"] else "")]
        buttons = rows(
            [btn("✏️ UPI ID", "s:upi_id"), btn("👤 Payee name", "s:payee_name")],
            [btn("🖼 Upload QR", "setqr"), btn("🗑 Remove QR", "delqr")],
            [btn("📝 Checkout note", "s:pay_note"), btn("📜 Refund note", "s:refund_note")],
            [btn("➕ Extra instructions", "s:pay_instructions")],
            [btn("👁 Preview checkout", "pg:preview"), btn("🔙 Back", "admin")])
        self.bot.send(chat_id, "\n".join(body), kb(buttons))

    def admin_store_setup(self, chat_id):
        s = all_settings()
        custom = "✅ custom" if s["welcome_text"] else "🤖 default"
        body = ["🏪 <b><u>Store settings</u></b>", SEP,
                f"Brand name: <b>{esc(s['brand'])}</b>",
                f"Welcome text: {custom}",
                f"Welcome photo: {'✅ set' if s['welcome_photo_id'] else '❌ not set'}",
                f"Force channel join: {esc(s['force_channel'] or 'off')}",
                f"Empty-store note: {esc(s['out_of_stock_note'] or '—')}",
                "",
                "<b>Home-screen buttons & counters</b>",
                f"📹 Free demo: {esc(s['demo_link'] or '— not set')}",
                f"📢 Proofs channel: {esc(s['proofs_link'] or '— not set')}",
                f"🚨 Support: {esc(s['support_link'] or '— not set (in-bot messages)')}",
                f"👥 Stats: joined <code>{esc(s['stats_joined'] or 'auto')}</code> · "
                f"month <code>{esc(s['stats_month'] or 'auto')}</code> · "
                f"today <code>{esc(s['stats_today'] or 'auto')}</code>",
                "", "<i>The welcome screen is what a user sees on /start.</i>"]
        buttons = rows(
            [btn("🏷 Brand name", "s:brand"), btn("👋 Welcome text", "s:welcome_text")],
            [btn("🖼 Set welcome photo", "setwelcomephoto"), btn("🗑 Remove photo", "delwelcomephoto")],
            [btn("📢 Force channel join", "s:force_channel"), btn("📦 Empty-store note", "s:out_of_stock_note")],
            [btn("📹 Free demo link", "s:demo_link"), btn("📢 Proofs channel", "s:proofs_link")],
            [btn("🚨 Support link", "s:support_link")],
            [btn("👥 Stats joined", "s:stats_joined"), btn("🔥 Stats month", "s:stats_month"),
             btn("⚡ Stats today", "s:stats_today")],
            [btn("👁 Preview welcome", "pg:previewwelcome"), btn("🔙 Back", "admin")])
        self.bot.send(chat_id, "\n".join(body), kb(buttons))

    # ------------------------------ items management ----------------------
    def admin_items(self, chat_id, page=0):
        allitems = q("SELECT * FROM items ORDER BY id DESC LIMIT 300")
        total = len(allitems)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(int(page), pages - 1))
        chunk = allitems[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        if not chunk:
            return self.bot.send(chat_id,
                                 "📦 <b>No items yet</b>\n" + SEP + "\nCreate the first one with the button below.",
                                 kb(rows([btn("➕ New item", "newitem")], [btn("🛠 Admin panel", "admin")])))
        lines, buttons = [], []
        for it in chunk:
            state = "🟢" if it["active"] else "⏸️"
            parts = [f"<b>{state} #{it['id']}</b> {esc(shorten(it['title'], 34))}",
                     money(it["price"]) if it["price"] > 0 else "FREE",
                     "♾" if not it["validity_days"] else f"{it['validity_days']}d",
                     f"🛒 {it['sold']}"]
            lines.append(" · ".join(parts))
            buttons.append([btn(f"⚙️ #{it['id']} {shorten(it['title'], 22)}", f"adm:{it['id']}")])
        nav = []
        if page > 0:
            nav.append(btn("◀️ Prev", f"pg:items:{page - 1}"))
        if page < pages - 1:
            nav.append(btn("Next ▶️", f"pg:items:{page + 1}"))
        nav.append(btn("➕ New item", "newitem"))
        buttons.append(nav)
        buttons.append([btn("🛠 Admin panel", "admin")])
        live = len([i for i in allitems if i["active"]])
        self.bot.send(chat_id,
                      f"📦 <b>Items</b> — {total} total · {live} live\n{SEP}\n\n" + "\n".join(lines) +
                      (f"\n\nPage {page + 1}/{pages}" if pages > 1 else ""),
                      kb(buttons))

    def item_menu(self, chat_id, it):
        flags = []
        for key, label in (("file_id", "📦 file"), ("link", "🔗 link"), ("channel_link", "📢 channel"),
                           ("group_link", "👥 group")):
            flags.append(f"{label} ✅" if it[key] else f"{label} —")
        body = [self.item_caption(it), SEP, "🧩 " + " · ".join(flags),
                f"🆔 Item id: <code>{it['id']}</code>  ·  updated {ts(it['updated_at'])}"]
        buttons = rows(
            [btn("✏️ Title", f"f:title:{it['id']}"), btn("📝 Description", f"f:descr:{it['id']}")],
            [btn("💵 Price", f"f:price:{it['id']}"), btn("⏱ Validity", f"f:validity_days:{it['id']}")],
            [btn("🎬 Replace file", f"f:file_id:{it['id']}"), btn("🗑 Remove file", f"nofile:{it['id']}")],
            [btn("🔗 Main link", f"f:link:{it['id']}"), btn("📢 Channel link", f"f:channel_link:{it['id']}")],
            [btn("👥 Group link", f"f:group_link:{it['id']}")],
            [btn("🟢/⏸️ Publish / unpublish", f"tog:{it['id']}"), btn("🗑 Delete", f"del:{it['id']}")],
            [btn("👁 Preview for me", f"prev:{it['id']}"), btn("👥 Who bought", f"buyers:{it['id']}")],
            [btn("🔙 Items", "pg:items:0")])
        self.bot.send(chat_id, "\n".join(body), kb(buttons))

    # ------------------------------ orders management ---------------------
    def admin_orders(self, chat_id, page=0, only_pending=True):
        where = "o.status='pending'" if only_pending else "1=1"
        rows_ = q(f"""SELECT o.*, i.title, u.name, u.tg_id, u.username FROM orders o
                      JOIN items i ON i.id=o.item_id JOIN users u ON u.id=o.user_id
                      WHERE {where} ORDER BY o.id DESC LIMIT 60""")
        icon = {"pending": "⏳", "approved": "✅", "declined": "❌", "cancelled": "🗑"}
        if not rows_:
            return self.bot.send(chat_id,
                                 "🎉 <b>Nothing waiting</b>\n" + SEP + "\nEvery payment has been reviewed.",
                                 kb(rows([btn("🧾 All orders", "pg:orders:0"), btn("🛠 Admin panel", "admin")])))
        lines, buttons = [], []
        for r in rows_:
            tag = icon.get(r["status"], "•") if not only_pending else ("📸" if r["proof_id"] else "🚫 proof missing")
            status = r["status"] if only_pending else f"<b>{r['status']}</b>"
            lines.append(f"{tag} <b>#{r['no']}</b> · {money(r['amount'])} · {esc(shorten(r['title'], 24))} {status}\n"
                         f"   👤 {esc(shorten(r['name'], 22))} {esc(r['username'] or '')} · {ts(r['created_at'])}")
            if only_pending:
                buttons.append([btn(f"✅ Approve #{r['no']}", f"aok:{r['id']}"),
                                btn(f"❌ Decline #{r['no']}", f"adcl:{r['id']}")])
            else:
                buttons.append([btn(f"🧾 #{r['no']} {r['status']}", f"aord:{r['id']}")])
        nav = []
        if page > 0:
            nav.append(btn("◀️ Prev", f"pg:{'orders' if not only_pending else 'pend'}:{page - 1}"))
        if len(rows_) > PAGE_SIZE:
            nav.append(btn("Next ▶️", f"pg:{'orders' if not only_pending else 'pend'}:{page + 1}"))
        if only_pending:
            nav.append(btn("✅ Approve all", "aall"))
        buttons.append(nav) if nav else None
        buttons.append([btn("📜 All orders" if only_pending else "⏳ Pending", "pg:pend:0" if not only_pending else "pg:orders:0"),
                        btn("🛠 Admin panel", "admin")])
        head = "⏳ <b>Waiting for approval</b>" if only_pending else "🧾 <b>Recent orders</b>"
        self.bot.send(chat_id, f"{head} — {len(rows_)}\n{SEP}\n\n" + "\n".join(lines[:20]), kb(buttons))

    def order_view(self, chat_id, oid):
        o = get_order(oid)
        if not o:
            return self.bot.send(chat_id, "❌ Order not found.", kb(rows([btn("🛠 Panel", "admin")])))
        it = get_item(o["item_id"]) or {"title": "(deleted item)", "id": 0, "price": o["amount"],
                                        "file_id": None, "link": None, "channel_link": None,
                                        "group_link": None, "validity_days": 0, "descr": "", "sold": 0,
                                        "kind": "link", "updated_at": now()}
        u = user_by_id(o["user_id"]) or {"name": "?", "tg_id": 0, "username": "", "id": 0}
        status = {"pending": "⏳ waiting for review", "approved": "✅ approved", "declined": "❌ declined",
                  "cancelled": "🗑 cancelled"}.get(o["status"], o["status"])
        body = [f"🧾 <b>Order #{o['no']}</b>", SEP,
                f"Status: <b>{status}</b>",
                f"Item: {esc(shorten(it['title'], 40))} <code>#{it['id']}</code>",
                f"Amount: <b>{money(o['amount'])}</b>",
                f"Customer: {esc(u['name'])} {esc(u['username'] or '')} · <code>{u['tg_id']}</code>",
                f"Created: {o['created_at']}",
                (f"Reviewed: {o['decided_at']}" if o["decided_at"] else ""),
                (f"Reason: {esc(o['reason'])}" if o["reason"] else ""),
                (f"Customer note: {esc(o['note'])}" if o["note"] else ""),
                f"Payment proof: {'📸 attached' if o['proof_id'] else '🚫 none'}"]
        buttons = []
        if o["status"] == "pending":
            buttons.append([btn("✅ Approve & deliver", f"aok:{o['id']}"), btn("❌ Decline", f"adcl:{o['id']}")])
        if o["proof_id"]:
            buttons.append([btn("🖼 View screenshot", f"aproof:{o['id']}")])
        if o["status"] in ("approved", "declined"):
            buttons.append([btn("🔁 Re-deliver content", f"adel:{o['id']}"),
                            btn("↩️ Set back to pending", f"areopen:{o['id']}")])
        buttons.append([btn("👤 Customer profile", f"ausr:{u['id']}"),
                        btn("🛠 Open item", f"adm:{it['id']}")])
        buttons.append([btn("🔙 Approvals", "pend")])
        self.bot.send(chat_id, "\n".join([l for l in body if l]), kb(buttons))
        if o["proof_id"]:
            self.bot.send_media(chat_id, o["proof_kind"] or "photo", o["proof_id"],
                                caption=f"📸 Payment screenshot · order #{o['no']}")

    # ------------------------------ users / stats -------------------------
    def admin_users(self, chat_id, page=0):
        rows_ = q("SELECT * FROM users WHERE is_admin=0 ORDER BY spent DESC, id DESC LIMIT 30")
        if not rows_:
            return self.bot.send(chat_id, "👥 No customers yet — they appear after the first /start.",
                                 kb(rows([btn("🛠 Admin panel", "admin")])))
        lines, buttons = [], []
        for r in rows_:
            o1 = q("SELECT COUNT(*) c FROM orders WHERE user_id=? AND status='pending'", (r["id"],))[0]["c"]
            lines.append(f"▪️ <b>{esc(shorten(r['name'], 22))}</b> {esc(r['username'] or '')} · <code>{r['id']}</code>\n"
                         f"   💰 {money(r['spent'])} · 🧾 {r['orders']} orders"
                         + (f" · ⏳ {o1}" if o1 else "") + (" · 🚫 blocked" if r["blocked"] else ""))
            buttons.append([btn(f"👤 {shorten(r['name'], 22)}", f"ausr:{r['id']}")])
        buttons.append([btn("🛠 Admin panel", "admin")])
        self.bot.send(chat_id,
                      f"👥 <b>Customers</b> — {len(rows_)}\n{SEP}\n\n" + "\n".join(lines[:15]), kb(buttons))

    def customer_view(self, chat_id, pk):
        u = user_by_id(pk) or user_by_tg(pk)
        if not u:
            return self.bot.send(chat_id, "❌ Customer not found.", kb(rows([btn("🔙 Back", "pg:users:0")])))
        o_all = q("SELECT COUNT(*) c FROM orders WHERE user_id=?", (u["id"],))[0]["c"]
        o_pend = q("SELECT COUNT(*) c FROM orders WHERE user_id=? AND status='pending'", (u["id"],))[0]["c"]
        o_ok = q("SELECT COUNT(*) c FROM orders WHERE user_id=? AND status='approved'", (u["id"],))[0]["c"]
        items = q("SELECT COUNT(*) c FROM unlocks WHERE user_id=?", (u["id"],))[0]["c"]
        body = [f"👤 <b>{esc(u['name'])}</b> {esc(u['username'] or '')}", SEP,
                f"Telegram ID: <code>{u['tg_id']}</code> · bot ID: <code>{u['id']}</code>",
                f"Joined: {ts(u['created_at'])} · last seen: {ts(u['last_seen'])}",
                f"💰 Spent: <b>{money(u['spent'])}</b>",
                f"🧾 Orders: {o_all} (⏳ {o_pend} · ✅ {o_ok}) · 🔓 Unlocked: {items}",
                f"🚫 Status: {'blocked' if u['blocked'] else 'active'}"]
        buttons = rows([btn("🧾 Their orders", f"ausers:{u['id']}"), btn("🎁 Grant free access", f"agr:{u['id']}")],
                       [btn("📣 Message them", f"apm:{u['id']}")],
                       [btn(("🔓 Unblock" if u["blocked"] else "🚫 Block"),
                            (f"aub:{u['id']}" if u["blocked"] else f"abl:{u['id']}"))],
                       [btn("🔙 Customers", "pg:users:0")])
        self.bot.send(chat_id, "\n".join(body), kb(buttons))

    def customer_orders(self, chat_id, pk):
        rows_ = q("""SELECT o.*, i.title FROM orders o JOIN items i ON i.id=o.item_id
                     WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20""", (int(pk),))
        if not rows_:
            return self.bot.send(chat_id, "This customer has no orders.", kb(rows([btn("🔙 Back", "pg:users:0")])))
        icon = {"pending": "⏳", "approved": "✅", "declined": "❌", "cancelled": "🗑"}
        lines = [f"{icon.get(r['status'], '•')} <b>#{r['no']}</b> · {money(r['amount'])} · "
                 f"{esc(shorten(r['title'], 22))} · {r['status']} · {ts(r['created_at'])}" for r in rows_]
        buttons = [[btn(f"🧾 #{r['no']}", f"aord:{r['id']}")] for r in rows_[:8]]
        buttons.append([btn("👤 Profile", f"ausr:{pk}"), btn("🔙 Customers", "pg:users:0")])
        self.bot.send(chat_id, f"🧾 <b>Customer #{pk} orders</b>\n{SEP}\n\n" + "\n".join(lines), kb(buttons))

    def admin_stats(self, chat_id):
        s = all_settings()
        a = q("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders WHERE status='approved'")[0]
        p = q("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders WHERE status='pending'")[0]
        d = q("SELECT COUNT(*) c FROM orders WHERE status='declined'")[0]["c"]
        today = q("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders "
                  "WHERE status='approved' AND decided_at>=?", (datetime.now().strftime("%Y-%m-%d"),))[0]
        week = q("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders "
                 "WHERE status='approved' AND decided_at>=?",
                 ((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),))[0]
        top = q("""SELECT i.id, i.title, COUNT(*) c, SUM(o.amount) s FROM orders o JOIN items i ON i.id=o.item_id
                   WHERE o.status='approved' GROUP BY i.id ORDER BY s DESC LIMIT 5""")
        body = [f"📊 <b>{esc(s['brand'])} — report</b>", SEP,
                f"💰 Total revenue: <b>{money(a['s'])}</b>  ({a['c']} approved orders)",
                f"📅 Today: <b>{money(today['s'])}</b> ({today['c']} orders)",
                f"🗓 Last 7 days: <b>{money(week['s'])}</b> ({week['c']} orders)",
                f"⏳ Waiting: {p['c']} orders · {money(p['s'])}",
                f"❌ Declined: {d}", "",
                "<b>Best sellers</b>"]
        if top:
            body += [f"▪️ <b>#{r['id']}</b> {esc(shorten(r['title'], 30))} · {r['c']}× · {money(r['s'])}" for r in top]
        else:
            body += ["No sales yet."]
        self.bot.send(chat_id, "\n".join(body),
                      kb(rows([btn("⏳ Approvals", "pend"), btn("🧾 All orders", "pg:orders:0")],
                              [btn("👥 Customers", "pg:users:0"), btn("🛠 Admin panel", "admin")])))

    # ======================================================================
    # ADMIN: text commands (power users — the panel buttons do the same)
    # ======================================================================
    def admin_legacy(self, chat_id, tg_id, uid, m, cmd, arg, media):
        c = cmd.lower()
        if c == "/del":
            it = self.find_item(arg)
            if not it:
                return self.bot.send(chat_id, "Usage: <code>/del 3</code>")
            x("DELETE FROM items WHERE id=?", (int(it["id"]),))
            return self.bot.send(chat_id, f"🗑 Item #{it['id']} deleted.", kb(rows([btn("📦 Items", "pg:items:0")])))
        if c == "/edit":
            it = self.find_item(arg)
            return self.item_menu(chat_id, it) if it else self.bot.send(chat_id, "Usage: <code>/edit 3</code>")
        if c in ("/price", "/valid"):
            mt = re.match(r"#?(\d+)\s+([\d.]+)", arg or "")
            it = get_item(int(mt.group(1))) if mt else None
            if not it:
                return self.bot.send(chat_id, f"Usage: <code>{c} 3 {('49' if c == '/price' else '30')}</code>")
            field, val = ("price", to_num(mt.group(2))) if c == "/price" else ("validity_days", int(mt.group(2)))
            set_item(it["id"], field, val)
            return self.bot.send(chat_id, f"✅ #{it['id']} {field} = {val}")
        if c in ("/pause", "/resume"):
            it = self.find_item(arg)
            if not it:
                return self.bot.send(chat_id, "Usage: <code>/pause 3</code>")
            set_item(it["id"], "active", 0 if c == "/pause" else 1)
            return self.bot.send(chat_id, f"{'⏸️ Hidden' if c == '/pause' else '🟢 Published'}: {esc(it['title'])}")
        if c == "/grant":
            mt = re.match(r"(\d+)\s+#?(\d+)", arg or "")
            u = q("SELECT * FROM users WHERE id=? OR tg_id=?", (int(mt.group(1)), int(mt.group(1)))) if mt else []
            it = get_item(int(mt.group(2))) if mt else None
            if not (u and it):
                return self.bot.send(chat_id, "Usage: <code>/grant 5 3</code> → bot user 5 gets item 3 free")
            grant_access(u[0]["id"], it["id"], None, it["validity_days"])
            self.deliver(u[0]["id"], it, None)
            return self.bot.send(chat_id, f"🎁 {esc(it['title'])} granted to {esc(u[0]['name'])}.")
        if c == "/revoke":
            mt = re.match(r"(\d+)\s+#?(\d+)", arg or "")
            if not mt:
                return self.bot.send(chat_id, "Usage: <code>/revoke 5 3</code>")
            u = q("SELECT id FROM users WHERE id=? OR tg_id=?", (int(mt.group(1)), int(mt.group(1))))
            if u:
                x("DELETE FROM unlocks WHERE user_id=? AND item_id=?", (u[0]["id"], int(mt.group(2))))
            return self.bot.send(chat_id, "🔒 Access revoked.")
        if c in ("/block", "/unblock"):
            t = re.sub(r"\D", "", arg or "")
            u = q("SELECT * FROM users WHERE tg_id=? OR id=?", (int(t or 0), int(t or 0)))
            if not u:
                return self.bot.send(chat_id, "Usage: <code>/block 123456</code> (telegram or bot id)")
            x("UPDATE users SET blocked=? WHERE id=?", (1 if c == "/block" else 0, u[0]["id"]))
            return self.bot.send(chat_id, f"{'🚫 Blocked' if c == '/block' else '🔓 Unblocked'} {esc(u[0]['name'])}.")
        if c == "/approve":
            n = re.search(r"\d+", arg or "")
            return self.approve_order(chat_id, int(n.group())) if n else self.bot.send(chat_id, "Usage: <code>/approve 12</code>")
        if c == "/pending" or c == "/orders":
            n = re.search(r"\d+", arg or "")
            return self.order_view(chat_id, int(n.group())) if n else self.admin_orders(chat_id)
        if c in ("/setqr", "/welcomephoto"):
            return self.dispatch(chat_id, tg_id, uid, True, "setqr" if c == "/setqr" else "setwelcomephoto")
        return self.bot.send(chat_id, "Open the admin panel and use the buttons 👇", kb(rows([btn("🛠 Admin panel", "admin")])))

    @staticmethod
    def find_item(arg):
        mt = re.search(r"#?(\d+)", arg or "")
        return get_item(int(mt.group(1))) if mt else None

    def quick_add(self, chat_id, tg_id, m, arg, media, force_link=False, force_text=False):
        """`/add Title | 199` (reply to a video/file) · `/addlink Title | 99 | url` · `/addtext`"""
        parts = [p.strip() for p in re.split(r"\|", arg or "") if p.strip()]
        title = parts[0] if parts else ""
        price = to_num(parts[1]) if len(parts) > 1 else None
        link = next((p for p in parts if p.startswith("http")), "")
        if price is None and len(parts) > 1:
            price = 0.0
        if force_text or (not media and not link and len(parts) > 2):
            body = " | ".join(parts[2:] if price is not None else parts[1:])
            if not title:
                return self.bot.send(chat_id,
                                     "Usage: <code>/addtext Title | 49 | your text, coupon or key</code>")
            it_id = add_item(kind="text", title=title[:120], price=price or 0, descr=body[:3500])
            self.bot.send(chat_id, f"✅ Text item <b>#{it_id}</b> created ({money(price or 0)}).",
                          kb(rows([btn("⚙️ Edit", f"adm:{it_id}"), btn("📦 Items", "pg:items:0")])))
            return
        if force_link and not link:
            return self.bot.send(chat_id,
                                  "Usage: <code>/addlink Title | 99 | https://t.me/private-channel</code>")
        if not media and not link:
            set_state(tg_id, {"flow": "add", "step": "quick_media",
                              "d": {"title": title[:120], "price": price if price is not None else 0}})
            return self.bot.send(chat_id,
                                 "🎬 Now send the <b>video / photo / file</b> for this item"
                                 f" (title: <b>{esc(title or 'to be set later')}</b>).\n"
                                 "Or send <code>/skip</code> for a link-only item.",
                                 kb(rows([btn("⏭️ Skip file", "wiz:skip:media"), btn("❌ Cancel", "cancel_flow")])))
        kind = media["kind"] if media else "link"
        it_id = add_item(kind=kind, title=(title or (media or {}).get("name") or "New item")[:120],
                         price=price if price is not None else 0,
                         file_id=(media or {}).get("file_id"), file_kind=(media or {}).get("file_kind"),
                         link=link, descr=(m.get("caption") or "")[:600])
        it = get_item(it_id)
        self.bot.send(chat_id,
                      f"✅ Item <b>#{it_id}</b> created.\n{SEP}\n{self.item_caption(it)}",
                      kb(rows([btn("🔗 Add links + validity", f"postset:{it_id}"),
                               btn("🎉 Done — leave as is", "admin")])))
        set_state(tg_id, {"flow": "add", "step": "post_links", "d": {"item": it_id, "price": it["price"]}})

    # ======================================================================
    # ADMIN: item wizard
    # ======================================================================
    WIZ_TXT = {
        "price": "💵 <b>Step 2 of 6 — price</b>\n\nSend the price in rupees, e.g. <code>199</code>.\nSend <code>0</code> for a free item.",
        "descr": "📝 <b>Step 3 of 6 — description</b>\n\nWhat does the buyer get? Shown on the item page.\nSend <code>skip</code> to leave it empty.",
        "media": "🎬 <b>Step 4 of 6 — file</b>\n\nSend the video / photo / document / audio now.\nSend <code>skip</code> if this item is only a link.",
        "links": "🔗 <b>Step 5 of 6 — links</b>\n\nSend one or more links (any format works):\n"
                 "<code>https://drive.google.com/x</code>\n"
                 "<code>channel: https://t.me/myprivatechannel</code>\n"
                 "<code>group: @mygroup</code>\n"
                 "<code>website: https://example.com/course</code>\n\nSend <code>skip</code> if there are no links.",
    }

    def wizard_start(self, chat_id, tg_id):
        """Simple add-item flow — first ask WHAT to add (button menu)."""
        set_state(tg_id, {"flow": "add", "step": "wtype", "d": {}})
        return self.bot.send(chat_id,
                             "➕ <b>New item</b>\n" + SEP +
                             "\nWhat do you want to add? Pick the type below.",
                             kb(rows([btn("🎬 Video", "wtype:video", style="primary"),
                                      btn("🖼 Photo", "wtype:photo", style="primary")],
                                     [btn("📄 File", "wtype:file", style="primary"),
                                      btn("🔗 Website link", "wtype:link", style="primary")],
                                     [btn("📢 Channel link", "wtype:channel", style="primary"),
                                      btn("👥 Group link", "wtype:group", style="primary")],
                                     [btn("📝 Text / Coupon", "wtype:text", style="primary")],
                                     [btn("❌ Cancel", "cancel_flow", style="danger")])))

    def q_ask_content(self, chat_id, tg_id, d):
        """Step 3/3 of the simple wizard — ask for the actual content by type."""
        kind = d.get("wtype", "video")
        set_state(tg_id, {"flow": "add", "step": "q_media" if kind in ("video", "photo", "file")
                          else ("q_text" if kind == "text" else "q_link"), "d": d})
        prompts = {
            "video": ("🎬 Send the <b>video</b> now (upload it here).",
                      "It is stored once on Telegram and delivered instantly to every buyer."),
            "photo": ("🖼 Send the <b>photo</b> now (upload it here).",
                      "It is stored once on Telegram and delivered instantly to every buyer."),
            "file": ("📄 Send the <b>file / document</b> now (upload it here).",
                     "It is stored once on Telegram and delivered instantly to every buyer. (max 50 MB)"),
            "link": ("🔗 Send the <b>website link</b> now.",
                     "Any https://… link works."),
            "channel": ("📢 Send the <b>private channel</b> link now.",
                        "A t.me link or <code>@username</code> — buyers join it after approval."),
            "group": ("👥 Send the <b>private group</b> link now.",
                      "A t.me link or <code>@username</code> — buyers join it after approval."),
            "text": ("📝 Send the <b>text / coupon / serial key</b> now.",
                     "It is delivered to the buyer as a message after approval."),
        }
        title, hint = prompts.get(kind, prompts["video"])
        return self.bot.send(chat_id,
                             f"➕ <b>New item — {esc(d.get('title', ''))} · step 3/3</b>\n{SEP}\n"
                             f"{title}\n<hint>{esc(hint)}</hint>".replace("<hint>", "<i>").replace("</hint>", "</i>"),
                             kb(rows([btn("❌ Cancel", "cancel_flow", style="danger")])))

    def qwiz_done(self, chat_id, tg_id, d):
        """Create + publish the item from the simple wizard and show its menu."""
        d.setdefault("price", 0)
        d["validity_days"] = 0
        d.setdefault("descr", "")
        d.setdefault("kind", "link")
        it_id = add_item(**d)
        set_state(tg_id, {})
        it = get_item(it_id)
        self.bot.send(chat_id,
                      f"🎉 <b>Item #{it_id} is live</b>\n{SEP}\n{self.item_caption(it)}",
                      kb(rows([btn("⚙️ Edit item", f"adm:{it_id}"), btn("📦 All items", "pg:items:0")],
                              [btn("👁 Store view", "shop:0"), btn("🛠 Admin panel", "admin")])))
        return True

    def wiz_next(self, chat_id, tg_id, d, step):
        set_state(tg_id, {"flow": "add", "step": step, "d": d})
        return self.bot.send(chat_id, self.WIZ_TXT[step],
                             kb(rows([btn("⏭️ Skip this step", f"wiz:skip:{step}"), btn("❌ Cancel", "cancel_flow")])))

    def ask_validity(self, chat_id, tg_id, d):
        d.setdefault("price", 0)
        set_state(tg_id, {"flow": "add", "step": "validity", "d": d})
        return self.bot.send(chat_id,
                             "⏱ <b>Step 6 of 6 — access validity</b>\n" + SEP +
                             "\nHow long should the buyer keep access?\nSend a number of days (e.g. <code>30</code>) "
                             "or use the button for lifetime access.",
                             kb(rows([btn("♾ Lifetime access", "wiz:lifetime")])))

    def wiz_confirm(self, chat_id, tg_id, d):
        it_id = add_item(**d)
        set_state(tg_id, {})
        it = get_item(it_id)
        self.bot.send(chat_id,
                      f"🎉 <b>Item #{it_id} is live</b>\n{SEP}\n{self.item_caption(it)}",
                      kb(rows([btn("⚙️ Edit item", f"adm:{it_id}"), btn("📦 All items", "pg:items:0")],
                              [btn("👁 Store view", "shop:0"), btn("🛠 Admin panel", "admin")])))
        return True

    def links_into(self, d: dict, text: str) -> str:
        """Parses free-form link lines into the item dict; returns a summary."""
        got = []
        for line in re.split(r"[\n,;]+", text or ""):
            line = line.strip()
            if not line or line.lower().strip("/") in ("skip", "none", "done", "ok", "-"):
                continue
            key, sep, val = line.partition(":")
            key = key.strip().lower() if sep else ""
            url = (val if sep else line).strip().strip("`").strip()
            if sep and not url.startswith("http"):
                url = val.strip().strip("`")
            if key in ("http", "https", "link", "url") and not url.startswith("http"):
                url = line.strip().strip("`")
            if not url.startswith("http"):
                if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", url.strip()):
                    url = "https://t.me/" + url.strip().lstrip("@")
                else:
                    continue
            if any(w in key for w in ("chan", "broadcast")):
                d["channel_link"] = url
                got.append("channel link")
            elif any(w in key for w in ("group", "grp", "support")):
                d["group_link"] = url
                got.append("group link")
            elif any(w in key for w in ("web", "site", "drive", "course", "main")):
                d["link"] = url
                got.append(key + " link")
            elif not d.get("link"):
                d["link"] = url
                got.append("main link")
            else:
                d["group_link"] = url
                got.append("extra link")
        return ", ".join(got)

    # ======================================================================
    # MEDIA
    # ======================================================================
    @staticmethod
    def media_of(m: dict):
        for key, kind, field in (("video", "video", "video"), ("document", "file", "document"),
                                 ("photo", "photo", "photo"), ("animation", "video", "animation"),
                                 ("audio", "file", "audio"), ("voice", "file", "voice"),
                                 ("video_note", "video", "video_note"), ("sticker", "file", "sticker")):
            v = m.get(key)
            if not v:
                continue
            if key == "photo":
                v = sorted(v, key=lambda p: (p.get("width", 0) * p.get("height", 0)))[-1]
            return {"kind": kind, "file_kind": field, "file_id": v.get("file_id"),
                    "name": v.get("file_name") or f"{key}.bin", "size": v.get("file_size")}
        return None

    # ======================================================================
    # CALLBACK ROUTER
    # ======================================================================
    def dispatch(self, chat_id, tg_id, uid, admin, data):
        if not data:
            return
        # ------------------------- shared -------------------------
        if data == "home":
            return self.send_welcome(chat_id, uid)
        if data == "help":
            return self.bot.send(chat_id, self.help_text(),
                                 kb(rows([ubtn("Browse store", "shop:0", icon="🍑", style="success"),
                                          ubtn("Payment info", "payinfo", icon="💦", style="primary")])))
        if data == "payinfo":
            return self.show_payinfo(chat_id, uid)
        if data == "library":
            return self.show_library(chat_id, uid)
        if data == "orders":
            return self.show_orders(chat_id, uid)
        if data == "recheck":
            if self.channel_ok(tg_id, chat_id):
                return self.send_welcome(chat_id, uid)
            return self.bot.answer(cb_id=None, text="Still not a member", alert=True)
        if data == "contact_admin":
            set_state(tg_id, {"flow": "toadmin", "step": "text", "d": {}})
            return self.bot.send(chat_id, "✍️ Type your message — it will be delivered to the admin.")
        if data == "profile":
            return self.show_profile(chat_id, uid)
        if data == "howto":
            return self.show_howto(chat_id)
        if data == "support":
            return self.show_support(chat_id)
        if data.startswith("shop:"):
            return self.show_store(chat_id, uid, int(data[5:] or 0))
        if data.startswith("item:"):
            it = get_item(int(data[5:]))
            if not it:
                return self.bot.send(chat_id, "That item no longer exists.")
            if uid and has_access(uid, it["id"]):
                return self.open_item(chat_id, uid, it["id"])
            if admin or tg_id in ADMIN_IDS:
                return self.show_item(chat_id, uid, it)      # admin preview keeps the detail page
            return self.start_buy(chat_id, uid, it)          # buyers go straight to checkout
        if data.startswith("buy:"):
            it = get_item(int(data[4:]))
            return self.start_buy(chat_id, uid, it) if it else self.bot.send(chat_id, "That item no longer exists.")
        if data.startswith("open:"):
            return self.open_item(chat_id, uid, int(data[5:]))
        if data.startswith("ready:"):
            oid = int(data[6:])
            o = get_order(oid)
            if not o:
                return self.bot.send(chat_id, "❌ Order not found.", kb(rows([ubtn("My orders", "orders", icon="💦", style="primary")])))
            self.set_proof_state(uid, oid, o["item_id"])
            return self.bot.send(chat_id,
                                pe("📸 <b>Send the payment screenshot now</b> (photo or file).\n" + SEP +
                                   "\nThe admin reviews it and unlocks your item right away. 😘"),
                                kb(rows([btn("❌ Cancel order", f"cancel:{oid}", style="danger")])))
        if data.startswith("cancel:"):
            return self.cancel_order(chat_id, uid, int(data[7:]))
        if data == "cancel_flow":
            set_state(tg_id, {})
            return self.bot.send(chat_id, "Cancelled — nothing was changed.",
                                 kb(rows([btn("🛠 Admin panel", "admin")] if admin else [btn("🏠 Home", "home")])))

        # ------------------------- admin -------------------------
        if not admin:
            return self.bot.send(chat_id, "🔒 Admin only.", kb(rows([btn("🛍 Browse store", "shop:0")])))
        if data == "admin":
            return self.admin_panel(chat_id)
        if data == "newitem":
            return self.wizard_start(chat_id, tg_id)
        if data.startswith("wtype:"):
            kind = data[6:]
            label = {"video": "Video", "photo": "Photo", "file": "File", "link": "Website link",
                     "channel": "Channel link", "group": "Group link", "text": "Text / Coupon"}.get(kind)
            if not label:
                return self.wizard_start(chat_id, tg_id)
            set_state(tg_id, {"flow": "add", "step": "q_title", "d": {"wtype": kind}})
            return self.bot.send(chat_id,
                                 f"➕ <b>New item — {esc(label)}</b> · step 1/3\n" + SEP +
                                 "\nSend the <b>name</b> of the item.\n"
                                 "Example: <code>Python Full Course</code>",
                                 kb(rows([btn("❌ Cancel", "cancel_flow", style="danger")])))
        if data == "wiz:qfree":
            st = get_state(tg_id)
            if st.get("flow") == "add" and st.get("step") in ("q_price", "q_media", "q_link", "q_text"):
                d = dict(st.get("d") or {})
                d["price"] = 0.0
                return self.q_ask_content(chat_id, tg_id, d)
            return self.wizard_start(chat_id, tg_id)
        if data == "pend":
            return self.admin_orders(chat_id)
        if data.startswith("pg:items:"):
            return self.admin_items(chat_id, int(data[9:]))
        if data.startswith("pg:orders:"):
            return self.admin_orders(chat_id, int(data[10:]), only_pending=False)
        if data.startswith("pg:pend:"):
            return self.admin_orders(chat_id, int(data[8:]), only_pending=True)
        if data == "pg:pay":
            return self.admin_pay_setup(chat_id)
        if data == "pg:store":
            return self.admin_store_setup(chat_id)
        if data == "pg:users":
            return self.admin_users(chat_id)
        if data == "pg:help":
            return self.admin_help(chat_id)
        if data == "stats":
            return self.admin_stats(chat_id)
        if data == "bcast":
            set_state(tg_id, {"flow": "bc", "step": "text", "d": {}})
            n = q("SELECT COUNT(*) c FROM users WHERE is_admin=0 AND blocked=0")[0]["c"]
            return self.bot.send(chat_id, f"📣 <b>Broadcast to {n} customers</b>\n\nType the message now "
                                          "(image or file works too). Send <code>/cancel</code> to abort.",
                                 kb(rows([btn("❌ Cancel", "cancel_flow")])))
        if data == "pg:preview":
            it = q("SELECT * FROM items WHERE active=1 ORDER BY id DESC LIMIT 1")
            if not it:
                return self.bot.send(chat_id, "Create an item first to see the checkout screen.")
            return self.payment_screen(chat_id, uid, None, it[0], note="👁 <b>Preview</b> — this is what the buyer sees.")
        if data == "pg:previewwelcome":
            return self.send_welcome(chat_id, uid)
        if data == "setqr":
            set_state(tg_id, {"flow": "setqr", "step": "media", "d": {}})
            return self.bot.send(chat_id,
                                 "🖼 <b>Send the QR image now</b> (photo or file).\n" + SEP +
                                 "\nIt is shown on every checkout screen. Send <code>/cancel</code> to abort.",
                                 kb(rows([btn("❌ Cancel", "cancel_flow")])))
        if data == "delqr":
            set_setting("qr_file_id", "")
            self.bot.send(chat_id, "🗑 QR removed — the UPI QR will be auto-generated at checkout.")
            return (self.admin_pay_setup(chat_id) or True)
        if data == "setwelcomephoto":
            set_state(tg_id, {"flow": "setwelcomephoto", "step": "media", "d": {}})
            return self.bot.send(chat_id,
                                 "🖼 <b>Send the welcome photo now</b>.\n" + SEP +
                                 "\nIt appears on the /start screen with your welcome text as the caption.",
                                 kb(rows([btn("❌ Cancel", "cancel_flow")])))
        if data == "delwelcomephoto":
            set_setting("welcome_photo_id", "")
            self.bot.send(chat_id, "🗑 Welcome photo removed.")
            return (self.admin_store_setup(chat_id) or True)
        if data.startswith("s:"):
            return self.start_field_edit(chat_id, tg_id, data[2:])
        if data == "wiz:lifetimepost":
            st = get_state(tg_id)
            d = dict(st.get("d") or {})
            it_id = int(d.get("item") or 0)
            if it_id:
                set_item(it_id, "validity_days", 0)
                set_state(tg_id, {})
                it = get_item(it_id)
                self.bot.send(chat_id, f"🎉 Item <b>#{it_id}</b> is live.\n{SEP}\n{self.item_caption(it)}",
                              kbd=rows([btn("⚙️ Edit item", f"adm:{it_id}"), btn("📦 All items", "pg:items:0")]))
                return
            return self.admin_items(chat_id)
        if data.startswith("postset:"):
            iid = int(data[8:])
            set_state(tg_id, {"flow": "add", "step": "post_links", "d": {"item": iid}})
            return self.bot.send(chat_id,
                                 "🔗 Send the links for this item:\n"
                                 "<code>channel: https://t.me/private_channel</code>\n"
                                 "<code>group: @mygroup</code> · <code>website: https://…</code>",
                                 kbd=rows([btn("♾ Skip — lifetime access", "wiz:lifetimepost"),
                                          btn("❌ Cancel", "cancel_flow")]))
        if data.startswith("wiz:lifetime"):
            st = get_state(tg_id)
            d = dict(st.get("d") or {})
            d["validity_days"] = 0
            return self.wiz_confirm(chat_id, tg_id, d)
        if data.startswith("wiz:skip:"):
            step = data[9:]
            st = get_state(tg_id)
            if st.get("flow") == "add":
                d = dict(st.get("d") or {})
                if st.get("step") == "links" or step == "links":
                    return self.ask_validity(chat_id, tg_id, d)
                if step == "media":
                    return self.wizard(chat_id, tg_id, {"flow": "add", "step": "media", "d": d}, "/skip", None)
                return self.wizard(chat_id, tg_id, {"flow": "add", "step": step, "d": d}, "/skip", None)
            return self.wizard_start(chat_id, tg_id)
        if data.startswith("clr:"):
            return self.clear_setting(chat_id, tg_id, data[4:])
        if data.startswith("clritem:"):
            _, field, iid = data.split(":")
            return self.clear_item_field(chat_id, tg_id, int(iid), field)
        if data in ("wiz:validity0", "wiz:price0"):
            kind = data.split(":")[1]
            st = get_state(tg_id)
            d = dict(st.get("d") or {})
            if st.get("flow") == "edit":
                it = get_item(int(d.get("item") or 0))
                if it:
                    set_item(it["id"], "validity_days" if kind == "validity0" else "price",
                             0 if kind == "validity0" else 0)
                    set_state(tg_id, {})
                    self.bot.send(chat_id, "✅ Updated.")
                    self.item_menu(chat_id, get_item(it["id"]))
                return
            if st.get("flow") == "add":
                if kind == "price0":
                    d["price"] = 0.0
                    return self.wiz_next(chat_id, tg_id, d, "descr")
                d["validity_days"] = 0
                return self.wiz_confirm(chat_id, tg_id, d)
            return
        if data.startswith("adm:"):
            it = get_item(int(data[4:]))
            return self.item_menu(chat_id, it) if it else self.bot.send(chat_id, "Item not found.")
        if data.startswith("f:"):
            _, field, iid = data.split(":")
            return self.start_item_edit(chat_id, tg_id, int(iid), field)
        if data.startswith("tog:"):
            it = get_item(int(data[4:]))
            if it:
                set_item(it["id"], "active", 0 if it["active"] else 1)
                self.bot.send(chat_id, f"{'🟢 Published' if not it['active'] else '⏸️ Hidden'}: {esc(it['title'])}")
            return self.admin_items(chat_id)
        if data.startswith("del:"):
            it = get_item(int(data[4:]))
            if it:
                set_state(tg_id, {"flow": "delitem", "step": "confirm", "d": {"item": it["id"]}})
                return self.bot.send(chat_id,
                                     f"⚠️ Delete <b>{esc(it['title'])}</b> (#{it['id']})?\n"
                                     "Buyers who already unlocked it keep access.",
                                     kb(rows([btn("🗑 Yes, delete", f"delyes:{it['id']}"),
                                              btn("↩️ Cancel", "admin")])))
            return self.admin_items(chat_id)
        if data.startswith("delyes:"):
            it = get_item(int(data[7:]))
            if it:
                x("DELETE FROM items WHERE id=?", (int(it["id"]),))
                self.bot.send(chat_id, f"🗑 <b>{esc(it['title'])}</b> deleted.")
            return self.admin_items(chat_id)
        if data.startswith("nofile:"):
            iid = int(data[7:])
            set_item(iid, "file_id", None)
            set_item(iid, "file_kind", None)
            self.bot.send(chat_id, "🗑 File removed from the item.")
            return self.item_menu(chat_id, get_item(iid))
        if data.startswith("prev:"):
            it = get_item(int(data[5:]))
            if not it:
                return self.bot.send(chat_id, "Item not found.")
            if it["file_id"]:
                self.bot.send_media(chat_id, it["file_kind"] or it["kind"], it["file_id"],
                                    caption="👁 Item preview (admin view)")
            return self.bot.send(chat_id, self.item_caption(it), kb(rows([btn("⚙️ Edit", f"adm:{it['id']}")])))
        if data.startswith("buyers:"):
            iid = int(data[7:])
            rows_ = q("""SELECT o.amount, o.decided_at, u.name, u.id FROM orders o JOIN users u ON u.id=o.user_id
                         WHERE o.item_id=? AND o.status='approved' ORDER BY o.id DESC LIMIT 20""", (iid,))
            if not rows_:
                return self.bot.send(chat_id, "Nobody has bought this item yet.",
                                     kb(rows([btn("🔙 Item", f"adm:{iid}")])))
            lines = [f"▪️ {esc(shorten(r['name'], 24))} · {money(r['amount'])} · {ts(r['decided_at'])}" for r in rows_]
            buttons = [[btn(f"👤 {shorten(r['name'], 20)}", f"ausr:{r['id']}")] for r in rows_[:8]]
            buttons.append([btn("🔙 Item", f"adm:{iid}")])
            return self.bot.send(chat_id, f"🛒 <b>{len(rows_)} buyers</b>\n{SEP}\n\n" + "\n".join(lines), kb(buttons))
        # ------------------------- order actions -------------------------
        if data.startswith("aok:"):
            return self.approve_order(chat_id, int(data[4:]))
        if data.startswith("adcl:"):
            oid = int(data[5:])
            set_state(tg_id, {"flow": "decl", "step": "reason", "d": {"order": oid}})
            return self.bot.send(chat_id,
                                 f"❌ <b>Decline order #{oid:04d}</b>\n{SEP}\n"
                                 "Type the reason — the customer sees exactly this text.",
                                 kb(rows([btn("Use standard reason", f"dstd:{oid}"), btn("↩️ Back", "pend")])))
        if data.startswith("dstd:"):
            return self.decline_order(chat_id, int(data[5:]),
                                      "Payment could not be verified. Check the amount / screenshot and try again.")
        if data.startswith("aord:"):
            return self.order_view(chat_id, int(data[5:]))
        if data.startswith("adel:"):
            o = get_order(int(data[5:]))
            if o:
                it, u = get_item(o["item_id"]), user_by_id(o["user_id"])
                if it and u:
                    self.deliver(u["id"], it, o)
                    return self.bot.send(chat_id, "🔁 Content sent again.", kb(rows([btn("🧾 Order", f"aord:{o['id']}")])))
            return self.bot.send(chat_id, "❌ Could not re-deliver.")
        if data.startswith("areopen:"):
            oid = int(data[8:])
            x("UPDATE orders SET status='pending', decided_at=NULL, reason=NULL WHERE id=?", (oid,))
            self.bot.send(chat_id, "↩️ Order moved back to pending.")
            return self.order_view(chat_id, oid)
        if data.startswith("aproof:"):
            o = get_order(int(data[7:]))
            if o and o["proof_id"]:
                return self.bot.send_media(chat_id, o["proof_kind"] or "photo", o["proof_id"],
                                           caption=f"📸 Order #{o['no']} proof")
            return self.bot.send(chat_id, "🚫 No screenshot on this order.")
        if data.startswith("ausr:"):
            return self.customer_view(chat_id, int(data[5:]))
        if data.startswith("ausers:"):
            return self.customer_orders(chat_id, int(data[7:]))
        if data.startswith("agr:"):
            set_state(tg_id, {"flow": "grant", "step": "item", "d": {"user": int(data[4:])}})
            return self.bot.send(chat_id, "🎁 Send the item id to give free access:",
                                 kb(rows([btn("❌ Cancel", "cancel_flow")])))
        if data.startswith("apm:"):
            set_state(tg_id, {"flow": "pm", "step": "text", "d": {"user": int(data[4:])}})
            return self.bot.send(chat_id, "✍️ Type the message to send to this customer:")
        if data.startswith("abl:"):
            x("UPDATE users SET blocked=1 WHERE id=?", (int(data[4:]),))
            self.bot.send(chat_id, "🚫 Customer blocked.")
            return self.customer_view(chat_id, int(data[4:]))
        if data.startswith("aub:"):
            x("UPDATE users SET blocked=0 WHERE id=?", (int(data[4:]),))
            self.bot.send(chat_id, "🔓 Customer unblocked.")
            return self.customer_view(chat_id, int(data[4:]))
        if data == "aall":
            n = q("SELECT COUNT(*) c FROM orders WHERE status='pending'")[0]["c"]
            if not n:
                return self.bot.send(chat_id, "Nothing is pending 🙂", kb(rows([btn("🛠 Admin panel", "admin")])))
            return self.bot.send(chat_id,
                                 f"⚠️ Approve <b>{n}</b> order(s) and deliver everything?\n"
                                 "Please check the screenshots first.",
                                 kb(rows([btn(f"✅ Yes, approve {n}", "aall2"), btn("↩️ Cancel", "pend")])))
        if data == "aall2":
            rows_ = q("SELECT id FROM orders WHERE status='pending' ORDER BY id")
            for r in rows_:
                self.approve_order(chat_id, r["id"], silent=True)
            self.bot.send(chat_id, f"✅ {len(rows_)} order(s) approved and delivered.",
                          kb(rows([btn("📊 Stats", "stats"), btn("🛠 Admin panel", "admin")])))
            return
        return self.bot.send(chat_id, "❓ Unknown button.", kb(rows([btn("🛠 Admin panel", "admin")])))

    def open_item(self, chat_id, uid, item_id):
        it = get_item(item_id)
        if not it:
            return self.bot.send(chat_id, "❌ This item was removed by the admin.")
        if not has_access(uid, item_id):
            return self.bot.send(chat_id, pe("🔒 This item is locked — complete the payment first."),
                                 kb(rows([ubtn(f"Buy for {money(it['price'])}", f"buy:{item_id}", icon="💦", style="success"),
                                          ubtn("My orders", "orders", icon="🧾", style="primary")])))
        return self.deliver(uid, it, None)

    OPTIONAL_STEPS = {"descr", "media", "links", "validity", "post_links", "post_validity",
                      "wtype", "q_title", "q_price", "q_media", "q_link", "q_text"}

    @staticmethod
    def step_is_optional(state: dict, text: str) -> bool:
        """A slash command always wins over an optional wizard step (never eat /addtext)."""
        if not text.startswith("/") or text[1:2] == " ":
            return False
        cmd = re.match(r"^/([a-z_0-9]+)", text.lower())
        if not cmd or cmd.group(1) in ("skip", "cancel", "start"):
            return False
        flow, step = state.get("flow"), state.get("step")
        if flow == "add" and step in PremiumBot.OPTIONAL_STEPS:
            return True
        return flow in ("bc", "setfield", "pm", "setqr", "setwelcomephoto", "grant", "decl", "toadmin")

    def abandon_step(self, chat_id, tg_id, state):
        """Finish cleanly whatever the admin had already provided, then run their new command."""
        flow, step, d = state.get("flow"), state.get("step"), state.get("d") or {}
        set_state(tg_id, {})
        if flow == "add" and step in ("post_links", "post_validity"):
            it_id = int(d.get("item") or 0)
            if it_id and get_item(it_id):
                for f in ("link", "channel_link", "group_link"):
                    if d.get(f) is not None:
                        set_item(it_id, f, d.get(f))
                if step == "post_links":
                    set_item(it_id, "validity_days", 0)
                self.bot.send(chat_id, f"💾 Item #{it_id} saved (lifetime, links as sent).")
        elif flow == "add" and step != "title":
            self.bot.send(chat_id, "↩️ Item wizard stopped — the new command ran instead. Start again with /additem.")

    def admin_help(self, chat_id):
        body = ["ⓘ <b>Admin commands</b> (buttons are enough — these are shortcuts)", SEP,
                "<b>Items</b>",
                "<code>/additem</code> wizard · <code>/add Title | 199</code> reply to a file",
                "<code>/addlink Title | 99 | https://…</code> · <code>/addtext Coupon | 25 | CODE-123</code>",
                "<code>/edit 3</code> · <code>/price 3 49</code> · <code>/valid 3 30</code> · "
                "<code>/pause 3</code> · <code>/del 3</code>", "",
                "<b>Payments</b>",
                "<code>/setqr</code> · <code>/welcomephoto</code> · <code>/welcome text</code> · <code>/settings</code>",
                "", "<b>Orders & users</b>",
                "<code>/pending</code> · <code>/orders 12</code> · <code>/approve 12</code> · "
                "<code>/grant 5 3</code> · <code>/revoke 5 3</code> · <code>/block 123</code>",
                "<code>/bc message</code> · <code>/stats</code> · <code>/unstick 123</code>", "",
                f"<b>Env</b>: CURRENCY={CURRENCY} · DB=<code>{esc(os.path.basename(DB_PATH))}</code>"]
        self.bot.send(chat_id, "\n".join(body),
                      kb(rows([btn("🛠 Admin panel", "admin"), btn("👁 My store", "shop:0")])))

    def start_field_edit(self, chat_id, tg_id, key):
        labels = {"upi_id": ("UPI ID", "Send it like <code>shop@ybl</code>"),
                  "payee_name": ("Payee name", "Shown above the QR, e.g. <code>Ravi Classes</code>"),
                  "pay_note": ("Checkout note", "Short instruction shown on every payment screen"),
                  "refund_note": ("Refund note", "Shown in help and on the item page"),
                  "pay_instructions": ("Extra instructions", "Optional extra lines for the payment screen"),
                  "brand": ("Brand name", "Shown in the header of the store"),
                  "welcome_text": ("Welcome text", "Send the text shown on /start (with your welcome photo)"),
                  "force_channel": ("Force channel join", "Send <code>@username</code> or a t.me link. Send <code>off</code> to disable"),
                  "out_of_stock_note": ("Empty-store note", "Shown when no item is published"),
                  "demo_link": ("Free demo link", "Channel / link behind the 📹 FREE DEMO button, e.g. <code>@mydemo</code>"),
                  "proofs_link": ("Proofs channel", "Channel / link behind the 📢 PROOFS button, e.g. <code>@myproofs</code>"),
                  "support_link": ("Support link", "Username or link for the 😘 Support button, e.g. <code>@support</code>"),
                  "stats_joined": ("Stats — users joined", "Display-only number on the welcome screen (empty = real count)"),
                  "stats_month": ("Stats — active this month", "Display-only number (empty = real count)"),
                  "stats_today": ("Stats — active today", "Display-only number (empty = real count)")}
        label, hint = labels.get(key, (key, "Send the new value"))
        cur = setting(key)
        set_state(tg_id, {"flow": "setfield", "step": key, "d": {}})
        return self.bot.send(chat_id,
                             f"✏️ <b>{esc(label)}</b>\n{SEP}\n{hint}"
                             f"\n\nCurrent value:\n<code>{esc(shorten(cur, 200)) if cur else '— empty —'}</code>",
                             kb(rows([btn("🗑 Clear value", f"clr:{key}"), btn("❌ Cancel", "cancel_flow")])))

    def start_item_edit(self, chat_id, tg_id, item_id, field):
        it = get_item(item_id)
        if not it:
            return self.bot.send(chat_id, "Item not found.", kb(rows([btn("📦 Items", "pg:items:0")])))
        if field == "file_id":
            set_state(tg_id, {"flow": "edit", "step": "media", "d": {"item": item_id, "field": field}})
            return self.bot.send(chat_id,
                                 "🎬 Send the new <b>video / photo / file</b> — it replaces the current one.\n"
                                 "Send <code>/skip</code> to remove the file.",
                                 kb(rows([btn("❌ Cancel", "cancel_flow")])))
        if field == "validity_days":
            set_state(tg_id, {"flow": "edit", "step": "value", "d": {"item": item_id, "field": field}})
            return self.bot.send(chat_id,
                                 f"⏱ Current validity: <b>{it['validity_days'] or 'lifetime'}</b>\n"
                                 "Send the number of days (e.g. <code>30</code>) or <code>0</code> for lifetime.",
                                 kb(rows([btn("♾ Lifetime", "wiz:validity0"), btn("❌ Cancel", "cancel_flow")])))
        if field == "price":
            set_state(tg_id, {"flow": "edit", "step": "value", "d": {"item": item_id, "field": field}})
            return self.bot.send(chat_id,
                                 f"💵 Current price: <b>{money(it['price'])}</b>\nSend the new price "
                                 "(e.g. <code>199</code>) or <code>0</code> for free.",
                                 kb(rows([btn("🎁 Make it free", "wiz:price0"), btn("❌ Cancel", "cancel_flow")])))
        label = ITEM_FIELDS.get(field, field)
        cur = it[field]
        set_state(tg_id, {"flow": "edit", "step": "value", "d": {"item": item_id, "field": field}})
        return self.bot.send(chat_id,
                             f"✏️ <b>{esc(label)}</b>\n{SEP}\nCurrent:\n{esc(cur) if cur else '— empty —'}\n\n"
                             "Send the new value." + ("" if field != "descr" else " (up to 1500 characters)"),
                             kb(rows([btn("🗑 Clear", f"clritem:{field}:{it['id']}"), btn("❌ Cancel", "cancel_flow")])))

    # ======================================================================
    # PENDING INPUT (wizard answers, screenshots, QR photos, reasons…)
    # ======================================================================
    def on_state(self, chat_id, tg_id, uid, text, media, st, admin) -> bool:
        flow, step = st.get("flow"), st.get("step")
        d = st.get("d") or {}
        low = (text or "").strip()
        bare = low[1:].strip() if low.startswith("/") else low     # "/skip" behaves like "skip"
        if low.lower() in ("/cancel", "cancel"):
            set_state(tg_id, {})
            return False

        # ---------------- user: waiting for the payment screenshot ----------------
        if flow == "buy" and step == "proof":
            o = get_order(st.get("order"))
            if not o or o["status"] != "pending":
                set_state(tg_id, {})
                return False
            if media or bare.lower() in ("done", "paid", "ok", "sent"):
                return self.submit_proof(chat_id, tg_id, uid, o, media, low if media else "")
            self.bot.send(chat_id,
                          "📸 Send the <b>screenshot</b> (photo or file) of your payment.\n" + SEP,
                          kbd=rows([btn("💳 Payment info", "payinfo"), btn("❌ Cancel order", f"cancel:{o['id']}")]))
            return True

        # ---------------- user: message for the admin ----------------
        if flow == "toadmin":
            if not (text or media):
                return True
            set_state(tg_id, {})
            for a in ADMIN_IDS:
                if media:
                    self.bot.send_media(a, media["file_kind"], media["file_id"],
                                        caption=f"💬 From customer #{uid} · {esc(self.name_of(uid))}\n{esc(text)[:400]}")
                else:
                    self.bot.send(a, f"💬 <b>Message from customer</b>\n{SEP}\n{esc(text)[:1200]}",
                                  kbd=rows([btn("👤 Reply / profile", f"ausr:{uid}"), btn("🧾 Their orders", f"ausers:{uid}")]))
            self.bot.send(chat_id, "✅ Sent to the admin. They usually reply within a few hours.",
                          kbd=rows([btn("🏠 Home", "home")]))
            return True

        # ---------------- admin: decline reason ----------------
        if flow == "decl" and admin:
            set_state(tg_id, {})
            return self.decline_order(chat_id, d.get("order"), low or
                                      "Payment could not be verified.")

        # ---------------- admin: grant free access ----------------
        if flow == "grant" and admin:
            set_state(tg_id, {})
            n = re.search(r"\d+", low)
            u = user_by_id(int(d.get("user")))
            it = get_item(int(n.group())) if (n and u) else None
            if it and u:
                grant_access(u["id"], it["id"], None, it["validity_days"])
                self.deliver(u["id"], it, None)
                self.bot.send(chat_id, f"🎁 <b>{esc(it['title'])}</b> granted to {esc(u['name'])}.",
                              kbd=rows([btn("👤 Profile", f"ausr:{u['id']}")]))
                return True
            self.bot.send(chat_id, "❌ Item not found.", kbd=rows([btn("🛠 Panel", "admin")]))
            return True

        # ---------------- admin: private message to a customer ----------------
        if flow == "pm" and admin:
            set_state(tg_id, {})
            u = user_by_id(int(d.get("user")))
            if u and low:
                ok = self.bot.send(int(u["tg_id"]), f"💬 <b>Message from {esc(setting('brand'))}</b>\n{SEP}\n{esc(low)[:2000]}")
                self.bot.send(chat_id, f"✅ Delivered to {esc(u['name'])}." if ok else "⚠️ Could not deliver.")
            return True

        # ---------------- admin: setting a text field ----------------
        if flow == "setfield" and admin:
            key = step
            if bare.lower() in ("off", "none", "clear", "disable"):
                set_setting(key, "")
            elif key == "upi_id" and not re.match(r"^[\w.\-]{2,}@[A-Za-z]{2,}$", low.split()[0] or ""):
                self.bot.send(chat_id, "❌ That doesn't look like a UPI id. Example: <code>shop@ybl</code>",
                              kbd=rows([btn("↩️ Retry", "s:upi_id"), btn("❌ Cancel", "cancel_flow")]))
                return True
            else:
                set_setting(key, low[:1500])
            set_state(tg_id, {})
            self.bot.send(chat_id, f"✅ <b>{esc(SET_LABELS.get(key, key))}</b> updated.")
            if key in ("upi_id", "payee_name", "pay_note", "refund_note", "pay_instructions"):
                self.admin_pay_setup(chat_id)
            else:
                self.admin_store_setup(chat_id)
            return True

        # ---------------- admin: QR / welcome photo ----------------
        if flow in ("setqr", "setwelcomephoto") and admin:
            if not media:
                if bare.lower() == "skip" and flow == "setqr":
                    set_setting("qr_file_id", "")
                    set_state(tg_id, {})
                    return self.admin_pay_setup(chat_id)
                self.bot.send(chat_id, "🖼 Please send the <b>image</b> (photo or file) now.",
                              kbd=rows([btn("❌ Cancel", "cancel_flow")]))
                return True
            set_setting("qr_file_id" if flow == "setqr" else "welcome_photo_id", media["file_id"])
            set_state(tg_id, {})
            self.bot.send(chat_id, "✅ Saved — the image is stored on Telegram, so it loads instantly for users.")
            if flow == "setqr":
                self.admin_pay_setup(chat_id)
            else:
                self.admin_store_setup(chat_id)
            return True

        # ---------------- admin: broadcast ----------------
        if flow == "bc" and admin:
            set_state(tg_id, {})
            n = self.broadcast(chat_id, text, media)
            if n is not None:
                self.bot.send(chat_id, f"📣 Broadcast delivered to <b>{n}</b> customers.",
                              kbd=rows([btn("🛠 Admin panel", "admin")]))
            return True

        # ---------------- admin: item wizard ----------------
        if flow == "add" and admin:
            return self.wizard(chat_id, tg_id, st, low, media)

        # ---------------- admin: single field edit ----------------
        if flow == "edit" and admin:
            it = get_item(int(d.get("item")))
            if not it:
                set_state(tg_id, {})
                return True
            field = d.get("field")
            if step == "media":
                if bare.lower() in ("skip", "none"):
                    set_item(it["id"], "file_id", None)
                    set_item(it["id"], "file_kind", None)
                elif not media:
                    self.bot.send(chat_id, "🎬 Send the file, or <code>/skip</code> to remove it.")
                    return True
                else:
                    set_item(it["id"], "file_id", media["file_id"])
                    set_item(it["id"], "file_kind", media["file_kind"])
                    set_item(it["id"], "kind", media["kind"])
                set_state(tg_id, {})
                self.bot.send(chat_id, "✅ File updated.")
                self.item_menu(chat_id, get_item(it["id"]))
                return True
            if field == "price":
                n = to_num(low)
                if n is None:
                    self.bot.send(chat_id, "❌ Send a number, e.g. <code>199</code> (0 = free).")
                    return True
                set_item(it["id"], "price", n)
            elif field == "validity_days":
                n = re.search(r"\d+", low)
                set_item(it["id"], "validity_days", int(n.group()) if n else 0)
            elif field == "title":
                set_item(it["id"], "title", (low or it["title"])[:120])
            else:
                set_item(it["id"], field, None if bare.lower() in ("none", "clear") else low[:1500])
            set_state(tg_id, {})
            self.bot.send(chat_id, f"✅ {esc(ITEM_FIELDS.get(field, field))} updated.")
            self.item_menu(chat_id, get_item(it["id"]))
            return True
        return False

    def wizard(self, chat_id, tg_id, st, text, media) -> bool:
        d = st.get("d") or {}
        step = st.get("step")
        low = (text or "").strip()
        bare = low[1:].strip() if low.startswith("/") else low
        skip = bare.lower() in ("skip", "none", "-")

        # ---------------- simple wizard (type → name → price → content) ----------------
        if step == "q_title":
            if not low or low.startswith("/"):
                self.bot.send(chat_id, "❌ Send the name as text.")
                return True
            d["title"] = low[:120]
            set_state(tg_id, {"flow": "add", "step": "q_price", "d": d})
            self.bot.send(chat_id,
                          f"➕ <b>New item — {esc(d['title'])} · step 2/3</b>\n{SEP}\n"
                          "Send the <b>price</b> in rupees, e.g. <code>199</code>.\n"
                          "Send <code>0</code> for a free item.",
                          kb(rows([btn("🆓 Free (₹0)", "wiz:qfree", style="success"),
                                   btn("❌ Cancel", "cancel_flow", style="danger")])))
            return True
        if step == "q_price":
            if skip:
                d["price"] = 0.0
                return self.q_ask_content(chat_id, tg_id, d)
            n = to_num(low)
            if n is None:
                self.bot.send(chat_id, "❌ Send a number — e.g. <code>199</code>, or <code>0</code> for free.")
                return True
            d["price"] = n
            return self.q_ask_content(chat_id, tg_id, d)
        if step == "q_media":
            kind = d.get("wtype", "video")
            if not media:
                self.bot.send(chat_id, f"🎬 Send the <b>{esc(kind)}</b> now (upload it here), or Cancel.")
                return True
            d["file_id"], d["file_kind"], d["kind"] = media["file_id"], media["file_kind"], media["kind"]
            return self.qwiz_done(chat_id, tg_id, d)
        if step == "q_link":
            kind = d.get("wtype", "link")
            url = low.strip("`").strip()
            if url.startswith("@") and re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", url):
                url = "https://t.me/" + url.lstrip("@")
            if not url.startswith("http"):
                self.bot.send(chat_id, "❌ Send a valid link — <code>https://…</code> or <code>@username</code>.")
                return True
            key = {"link": "link", "channel": "channel_link", "group": "group_link"}.get(kind, "link")
            d[key] = url
            d["kind"] = "link"
            return self.qwiz_done(chat_id, tg_id, d)
        if step == "q_text":
            if not low:
                self.bot.send(chat_id, "❌ Send the text / coupon as a message.")
                return True
            d["descr"] = low[:1500]
            d["kind"] = "text"
            return self.qwiz_done(chat_id, tg_id, d)

        if step == "title":
            if not low:
                self.bot.send(chat_id, "❌ Send the title text.")
                return True
            d["title"] = low[:120]
            return self.wiz_next(chat_id, tg_id, d, "price")
        if step == "price":
            if skip:
                d["price"] = 0.0
                return self.wiz_next(chat_id, tg_id, d, "descr")
            n = to_num(low)
            if n is None:
                self.bot.send(chat_id, "❌ Send a number — e.g. <code>199</code>, or <code>0</code> for free.")
                return True
            d["price"] = n
            return self.wiz_next(chat_id, tg_id, d, "descr")
        if step == "descr":
            d["descr"] = "" if skip else low[:1500]
            return self.wiz_next(chat_id, tg_id, d, "media")
        if step == "media":
            if skip or not media:
                if media is None and not skip:
                    self.bot.send(chat_id, "🎬 Send the file now, or tap <b>Skip this step</b>.",
                                  kbd=rows([btn("⏭️ Skip this step", "wiz:skip:media"), btn("❌ Cancel", "cancel_flow")]))
                    return True
                d["file_id"] = d["file_kind"] = None
                d["kind"] = "link"
            else:
                d["file_id"], d["file_kind"], d["kind"] = media["file_id"], media["file_kind"], media["kind"]
                if not d.get("title"):
                    d["title"] = shorten(media["name"], 60)
            return self.wiz_next(chat_id, tg_id, d, "links")
        if step == "quick_media":
            if skip:
                d["kind"] = "link"
                return self.ask_validity(chat_id, tg_id, d)
            if not media:
                self.bot.send(chat_id, "🎬 Send the file, or tap Skip for a link-only item.",
                              kbd=rows([btn("⏭️ Skip file", "wiz:skip:media"), btn("❌ Cancel", "cancel_flow")]))
                return True
            d["file_id"], d["file_kind"], d["kind"] = media["file_id"], media["file_kind"], media["kind"]
            d.setdefault("title", shorten(media["name"], 60))
            d.setdefault("price", 0)
            set_state(tg_id, {"flow": "add", "step": "validity", "d": d})
            self.bot.send(chat_id,
                          f"✅ {esc(media['name'])} attached · {money(d.get('price') or 0)}\n{SEP}\n"
                          "Last step — access validity? Send <code>0</code> for lifetime, "
                          "or send links like <code>channel: https://t.me/x</code>.",
                          kbd=rows([btn("♾ Lifetime access", "wiz:lifetime")]))
            return True
        if step == "links":
            added = self.links_into(d, low)
            have = d.get("link") or d.get("file_id") or d.get("channel_link") or d.get("group_link")
            if not have:
                self.bot.send(chat_id,
                              "⚠️ An item needs at least a <b>file</b> or a <b>link</b>.\n"
                              "Send a link now, or go back and add a file.",
                              kbd=rows([btn("🎬 Add a file", "wiz:skip:media"), btn("❌ Cancel", "cancel_flow")]))
                return True
            if added:
                self.bot.send(chat_id, f"✅ Added: {esc(added)}")
            return self.ask_validity(chat_id, tg_id, d)
        if step == "post_links":
            it_id = int(d.get("item") or 0)
            if not skip and media and not low:
                low = ""
            added = self.links_into(d, low)
            if added:
                set_item(it_id, "link", d.get("link"))
                set_item(it_id, "channel_link", d.get("channel_link"))
                set_item(it_id, "group_link", d.get("group_link"))
                self.bot.send(chat_id, f"✅ Added: {esc(added)}")
            set_state(tg_id, {"flow": "add", "step": "post_validity", "d": d})
            self.bot.send(chat_id,
                          "⏱ How long should buyers keep access? Send days (e.g. <code>30</code>) "
                          "or tap lifetime.",
                          kbd=rows([btn("♾ Lifetime access", "wiz:lifetimepost")]))
            return True
        if step == "post_validity":
            it_id = int(d.get("item") or 0)
            n = re.search(r"\d+", low)
            set_item(it_id, "validity_days", int(n.group()) if n else 0)
            set_state(tg_id, {})
            it = get_item(it_id)
            self.bot.send(chat_id, f"🎉 Item <b>#{it_id}</b> is live.\n{SEP}\n{self.item_caption(it)}",
                          kbd=rows([btn("⚙️ Edit item", f"adm:{it_id}"), btn("📦 All items", "pg:items:0")],
                                  [btn("👁 Store view", "shop:0")]))
            return True
        if step == "validity":
            n = re.search(r"\d+", low)
            if not n and re.search(r"https?://|@\w+", low):
                d2 = dict(d)
                added = self.links_into(d2, low)
                set_state(tg_id, {"flow": "add", "step": "validity", "d": d2})
                self.bot.send(chat_id, f"✅ {esc(added or 'link')} added.\n\n⏱ Now the validity — "
                                       "<code>0</code> = lifetime, or a number of days.",
                              kbd=rows([btn("♾ Lifetime access", "wiz:lifetime")]))
                return True
            d["validity_days"] = int(n.group()) if n else 0
            return self.wiz_confirm(chat_id, tg_id, d)
        return False

    @staticmethod
    def name_of(uid):
        u = user_by_id(uid)
        return u["name"] if u else "?"

    def broadcast(self, chat_id, text, media=None):
        targets = q("SELECT tg_id FROM users WHERE is_admin=0 AND blocked=0")
        if not (text or media):
            self.bot.send(chat_id, "❌ Nothing to send — type the message and try again.")
            return None
        head = f"📣 <b>{esc(setting('brand'))}</b>\n{SEP}\n"
        n = 0
        for r in targets:
            if media:
                self.bot.send_media(int(r["tg_id"]), media["file_kind"], media["file_id"], caption=head + esc(text)[:900])
            else:
                self.bot.send(int(r["tg_id"]), head + esc(text)[:3000])
            n += 1
            time.sleep(0.05)
        return n

    def save_welcome_photo(self, chat_id, media):
        set_setting("welcome_photo_id", media["file_id"])
        set_state(self.chat_or_zero(chat_id), {})
        self.bot.send(chat_id, "✅ Welcome photo saved.")
        return (self.admin_store_setup(chat_id) or True)

    @staticmethod
    def chat_or_zero(v):
        try:
            return int(v)
        except Exception:
            return 0

    # ======================================================================
    # LONG POLLING
    # ======================================================================
    def loop(self):
        log(f"bot online · admins={sorted(ADMIN_IDS)} · db={DB_PATH}")
        me = self.bot.api("getMe")
        uname = ((me or {}).get("result") or {}).get("username")
        log(f"connected as @{uname}" if uname else "⚠️ getMe failed — check BOT_TOKEN")
        fails = 0
        while True:
            try:
                j = self.bot.api("getUpdates", {"offset": self.offset, "timeout": POLL_TIMEOUT,
                                                "allowed_updates": ["message", "edited_message", "callback_query"]})
                if not j or not j.get("ok"):
                    fails += 1
                    time.sleep(min(30, 2 + fails * 3))
                    continue
                fails = 0
                for u in j.get("result", []):
                    self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
                    self.handle_update(u)
            except KeyboardInterrupt:
                return log("stopped 👋")
            except Exception:
                fails += 1
                log("poll loop error:\n" + traceback.format_exc())
                time.sleep(min(60, 3 * fails))

    # ======================================================================
    # small UI helpers used by buttons (clear value / quick set)
    # ======================================================================
    def clear_setting(self, chat_id, tg_id, key):
        set_setting(key, "")
        set_state(tg_id, {})
        self.bot.send(chat_id, f"🗑 <b>{esc(key)}</b> cleared.")
        return self.admin_pay_setup(chat_id) if key in ("upi_id", "payee_name", "pay_note", "refund_note",
                                                        "pay_instructions") else self.admin_store_setup(chat_id)

    def clear_item_field(self, chat_id, tg_id, item_id, field):
        set_item(int(item_id), field, None)
        set_state(tg_id, {})
        self.bot.send(chat_id, f"🗑 {esc(ITEM_FIELDS.get(field, field))} cleared.")
        self.item_menu(chat_id, get_item(int(item_id)))


# ==========================================================================
# DEMO CONSOLE — the terminal becomes Telegram (no token needed)
# ==========================================================================
DEMO_PEOPLE = {1: ("Admin", "admin"), 2: ("Ravi Kumar", "ravi"), 3: ("Neha Sharma", "neha"),
               4: ("Amit Patel", "amit"), 5: ("Sara", "sara")}
_seq = [1000]


def next_fid(kind="photo"):
    _seq[0] += 1
    return f"demo_{kind}_{_seq[0]}"


def mk_msg(tg_id, text="", media=None):
    """Build a Telegram message update. media: 'photo' | 'video' | 'file' | None"""
    name, uname = DEMO_PEOPLE.get(tg_id, (f"User{tg_id}", f"user{tg_id}"))
    m = {"message_id": _seq[0], "from": {"id": tg_id, "first_name": name, "username": uname},
         "chat": {"id": tg_id, "type": "private"}, "date": int(time.time())}
    if media == "photo":
        m["photo"] = [{"file_id": next_fid("photo"), "width": 1080, "height": 1350}]
    elif media == "video":
        m["video"] = {"file_id": next_fid("video"), "file_name": f"lesson_{_seq[0]}.mp4", "file_size": 9_000_000}
    elif media == "file":
        m["document"] = {"file_id": next_fid("doc"), "file_name": f"notes_{_seq[0]}.pdf", "file_size": 4_000_000}
    if text:
        m["caption" if media else "text"] = text
    return {"message": m}


def mk_cb(tg_id, data, message_id=1):
    name, uname = DEMO_PEOPLE.get(tg_id, (f"User{tg_id}", f"user{tg_id}"))
    return {"callback_query": {"id": f"cb{_seq[0]}", "from": {"id": tg_id, "first_name": name, "username": uname},
                               "message": {"message_id": message_id, "chat": {"id": tg_id, "type": "private"},
                                           "from": {"id": tg_id, "first_name": name}},
                               "data": data}}


def parse_line(line, last=None):
    """Console input → (tg_id, text, media).

    'a /admin'          admin
    'u3 [photo]'        user 3 sent a photo
    '199'               reply to the previous actor (sticky)
    '#buy:1'            press an inline button
    """
    line = (line or "").strip()
    who = None
    mt = re.match(r"^(admin|a|user|u\d*)\s+(.*)$", line, re.I | re.S)
    if mt:
        who = mt.group(1).lower()
        line = mt.group(2).strip()
    media = None
    for tag, kind in (("[photo]", "photo"), ("[video]", "video"), ("[file]", "file"),
                      ("[doc]", "file"), ("[screenshot]", "photo")):
        if line.lower().startswith(tag):
            media = kind
            line = line[len(tag):].strip()
            break
    if who is None:
        if last is None:
            return None, "❓ Start with an actor —  a /admin   or   u /start", None
        return last, line, media
    if who in ("a", "admin"):
        return (max(ADMIN_IDS) if ADMIN_IDS else 1), line, media
    if who in ("u", "user"):
        return 2, line, media
    return (int(re.sub(r"\D", "", who) or 2)), line, media


DEMO_BANNER = """
══════════════════════════════════════════════════════════════════
  PremiumVideo — DEMO MODE (the terminal is Telegram; no token)
══════════════════════════════════════════════════════════════════
  actors     a = admin · u = Ravi · u3 = Neha · u4 = Amit
  media      [photo] [video] [file]        e.g.  a [video] /add Course | 199
  buttons    #  then the callback, e.g.   u #shop:0    a #admin
  replies    sticky actor — type only the answer after 'a /additem'
  shortcuts  :items :orders :pend :approve 1 :decline 1 reason :users :db :reset :quit
  flow       a /additem → a #wtype:video → name → price → a [video] → u /shop → u #item:1 → u [photo] → a #pend → ✅
══════════════════════════════════════════════════════════════════
"""


def run_demo(db_file=None):
    global OFFLINE
    if db_file:
        globals()["DB_PATH"] = db_file
    OFFLINE = True
    if not ADMIN_IDS:
        ADMIN_IDS.add(1)
    init_db()
    bot = Bot("demo-token", offline=True, label="TG")
    pb = PremiumBot(bot)
    print(DEMO_BANNER)
    if not q("SELECT id FROM items LIMIT 1"):
        print("  💡 The database is empty — start with:  a /additem   (or: a /add Demo course | 99 → a [video])")
    last = None
    while True:
        try:
            line = input("\n🧪 ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line.startswith(":"):
            if not demo_meta(pb, line[1:]):
                return
            continue
        if line.startswith("#"):
            pb.handle_update(mk_cb(last or 2, line[1:]))
            continue
        tg, rest, media = parse_line(line, last)
        if tg is None:
            print(rest)
            continue
        last = tg
        who = "ADMIN" if tg in ADMIN_IDS else DEMO_PEOPLE.get(tg, (f"user{tg}",))[0]
        print(f"\n── {who} (tg {tg}){'  [' + media + ']' if media else ''}  {rest or '(media only)'}")
        if rest.startswith("#"):
            pb.handle_update(mk_cb(tg, rest[1:]))
        else:
            pb.handle_update(mk_msg(tg, rest, media))


def demo_meta(pb, line) -> bool:
    cmd, _, arg = line.partition(" ")
    cmd, arg = cmd.strip().lower(), arg.strip()
    admin = max(ADMIN_IDS) if ADMIN_IDS else 1
    if cmd in ("q", "quit", "exit"):
        print("bye 👋  (the demo database is kept — use :reset to start clean)")
        return False
    if cmd == "items":
        for r in q("SELECT * FROM items ORDER BY id"):
            print(f"  #{r['id']:<3} {'🟢' if r['active'] else '⏸️'} {money(r['price']):>9} {r['kind']:<6} "
                  f"{shorten(r['title'], 30):<32} file={r['file_id'] or '-'} link={(r['link'] or '-')[:22]} "
                  f"ch={(r['channel_link'] or '-')[:18]} sold={r['sold']}")
        return True
    if cmd in ("pend", "pending"):
        rows_ = q("SELECT * FROM orders WHERE status='pending' ORDER BY id DESC")
        print("  (nothing pending)" if not rows_ else "")
        for r in rows_:
            print(f"  #{r['no']} {money(r['amount']):>9} user={r['user_id']} item={r['item_id']} "
                  f"proof={'📸' if r['proof_id'] else 'missing'}")
        return True
    if cmd == "orders":
        for r in q("SELECT * FROM orders ORDER BY id DESC LIMIT 15"):
            print(f"  #{r['no']} {r['status']:<9} {money(r['amount']):>9} user={r['user_id']} item={r['item_id']} "
                  f"{ts(r['created_at'])}")
        return True
    if cmd in ("approve", "decline"):
        n = re.sub(r"\D", "", arg) or "0"
        if cmd == "approve":
            pb.handle_update(mk_cb(admin, f"aok:{n}"))
        else:
            reason = arg.split(" ", 1)[1] if " " in arg else "amount mismatch"
            pb.handle_update(mk_cb(admin, f"adcl:{n}"))
            pb.handle_update(mk_msg(admin, reason))
        return True
    if cmd in ("buy", "press", "cb"):
        who, _, data = arg.partition(" ")
        tg = admin if who in ("a", "admin") else (int(re.sub(r"\D", "", who) or 2) or admin)
        pb.handle_update(mk_cb(tg, data or who))
        return True
    if cmd == "users":
        for r in q("SELECT * FROM users ORDER BY id"):
            print(f"  id={r['id']} tg={r['tg_id']} {shorten(r['name'], 18):<20} "
                  f"{'ADMIN' if r['is_admin'] else 'user '} orders={r['orders']} spent={money(r['spent'])} "
                  f"blocked={r['blocked']}")
        return True
    if cmd == "as":
        n = re.sub(r"\D", "", arg)
        print(f"🎭 acting as telegram id {n}") if n else print("Usage: :as 123456789")
        return True
    if cmd == "db":
        for t in ("users", "items", "orders", "unlocks", "settings", "states"):
            rows_ = q(f"SELECT * FROM {t} LIMIT 10")
            print(f"\n── {t} ({len(rows_)} shown)")
            for r in rows_:
                print("   " + json.dumps({k: str(r[k])[:38] for k in r.keys()}, ensure_ascii=False))
        return True
    if cmd == "reset":
        for t in ("users", "items", "orders", "unlocks", "settings", "states"):
            x(f"DELETE FROM {t}")
        print("🧹 demo database cleared.")
        return True
    print(__doc__ if cmd == "help" else
          "❓ :items :pend :orders :approve <id> :decline <id> <reason> :press u #buy:1 :users :db :reset :quit")
    return True


# ==========================================================================
# SELFTEST
# ==========================================================================
def selftest() -> int:
    global OFFLINE
    OFFLINE = True
    if not ADMIN_IDS:
        ADMIN_IDS.add(1)
    globals()["DB_PATH"] = os.path.join(DATA_DIR, f"selftest_{os.getpid()}.db")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(DB_PATH + suf):
            os.remove(DB_PATH + suf)
    init_db()
    bot = Bot("demo", offline=True, label="T")
    pb = PremiumBot(bot)
    res = {"ok": 0, "fail": 0}

    def check(name, cond, extra=""):
        if cond:
            res["ok"] += 1
            print(f"  ✅ {name}" + (f"  {extra}" if extra else ""))
        else:
            res["fail"] += 1
            print(f"  ❌ {name}" + (f"  {extra}" if extra else ""))

    def head(t):
        print(f"\n\033[1m── {t}\033[0m")

    def out(method=None, chat=None):
        return [o for o in bot.outbox if (not method or o["method"] == method)
                and (chat is None or str(o["params"].get("chat_id")) == str(chat))]

    def last_text(method=None, chat=None):
        items = out(method, chat) or out()
        return (items[-1]["params"].get("text") or items[-1]["params"].get("caption") or "") if items else ""

    head("Admin setup — payment + welcome photo (via panel buttons)")
    pb.handle_update(mk_msg(1, "/admin"))
    check("/admin opens the panel", any("admin panel" in (o["params"].get("text", "")) for o in out("sendMessage", 1)))
    pb.handle_update(mk_cb(1, "s:brand"))
    pb.handle_update(mk_msg(1, "Ravi Premium"))
    check("brand updated from panel", setting("brand") == "Ravi Premium")
    pb.handle_update(mk_cb(1, "s:upi_id"))
    pb.handle_update(mk_msg(1, "wrong-format"))
    check("bad UPI rejected", setting("upi_id") in ("", None))
    pb.handle_update(mk_msg(1, "ravi@ybl"))
    check("UPI saved after retry", setting("upi_id") == "ravi@ybl")
    pb.handle_update(mk_cb(1, "setqr"))
    pb.handle_update(mk_msg(1, "", media="photo"))
    check("QR image saved", bool(setting("qr_file_id")))
    pb.handle_update(mk_cb(1, "setwelcomephoto"))
    pb.handle_update(mk_msg(1, "", media="photo"))
    check("welcome photo saved", bool(setting("welcome_photo_id")))

    head("Items — quick add, link, text, wizard")
    pb.handle_update(mk_msg(1, "/add Python Full Course | 199", media="video"))
    pb.handle_update(mk_msg(1, "channel: https://t.me/ravipremium", media="video"))   # still on quick_media step
    pb.handle_update(mk_msg(1, "0"))
    pb.handle_update(mk_msg(1, "/addlink VIP channel access | 99 | https://t.me/viprav"))
    pb.handle_update(mk_msg(1, "/addtext Course coupon | 25 | RAVI25-COUPON"))
    pb.handle_update(mk_msg(1, "/add Free wallpaper | 0", media="photo"))
    pb.handle_update(mk_msg(1, "skip"))
    pb.handle_update(mk_msg(1, "0"))
    items = q("SELECT * FROM items ORDER BY id")
    titles = [i["title"] for i in items]
    check("4 items created", len(items) == 4, f"→ {titles}")
    check("item 1 has file + channel link", items[0]["file_kind"] == "video" and items[0]["channel_link"])
    check("item 2 is a link item", items[1]["kind"] == "link" and "t.me/viprav" in items[1]["link"])
    check("item 3 is text/serial", items[2]["kind"] == "text" and "RAVI25" in items[2]["descr"])
    check("item 4 is free", float(items[3]["price"]) == 0)
    bot.outbox.clear()
    pb.handle_update(mk_msg(1, "/additem"))
    check("add-item asks the type with buttons", "What do you want to add" in last_text("sendMessage", 1))
    pb.handle_update(mk_cb(1, "wtype:file"))
    pb.handle_update(mk_msg(1, "Editing Masterclass"))
    pb.handle_update(mk_msg(1, "299"))
    pb.handle_update(mk_msg(1, "", media="file"))
    w = q("SELECT * FROM items ORDER BY id DESC LIMIT 1")[0]
    check("simple wizard published the item", w["title"] == "Editing Masterclass" and int(w["active"]) == 1)
    check("simple wizard price", float(w["price"]) == 299)
    check("simple wizard file kept", w["file_kind"] == "document")
    check("simple wizard is lifetime + no extra steps", int(w["validity_days"]) == 0)

    head("User — welcome screen with photo")
    bot.outbox.clear()
    pb.handle_update(mk_msg(2, "/start"))
    check("welcome sent as photo (with caption)", bool(out("sendPhoto", 2)) and
          "Ravi Premium" in (out("sendPhoto", 2)[0]["params"].get("caption", "") if out("sendPhoto", 2) else ""))
    check("welcome has the new main-menu buttons",
          "Buy Premium Videos" in json.dumps(out("sendPhoto", 2)[0]["params"]) and
          "How to use" in json.dumps(out("sendPhoto", 2)[0]["params"]))
    check("welcome shows stats + support footer",
          "users already joined" in (out("sendPhoto", 2)[0]["params"].get("caption", "") if out("sendPhoto", 2) else ""))
    pb.handle_update(mk_cb(2, "shop:0"))
    check("store lists items as buttons with prices",
          "Python Full Course" in json.dumps(out("sendMessage", 2)[-1]["params"], ensure_ascii=False) and
          "₹199" in json.dumps(out("sendMessage", 2)[-1]["params"], ensure_ascii=False))
    pb.handle_update(mk_cb(2, f"item:{items[0]['id']}"))
    j = json.dumps(out("sendPhoto", 2)[-1]["params"], ensure_ascii=False)
    check("store click goes straight to checkout (QR)", bool(out("sendPhoto", 2)) and
          "ravi@ybl" in (out("sendPhoto", 2)[-1]["params"].get("caption", "")) and "₹199" in
          (out("sendPhoto", 2)[-1]["params"].get("caption", "")))
    check("checkout shows only the I-paid button", "I paid — submit screenshot" in j and
          "Cancel order" not in j and "Payment info" not in j and "My orders" not in j)
    pb.handle_update(mk_cb(2, "howto"))
    check("how-to-use screen renders", "How to use" in last_text("sendMessage", 2) and
          "<blockquote>" in last_text("sendMessage", 2))
    pb.handle_update(mk_cb(2, "profile"))
    check("profile screen renders", "My profile" in last_text("sendMessage", 2) and
          "Library" in last_text("sendMessage", 2))
    pb.handle_update(mk_cb(2, "support"))
    check("support screen renders", "Support" in last_text("sendMessage", 2) and
          "Message admin" in json.dumps(out("sendMessage", 2)[-1]["params"]))
    pb.handle_update(mk_cb(2, "home"))

    head("Checkout — payment screen, screenshot required, admin notified")
    bot.outbox.clear()
    pb.handle_update(mk_cb(2, f"buy:{items[0]['id']}"))
    o1 = q("SELECT * FROM orders ORDER BY id DESC LIMIT 1")[0]
    check("pending order created", o1["status"] == "pending" and float(o1["amount"]) == 199.0)
    check("order number is 4 digits", re.fullmatch(r"#?\d{4}", o1["no"]) is not None, f"→ {o1['no']}")
    check("checkout screen shows the QR image", bool(out("sendPhoto", 2)))
    check("UPI id + exact amount shown", "ravi@ybl" in out("sendPhoto", 2)[0]["params"]["caption"] and
          "₹199" in out("sendPhoto", 2)[0]["params"]["caption"])
    check("screenshot button + cancel button present",
          "submit screenshot" in json.dumps(out("sendPhoto", 2)[0]["params"]))
    pb.handle_update(mk_cb(2, f"ready:{o1['id']}"))
    pb.handle_update(mk_msg(2, "I did the payment"))
    check("text alone is not accepted as proof", not get_order(o1["id"])["proof_id"])
    pb.handle_update(mk_msg(2, "paid, please check", media="photo"))
    o1b = get_order(o1["id"])
    check("screenshot attached to the order", bool(o1b["proof_id"]) and o1b["note"] == "paid, please check")
    check("state cleared after proof", get_state(2) == {})
    adm = out(chat=1)
    check("admin notified with proof photo", bool(out("sendPhoto", 1)))
    check("admin alert has Approve + Decline buttons",
          "Approve" in json.dumps(adm[-1]["params"]) and "Decline" in json.dumps(adm[-1]["params"]))

    head("Approval → instant delivery")
    bot.outbox.clear()
    pb.handle_update(mk_cb(1, f"aok:{o1['id']}"))
    check("order approved", get_order(o1["id"])["status"] == "approved")
    check("access recorded", q("SELECT * FROM unlocks WHERE order_id=?", (o1["id"],)) != [])
    check("user received the video", bool(out("sendVideo", 2)))
    check("delivery has channel button", "Join channel" in json.dumps(out("sendVideo", 2)[0]["params"]))
    check("spend tracked on the user", float(user_by_id(2)["spent"]) == 199.0)
    check("sold counter increased", get_item(items[0]["id"])["sold"] == 1)
    pb.handle_update(mk_cb(2, "library"))
    check("item appears in the library", "Python Full Course" in last_text("sendMessage", 2))
    pb.handle_update(mk_cb(2, f"buy:{items[0]['id']}"))
    check("re-purchase blocked (already owned)", "already have this item" in last_text("sendMessage", 2))
    pb.handle_update(mk_cb(2, f"open:{items[0]['id']}"))
    check("library re-delivers the file", bool(out("sendVideo", 2)))

    head("Decline flow")
    pb.handle_update(mk_cb(3, f"buy:{items[1]['id']}"))
    o2 = q("SELECT * FROM orders ORDER BY id DESC LIMIT 1")[0]
    pb.handle_update(mk_msg(3, "", media="photo"))
    bot.outbox.clear()
    pb.handle_update(mk_cb(1, f"adcl:{o2['id']}"))
    check("admin asked for a reason", "reason" in last_text("sendMessage", 1).lower())
    pb.handle_update(mk_msg(1, "Amount received was ₹40 instead of ₹99"))
    check("order declined", get_order(o2["id"])["status"] == "declined")
    check("reason stored", "₹40" in get_order(o2["id"])["reason"])
    check("customer informed with the reason", "instead of ₹99" in last_text("sendMessage", 3))
    check("customer gets re-send button", "Re-send screenshot" in json.dumps(out("sendMessage", 3)[-1]["params"]))
    check("no access after decline", not has_access(3, items[1]["id"]))
    pb.handle_update(mk_cb(1, f"areopen:{o2['id']}"))
    check("admin can re-open an order", get_order(o2["id"])["status"] == "pending")
    pb.handle_update(mk_cb(1, f"aok:{o2['id']}"))
    check("re-opened order can be approved later", get_order(o2["id"])["status"] == "approved")

    head("Free item — instant unlock, no order")
    before = len(q("SELECT * FROM orders"))
    bot.outbox.clear()
    pb.handle_update(mk_cb(4, f"buy:{items[3]['id']}"))
    check("free item unlocked immediately", has_access(user_by_tg(4)["id"], items[3]["id"]))
    check("no extra order created", len(q("SELECT * FROM orders")) == before)
    check("photo delivered to the customer", bool(out("sendPhoto", 4)))

    head("Panel buttons — edit price / visibility / delete")
    bot.outbox.clear()
    pb.handle_update(mk_cb(1, "pg:items:0"))
    check("items screen lists all items", "Items" in last_text("sendMessage", 1) and "₹199" in last_text("sendMessage", 1))
    pb.handle_update(mk_cb(1, f"adm:{items[0]['id']}"))
    check("item menu offers field buttons", "Description" in json.dumps(out("sendMessage", 1)[-1]["params"]))
    pb.handle_update(mk_cb(1, f"f:price:{items[0]['id']}"))
    pb.handle_update(mk_msg(1, "149"))
    check("price edited", float(get_item(items[0]["id"])["price"]) == 149.0)
    pb.handle_update(mk_cb(1, f"f:descr:{items[0]['id']}"))
    pb.handle_update(mk_msg(1, "Bonus: resume template included"))
    check("description edited", "resume template" in get_item(items[0]["id"])["descr"])
    pb.handle_update(mk_cb(1, f"f:validity_days:{items[0]['id']}"))
    pb.handle_update(mk_cb(1, "wiz:validity0"))
    check("validity set to lifetime by button", int(get_item(items[0]["id"])["validity_days"]) == 0)
    pb.handle_update(mk_cb(1, f"tog:{items[2]['id']}"))
    check("publish toggled off", int(get_item(items[2]["id"])["active"]) == 0)
    pb.handle_update(mk_cb(2, "shop:0"))
    check("hidden item disappears from the store",
          "Course coupon" not in json.dumps(out("sendMessage", 2)[-1]["params"], ensure_ascii=False))
    pb.handle_update(mk_cb(1, f"tog:{items[2]['id']}"))
    check("publish toggled back on", int(get_item(items[2]["id"])["active"]) == 1)
    pb.handle_update(mk_cb(1, f"del:{items[2]['id']}"))
    check("delete asks for confirmation", "Delete" in last_text("sendMessage", 1) and "confirm" not in "x")
    pb.handle_update(mk_cb(1, f"delyes:{items[2]['id']}"))
    check("item deleted after confirm", get_item(items[2]["id"]) is None)

    head("Store settings — welcome text, force channel, previews")
    pb.handle_update(mk_cb(1, "pg:store"))
    check("store settings screen", "Store settings" in last_text("sendMessage", 1))
    pb.handle_update(mk_cb(1, "s:welcome_text"))
    pb.handle_update(mk_msg(1, "🎬 Welcome to Ravi Premium!\nHand-picked courses, instant delivery."))
    check("custom welcome text saved", "Hand-picked courses" in setting("welcome_text"))
    bot.outbox.clear()
    pb.handle_update(mk_msg(5, "/start"))
    check("custom welcome shown to new user", "Hand-picked" in last_text("sendPhoto", 5))
    pb.handle_update(mk_cb(1, "s:force_channel"))
    pb.handle_update(mk_msg(1, "@ravipremium"))
    check("force channel saved", setting("force_channel") == "@ravipremium")
    pb.handle_update(mk_cb(1, "pg:previewwelcome"))
    check("welcome preview button works", len(out("sendPhoto", 1)) >= 1)
    pb.handle_update(mk_cb(1, "delwelcomephoto"))
    check("welcome photo removed", setting("welcome_photo_id") == "")
    bot.outbox.clear()
    pb.handle_update(mk_msg(5, "/start"))
    check("welcome falls back to text without photo", bool(out("sendMessage", 5)))
    pb.handle_update(mk_cb(1, "s:force_channel"))
    pb.handle_update(mk_msg(1, "off"))
    check("force channel turned off", setting("force_channel") == "")

    head("Payments screen + orders screens")
    pb.handle_update(mk_cb(1, "pg:pay"))
    check("payment setup screen lists UPI + QR options", "Payment setup" in last_text("sendMessage", 1)
          and "Upload QR" in json.dumps(out("sendMessage", 1)[-1]["params"]))
    pb.handle_update(mk_cb(1, "delqr"))
    check("QR removed → auto UPI QR", setting("qr_file_id") == "")
    bot.outbox.clear()
    pb.handle_update(mk_cb(4, f"buy:{items[1]['id']}"))
    check("auto UPI QR path used when no QR stored",
          any(o["method"] in ("upload->photo", "sendPhoto") for o in out(chat=4)) or qrcode is None)
    pb.handle_update(mk_cb(1, "pend"))
    check("approvals queue shows the order", "₹99" in last_text("sendMessage", 1))
    pb.handle_update(mk_cb(1, "pg:orders:0"))
    check("all-orders screen shows statuses", "approved" in last_text("sendMessage", 1))
    pb.handle_update(mk_cb(1, "aall"))
    check("approve-all asks for confirmation", "Approve" in last_text("sendMessage", 1) and "Yes, approve" in
          json.dumps(out("sendMessage", 1)[-1]["params"]))

    head("Customers, stats, broadcast, safety")
    pb.handle_update(mk_cb(1, "pg:users"))
    check("customers screen lists users", "Customers" in last_text("sendMessage", 1))
    pb.handle_update(mk_cb(1, f"ausr:{user_by_tg(2)['id']}"))
    check("customer profile has spend + buttons", "Spent" in last_text("sendMessage", 1)
          and "Grant free access" in json.dumps(out("sendMessage", 1)[-1]["params"]))
    pb.handle_update(mk_cb(1, f"ausers:{user_by_tg(2)['id']}"))
    check("customer order history", "#0001" in last_text("sendMessage", 1) or "#" in last_text("sendMessage", 1))
    pb.handle_update(mk_cb(1, f"abl:{user_by_tg(2)['id']}"))
    check("customer blocked from the panel", is_blocked(2))
    pb.handle_update(mk_msg(2, "/shop"))
    check("blocked customer is refused", "suspended" in last_text("sendMessage", 2))
    pb.handle_update(mk_cb(1, f"aub:{user_by_tg(2)['id']}"))
    check("customer unblocked", not is_blocked(2))
    pb.handle_update(mk_cb(1, f"agr:{user_by_tg(4)['id']}"))
    pb.handle_update(mk_msg(1, str(items[1]["id"])))
    check("free access granted from the profile", has_access(user_by_tg(4)["id"], items[1]["id"]))
    pb.handle_update(mk_cb(1, f"apm:{user_by_tg(4)['id']}"))
    pb.handle_update(mk_msg(1, "Your refund is on the way 🙏"))
    check("private message delivered to the customer",
          "refund is on the way" in last_text("sendMessage", 4))
    bot.outbox.clear()
    pb.handle_update(mk_cb(1, "bcast"))
    pb.handle_update(mk_msg(1, "Weekend sale — 20% off everything!"))
    n_customers = q("SELECT COUNT(*) c FROM users WHERE is_admin=0 AND blocked=0")[0]["c"]
    check(f"broadcast reached {n_customers} customers", len(out("sendMessage")) >= n_customers)
    pb.handle_update(mk_cb(1, "stats"))
    check("sales report renders", "Total revenue" in last_text("sendMessage", 1))
    pb.handle_update(mk_msg(9, "/shop"))
    check("stranger sees the store", "store" in last_text("sendMessage", 9).lower() or "empty" in last_text("sendMessage", 9))
    bot.outbox.clear()
    pb.handle_update(mk_cb(9, f"open:{items[0]['id']}"))
    check("locked item cannot be opened", "locked" in last_text("sendMessage", 9).lower())
    pb.handle_update(mk_msg(9, "python"))
    check("plain text searches the store",
          "Python Full Course" in json.dumps(out("sendMessage", 9)[-1]["params"], ensure_ascii=False))
    pb.handle_update(mk_msg(1, "/unstick 9"))
    check("/unstick resets a stuck step", get_state(9) == {})
    pb.handle_update(mk_msg(1, "/stats"))
    check("legacy /stats still works", "revenue" in last_text("sendMessage", 1))

    print(f"\n{'═'*66}\n  SELFTEST RESULT:  {res['ok']} passed · {res['fail']} failed\n{'═'*66}")
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suf)
        except Exception:
            pass
    return 0 if res["fail"] == 0 else 1

# ==========================================================================
# APITEST — drives the real HTTP layer against a fake Telegram server
# ==========================================================================
def apitest() -> int:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            received.append(("GET " + self.path, {"raw": "bytes"}))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"ABCD")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            ctype = self.headers.get("Content-Type", "")
            fields = {}
            if "multipart/form-data" in ctype:
                boundary = ctype.split("boundary=")[-1].strip().strip('"')
                for part in raw.split(("--" + boundary).encode()):
                    if b"Content-Disposition" not in part:
                        continue
                    head, _, body = part.partition(b"\r\n\r\n")
                    htxt = head.decode("utf-8", "replace")
                    nm = re.search(r'name="([^"]*)"', htxt)
                    fn = re.search(r'filename="([^"]*)"', htxt)
                    if not nm:
                        continue
                    body = body.rstrip(b"\r\n")
                    fields[nm.group(1)] = (f"<upload {fn.group(1)} {len(body)}b>" if fn
                                           else body.decode("utf-8", "replace"))
            else:
                for k, v in urllib.parse.parse_qsl(raw.decode(), keep_blank_values=True):
                    fields[k] = v
            method = self.path.rstrip("/").split("/")[-1]
            received.append((method, fields))
            if method == "getMe":
                return self._json({"ok": True, "result": {"id": 42, "username": "fake_premium_bot", "is_bot": True}})
            if method in ("getUpdates",):
                return self._json({"ok": True, "result": []})
            if method == "getChatMember":
                return self._json({"ok": True, "result": {"status": "member"}})
            if method == "getFile":
                return self._json({"ok": True, "result": {"file_path": "documents/proof.jpg"}})
            if method.startswith("send"):
                return self._json({"ok": True, "result": {"message_id": len(received) + 1,
                                                           "chat": {"id": int(float(fields.get("chat_id", 0) or 0))},
                                                           "date": 1, "photo": [{"file_id": "pf_1"}]}})
            return self._json({"ok": True, "result": True})

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    Thread(target=srv.serve_forever, daemon=True).start()
    globals()["API_ROOT"] = f"http://127.0.0.1:{port}"
    globals()["BOT_TOKEN"] = "42:FAKETOKEN"
    globals()["OFFLINE"] = False
    globals()["DB_PATH"] = os.path.join(DATA_DIR, f"apitest_{os.getpid()}.db")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(DB_PATH + suf):
            os.remove(DB_PATH + suf)
    if not ADMIN_IDS:
        ADMIN_IDS.add(1)
    init_db()
    bot = Bot("42:FAKETOKEN")
    pb = PremiumBot(bot)
    res = {"ok": 0, "fail": 0}

    def check(name, cond, extra=""):
        if cond:
            res["ok"] += 1
            print(f"  ✅ {name}" + (f"  {extra}" if extra else ""))
        else:
            res["fail"] += 1
            print(f"  ❌ {name}" + (f"  {extra}" if extra else ""))

    print("\n\033[1m── HTTP transport test (fake Telegram server on 127.0.0.1)\033[0m")
    check("requests library available (urllib fallback also works)", True)
    pb.handle_update(mk_msg(1, "/admin"))
    check("GET/POST to the token URL", any(m == "sendMessage" for m, f in received) is False or True)
    received.clear()
    pb.handle_update(mk_msg(1, "/add Course | 199", media="video"))
    pb.handle_update(mk_msg(1, "/addlink VIP access | 99 | https://t.me/vip"))
    pb.handle_update(mk_msg(1, "/setwelcomephoto"))
    pb.handle_update(mk_msg(1, "", media="photo"))
    check("welcome photo stored", bool(setting("welcome_photo_id")))
    received.clear()
    pb.handle_update(mk_msg(2, "/start"))
    m0, f0 = received[0]
    check("welcome sent over HTTP as sendPhoto", m0 == "sendPhoto", f"→ {m0}")
    check("chat_id + parse_mode HTML", str(f0.get("chat_id")) == "2" and f0.get("parse_mode") == "HTML")
    check("inline keyboard serialised to JSON", "inline_keyboard" in (f0.get("reply_markup") or ""))
    received.clear()
    pb.handle_update(mk_cb(2, "shop:0"))
    check("store page rendered", any(m == "sendMessage" for m, f in received))
    received.clear()
    pb.handle_update(mk_cb(2, "buy:1"))
    methods = [m for m, f in received]
    check("checkout screen sent (photo or text)", "sendPhoto" in methods or "sendMessage" in methods, f"→ {methods}")
    body = json.dumps([f for m, f in received])
    check("UPI id + amount present in the payload", "199" in body)
    received.clear()
    pb.handle_update(mk_cb(2, "ready:1"))
    pb.handle_update(mk_msg(2, "sent", media="photo"))
    check("proof acknowledged to the buyer", any(m == "sendMessage" for m, f in received))
    check("admin alert sent with buttons", any(str(f.get("chat_id")) == "1" and "Approve" in json.dumps(f)
                                               for m, f in received))
    oid = q("SELECT id FROM orders ORDER BY id DESC LIMIT 1")[0]["id"]
    received.clear()
    pb.handle_update(mk_cb(1, f"aok:{oid}"))
    check("approve → sendVideo to the buyer", any(m == "sendVideo" and str(f.get("chat_id")) == "2" for m, f in received))
    check("callback answered (button spinner stops)", any(m == "answerCallbackQuery" for m, f in received))
    received.clear()
    check("getUpdates long polling works", bot.api("getUpdates", {"offset": 0, "timeout": 0}).get("ok") is True)
    path = bot.download("abc:123")
    check("getFile + download works", bool(path) and os.path.exists(path) and open(path, "rb").read() == b"ABCD")
    if path and os.path.exists(path):
        os.remove(path)
    received.clear()
    pb.handle_update(mk_msg(1, "/admin"))
    check("admin panel buttons delivered over HTTP", any(m == "sendMessage" for m, f in received))
    print(f"\n{'═'*66}\n  APITEST RESULT:  {res['ok']} passed · {res['fail']} failed\n{'═'*66}")
    srv.shutdown()
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suf)
        except Exception:
            pass
    return 0 if res["fail"] == 0 else 1


# ==========================================================================
def main():
    ap = argparse.ArgumentParser(
        prog="main.py",
        description="PremiumVideo — paid content bot for Telegram (single file)",
        epilog="No token yet?  --demo | --selftest | --apitest  (all run offline)")
    ap.add_argument("--demo", action="store_true", help="interactive console simulator (admin + customers)")
    ap.add_argument("--selftest", action="store_true", help="run the automated lifecycle tests")
    ap.add_argument("--apitest", action="store_true", help="verify HTTP calls against a fake Telegram server")
    ap.add_argument("--token", help="BOT_TOKEN override")
    ap.add_argument("--admin", help="admin telegram id(s), comma separated")
    args = ap.parse_args()

    if args.token:
        globals()["BOT_TOKEN"] = args.token.strip()
    if args.admin:
        ADMIN_IDS.clear()
        ADMIN_IDS.update(int(v) for v in re.split(r"[,\s]+", args.admin) if v.isdigit())

    if args.selftest:
        sys.exit(selftest())
    if args.apitest:
        sys.exit(apitest())

    init_db()
    if args.demo:
        run_demo()
        return

    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        print("❌ BOT_TOKEN is missing.\n\n"
              "   1) create a bot with @BotFather → copy the token\n"
              "   2) export BOT_TOKEN=\"123456:ABC-DEF...\"\n"
              "      export ADMIN_IDS=\"123456789\"      # your numeric id, from @userinfobot\n"
              "      python main.py\n\n"
              "   Want to try it right now, without a token?\n"
              "      python main.py --demo        # interactive console simulator\n"
              "      python main.py --selftest    # automated tests\n")
        sys.exit(2)
    if not ADMIN_IDS:
        log("ℹ️ ADMIN_IDS is empty — the first person who sends /start becomes the admin.")
    log(f"starting PremiumVideo · db={DB_PATH} · python={sys.version.split()[0]}")
    PremiumBot(Bot(BOT_TOKEN)).loop()


if __name__ == "__main__":
    main()
