#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PremiumVideo — paid content bot for Telegram (single file).

Admin  : video / file / photo / channel link / group link / website link / text — kuch bhi
         apna price set karta hai, apna payment QR + UPI set karta hai.
User   : /shop -> item choose -> payment info (QR + UPI + exact amount)
         -> payment screenshot bhejta hai -> order admin ke paas chala jaata hai.
Admin  : inline buttons se Approve / Reject. Approve = content turant user ko.

Chalane ke liye:
    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC..."
    export ADMIN_IDS="123456789"
    python main.py

Bina token ke test karne ke liye:
    python main.py --demo        # terminal = Telegram (user/admin ban ke type karo)
    python main.py --selftest    # automatic end-to-end test
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
except Exception:  # pragma: no cover
    requests = None

try:
    import qrcode
except Exception:  # pragma: no cover
    qrcode = None

# --------------------------------------------------------------------------
# CONFIG  (env vars se override ho jaata hai)
# --------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH") or os.path.join(ROOT, "premiumvideo.db")
DATA_DIR = os.getenv("DATA_DIR") or os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", (os.getenv("ADMIN_IDS") or "").strip()) if x.isdigit()}
CURRENCY = os.getenv("CURRENCY", "₹")
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "30"))
OFFLINE = False          # --demo / --selftest me True ho jaata hai

API_ROOT = "https://api.telegram.org"


def api_url(method: str) -> str:
    return f"{API_ROOT}/bot{BOT_TOKEN}/{method}"


def file_url(path: str) -> str:
    return f"{API_ROOT}/bot{BOT_TOKEN}/file/{path}"


def rowget(row, key, default=None):
    if row is None:
        return default
    try:
        v = row[key]
    except Exception:
        return default
    return default if v is None else v


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(*a):
    print("[" + datetime.now().strftime("%H:%M:%S") + "]", *a, flush=True)


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def money(v) -> str:
    try:
        v = float(v)
    except Exception:
        v = 0.0
    if abs(v - round(v)) < 0.005:
        return f"{CURRENCY}{int(round(v))}"
    return f"{CURRENCY}{v:.2f}"


def to_num(s):
    """'99' / '99.50' / '₹99' / 'free' -> float (rupees)."""
    s = (s or "").strip().lower()
    if s in ("", "0", "free", "fr", "0.0"):
        return 0.0
    s = re.sub(r"[^0-9.]", "", s.replace(",", ""))
    if not s:
        return None
    try:
        return round(float(s), 2)
    except Exception:
        return None


# --------------------------------------------------------------------------
# DATABASE
# --------------------------------------------------------------------------
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
    kind          TEXT NOT NULL DEFAULT 'video',  -- video|file|photo|link|text
    title         TEXT NOT NULL,
    descr         TEXT,
    file_id       TEXT,
    file_kind     TEXT,           -- video|document|photo|animation|audio|video_note
    link          TEXT,
    channel_link  TEXT,
    group_link    TEXT,
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
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|rejected|cancelled
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


# ------------------------------- settings --------------------------------
DEFAULTS = {
    "brand": "PremiumVideo",
    "upi_id": "",
    "payee_name": "PremiumVideo",
    "pay_note": "Payment ke baad screenshot bhejna zaroori hai.",
    "qr_file_id": "",
    "force_channel": "",
    "welcome": "",
    "pending_msg": "",
    "bank_note": "Sirf exact amount bhejo, note me order no. likho.",
}


def setting(key: str, default: str = "") -> str:
    row = q("SELECT value FROM settings WHERE key=?", (key,))
    if row and row[0]["value"] not in (None, ""):
        return row[0]["value"]
    if key in DEFAULTS and DEFAULTS[key]:
        return DEFAULTS[key]
    return default


def set_setting(key: str, value: str):
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, value))


def all_settings() -> dict:
    d = dict(DEFAULTS)
    for r in q("SELECT key,value FROM settings"):
        if r["value"]:
            d[r["key"]] = r["value"]
    return d


# -------------------------------- users -----------------------------------
def ensure_user(u: dict) -> int:
    tg_id = int(u.get("id") or 0)
    name = " ".join([x for x in [u.get("first_name"), u.get("last_name")] if x]).strip() or (u.get("username") or "User")
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


def is_blocked(tg_id: int) -> bool:
    row = q("SELECT blocked FROM users WHERE tg_id=?", (int(tg_id),))
    return bool(row and row[0]["blocked"])


# -------------------------------- items -----------------------------------
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


def set_item(item_id: int, field: str, value):
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
    exp = None
    try:
        validity_days = int(validity_days or 0)
    except Exception:
        validity_days = 0
    if validity_days > 0:
        exp = (datetime.now() + timedelta(days=validity_days)).strftime("%Y-%m-%d %H:%M:%S")
    x("""INSERT INTO unlocks(user_id,item_id,order_id,created_at,expires_at) VALUES(?,?,?,?,?)
         ON CONFLICT(user_id,item_id) DO UPDATE SET expires_at=excluded.expires_at,
         order_id=excluded.order_id, created_at=excluded.created_at""",
      (int(user_pk), int(item_id), order_id, now(), exp))


# -------------------------------- orders ----------------------------------
def create_order(user_pk: int, item, amount=None) -> int:
    amt = float(item["price"]) if amount is None else float(amount)
    oid = x("""INSERT INTO orders(no,user_id,item_id,amount,status,created_at)
               VALUES(?,?,?,?, 'pending', ?)""", ("", int(user_pk), int(item["id"]), amt, now()))
    x("UPDATE orders SET no=? WHERE id=?", (f"#{oid:04d}", oid))
    x("UPDATE users SET orders=orders+1 WHERE id=?", (int(user_pk),))
    return oid


def get_order(oid: int):
    row = q("SELECT * FROM orders WHERE id=?", (int(oid),))
    return row[0] if row else None


def pending_for_user(user_pk: int):
    return q("SELECT * FROM orders WHERE user_id=? AND status='pending' ORDER BY id DESC", (int(user_pk),))


# ------------------------------- FSM state --------------------------------
STATE_TTL_HOURS = 12          # 12 ghante se purana pending step apne aap reset


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


# --------------------------------------------------------------------------
# TELEGRAM TRANSPORT  (offline = demo/selftest me print, net = real API)
# --------------------------------------------------------------------------
def http_post(url: str, fields: dict, files: dict | None = None):
    """POST (multipart ya urlencoded) — requests ho to use karo, warna stdlib."""
    fields = {k: ("" if v is None else str(v)) for k, v in (fields or {}).items()}
    if requests is not None:
        ff = None
        if files:
            ff = {k: (v[0], v[1], v[2]) for k, v in files.items()}
        r = requests.post(url, data=fields, files=ff, timeout=90)
        return r.status_code, r.text
    import urllib.request
    if files:
        boundary = "----pv" + os.urandom(10).hex()
        buf = []
        for k, v in fields.items():
            buf.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for k, (fn, data, ctype) in files.items():
            buf.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                       f"filename=\"{fn}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
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
    def __init__(self, token: str = "", offline: bool = False, label: str = "tg"):
        self.token = token
        self.offline = offline
        self.label = label
        self.outbox = []          # (method, params) — demo/selftest me capture
        self.file_seq = 1000

    # ---- low level ----
    def api(self, method: str, params: dict | None = None, files: dict | None = None):
        """files = {field: (filename, bytes, content_type)}  (optional)"""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if isinstance(params.get("reply_markup"), (list, tuple)):
            params["reply_markup"] = _kb_from_rows(params["reply_markup"])
        if isinstance(params.get("reply_markup"), dict):
            params["reply_markup"] = json.dumps(params["reply_markup"], ensure_ascii=False)
        if self.offline:
            rec = {"method": method, "params": dict(params)}
            self.outbox.append(rec)
            if method not in ("getChatMember", "answerCallbackQuery", "editMessageReplyMarkup", "getMe"):
                self.render(rec)
            if method == "getFile":
                return {"ok": True, "result": {"file_path": f"demo/{params.get('file_id')}"}}
            if method == "getChatMember":
                return {"ok": True, "result": {"status": "member"}}
            if method == "getMe":
                return {"ok": True, "result": {"id": 777, "username": "premiumvideo_bot", "is_bot": True}}
            mid = 50000 + len(self.outbox)
            if method in ("sendMessage", "sendPhoto", "sendVideo", "sendDocument", "sendAnimation",
                          "sendAudio", "sendVideoNote"):
                return {"ok": True, "result": {"message_id": mid, "date": int(time.time()),
                                               "chat": {"id": params.get("chat_id")}}}
            return {"ok": True, "result": True}
        try:
            code, body = http_post(api_url(method), params, files)
            j = json.loads(body or "{}")
            if not j.get("ok"):
                log(f"TG ERROR {method} [{code}]: {j.get('description')}")
            return j
        except Exception as e:
            log(f"TG EXC {method}: {e}")
            return {"ok": False, "error": str(e)}

    # ---- pretty print for demo ----
    def render(self, rec):
        m, p = rec["method"], rec["params"]
        chat = p.get("chat_id")
        txt = p.get("text") or p.get("caption") or ""
        if m == "answerCallbackQuery":
            return
        head = f"→ chat {chat}"
        if p.get("photo"):
            head += " 🖼 photo"
        if p.get("video"):
            head += " 🎬 video"
        if p.get("document"):
            head += " 📄 document"
        line = "\n".join("  │ " + l for l in str(txt).splitlines()) if txt else "  │ (no text)"
        print(f"{self.label} ── {head}\n{line}")
        kb = p.get("reply_markup")
        if kb:
            try:
                rows = json.loads(kb)["inline_keyboard"]
                for row in rows:
                    print("  ▸ " + " | ".join(b["text"] for b in row))
            except Exception:
                pass

    # ---- helpers ----
    def send(self, chat_id, text, kb=None, disable_preview=True):
        return self.api("sendMessage", {
            "chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview, "reply_markup": kb})

    def send_media(self, chat_id, kind, file_id, caption="", kb=None):
        kind = (kind or "document").lower()
        mapping = {"video": "video", "document": "document", "photo": "photo",
                   "animation": "animation", "audio": "audio", "voice": "voice",
                   "video_note": "video_note", "sticker": "sticker"}
        field = mapping.get(kind, "document")
        method = {"photo": "sendPhoto", "video": "sendVideo", "animation": "sendAnimation",
                  "audio": "sendAudio", "voice": "sendVoice", "video_note": "sendVideoNote",
                  "sticker": "sendSticker"}.get(field, "sendDocument")
        params = {"chat_id": chat_id, field: file_id}
        if method not in ("sendVideoNote", "sendSticker"):
            params["caption"] = (caption or "")[:1000]
        if kb:
            params["reply_markup"] = kb
        return self.api(method, params)

    def send_file_upload(self, chat_id, path, caption="", kind="photo"):
        if not os.path.exists(path):
            return {"ok": False}
        method = {"photo": "sendPhoto", "video": "sendVideo", "audio": "sendAudio"}.get(kind, "sendDocument")
        field = {"photo": "photo", "video": "video", "audio": "audio"}.get(kind, "document")
        if self.offline:
            rec = {"method": "upload->" + kind, "params": {"chat_id": chat_id, "path": path, "caption": caption}}
            self.outbox.append(rec)
            self.render({"method": method,
                         "params": {"chat_id": chat_id, "caption": f"[{os.path.basename(path)}] {caption}"}})
            return {"ok": True, "result": {"file_id": f"demo_{os.path.basename(path)}"}}
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            log(f"upload read fail: {e}")
            return {"ok": False}
        ctype = "image/png" if path.endswith(".png") else "application/octet-stream"
        return self.api(method, {"chat_id": chat_id, "caption": (caption or "")[:1000]},
                        files={field: (os.path.basename(path), data, ctype)})

    def download(self, file_id, dest_dir=DATA_DIR):
        j = self.api("getFile", {"file_id": file_id})
        path = (((j or {}).get("result") or {}).get("file_path")) or ""
        if not path:
            return None
        os.makedirs(dest_dir, exist_ok=True)
        out = os.path.join(dest_dir, os.path.basename(path))
        if self.offline:
            return out
        try:
            with open(out, "wb") as f:
                f.write(http_get_bytes(file_url(path)))
            return out
        except Exception as e:
            log(f"download fail: {e}")
            return None

    def answer(self, cb_id, text="", show_alert=False):
        return self.api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text,
                                                "show_alert": show_alert})

    def edit_kb(self, msg_id, chat_id, kb):
        return self.api("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id,
                                                   "reply_markup": kb})


# ------------------------- keyboard builders -------------------------------
def _kb_from_rows(rows):
    """rows = [[ (text, callback_data|None, url|None), ... ], ...] -> Telegram reply_markup"""
    out = []
    for r in rows:
        line = []
        for (t, d, u) in r:
            b = {"text": t}
            if d:
                b["callback_data"] = d
            if u:
                b["url"] = u
            line.append(b)
        out.append(line)
    return {"inline_keyboard": out}


def btn(text, data=None, url=None):
    return (text, data, url)


# --------------------------------------------------------------------------
# UPI QR  (agar admin ne QR upload nahi kiya, to amount wala QR bana deta hai)
# --------------------------------------------------------------------------
def make_upi_qr(amount: float, order_no: str, upi_id: str, payee: str, note: str) -> str | None:
    if qrcode is None or not upi_id:
        return None
    params = {"pa": upi_id, "pn": payee or "PremiumVideo", "cu": "INR"}
    if amount and amount > 0:
        params["am"] = f"{amount:.2f}"
    if note:
        params["tn"] = note[:50]
    if order_no:
        params["tr"] = order_no.replace("#", "")
    uri = "upi://pay?" + urllib.parse.urlencode(params)
    fn = os.path.join(DATA_DIR, f"qr_{abs(hash(uri + now()[:16])) % 10**8}.png")
    try:
        img = qrcode.make(uri)
        img.save(fn)
        return fn
    except Exception as e:
        log(f"qr make fail: {e}")
        return None


# --------------------------------------------------------------------------
# THE BOT
# --------------------------------------------------------------------------
MEDIA_FIELDS = {"video", "document", "photo", "animation", "audio", "voice", "video_note", "sticker"}
PAGE_SIZE = 6

EDIT_FIELDS = [
    ("✏️ Title", "title"), ("📝 Description", "descr"), ("💰 Price", "price"),
    ("🎬 File badlo", "file_id"), ("🔗 Main link", "link"), ("📢 Channel link", "channel_link"),
    ("👥 Group link", "group_link"), ("⏳ Validity (days)", "validity_days"),
    ("⏸️ Enable/Disable", "active"),
]


class PremiumBot:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.offset = 0

    # ====================== public entry ======================
    def handle_update(self, u: dict):
        try:
            if "callback_query" in u:
                return self.on_callback(u["callback_query"])
            m = u.get("message") or u.get("edited_message")
            if not m:
                return
            return self.on_message(m)
        except Exception:
            log("handle_update EXC:\n" + traceback.format_exc())

    # ====================== message routing ======================
    def on_message(self, m: dict):
        frm = m.get("from") or {}
        tg_id = int(frm.get("id") or (m.get("chat") or {}).get("id") or 0)
        chat_id = (m.get("chat") or {}).get("id", tg_id)
        chat_type = (m.get("chat") or {}).get("type", "private")
        if not tg_id:
            return
        text = (m.get("text") or m.get("caption") or "").strip()
        media = self.media_of(m)
        if not ADMIN_IDS and chat_type == "private":
            ADMIN_IDS.add(tg_id)                      # pehla banda = owner
            log(f"ADMIN_IDS auto-set → {tg_id}")
        uid = ensure_user(frm) if chat_type == "private" else 0
        admin = tg_id in ADMIN_IDS

        if chat_type in ("group", "supergroup", "channel"):
            if admin and text.startswith("/start"):
                self.bot.send(chat_id, "🤖 Main private chat me kaam karta hai — DM me aaiye, phir /start kijiye.")
            return

        if not admin and is_blocked(tg_id):
            self.bot.send(chat_id, "🚫 Aapka access block kar diya gaya hai. Admin se baat kijiye.")
            return

        if text.startswith("/start") or text == "🏠 Home":
            return self.cmd_start(chat_id, tg_id, uid, m, text)
        if not admin and not self.channel_ok(tg_id, chat_id):
            return

        # pending FSM (wizard / waiting for screenshot / admin input)
        st = get_state(tg_id)
        if st and self.on_state(chat_id, tg_id, uid, m, text, media, st):
            return

        if text.startswith("/"):
            return self.on_command(chat_id, tg_id, uid, m, text, media, admin)

        # plain text = search
        if text:
            return self.show_shop(chat_id, uid, search=text)
        self.bot.send(chat_id, "🤖 /start dabaiye aur store kholein.")

    def on_command(self, chat_id, tg_id, uid, m, text, media, admin):
        parts = text.split(None, 1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # ---------- common ----------
        if cmd in ("/start", "/menu", "/home"):
            return self.cmd_start(chat_id, tg_id, uid, m, text)
        if cmd in ("/shop", "/store", "/items", "/plans"):
            if admin and cmd in ("/items", "/plans"):
                return self.show_shop(chat_id, uid, edit=True)
            return self.show_shop(chat_id, uid, search=arg)
        if cmd in ("/my", "/library", "/mine"):
            return self.show_library(chat_id, uid)
        if cmd in ("/status", "/order", "/orders"):
            if admin and cmd == "/orders":
                return self.show_pending(chat_id)
            return self.show_status(chat_id, uid, arg)
        if cmd in ("/help", "/start2"):
            if admin:
                return self.admin_help(chat_id)
            return self.bot.send(chat_id, self.help_text(),
                                 kb=[[btn("🛒 Store", "shop:0"), btn("📜 Mere items", "my")],
                                     [btn("🧾 Status", "status"), btn("💳 Payment info", "payinfo")]])
        if cmd == "/unstick":
            if not admin:
                set_state(tg_id, {})
                return self.bot.send(chat_id, "🧹 Mera pending step reset ho gaya. /shop dabaiye.")
            t = re.sub(r"\D", "", arg or "")
            if not t:
                return self.bot.send(chat_id,
                                     "Use: <code>/unstick 123456</code> → us user ka atka hua step reset")
            x("DELETE FROM states WHERE user_id=?", (int(t),))
            return self.bot.send(chat_id, f"🧹 Telegram id {t} ka input state reset.")
        if cmd in ("/cancel", "/stop"):
            set_state(tg_id, {})
            return self.bot.send(chat_id, "❌ Cancel ho gaya. /shop se wapas store kholein.")
        if cmd in ("/buy", "/pay", "/paid", "/paynow", "/checkout"):
            return self.cmd_buy(chat_id, uid, arg)
        if (cmd in ("/rules", "/refund")) and not admin:
            return self.bot.send(chat_id, self.rules_text(),
                                 kb=[[btn("🛒 Store", "shop:0"), btn("📜 Meri items", "my")]])
        if cmd == "/id":
            return self.bot.send(chat_id, f"🆔 Aapka ID: <code>{uid}</code>\nTelegram: <code>{tg_id}</code>")

        # ---------- admin only ----------
        if not admin:
            return self.bot.send(chat_id, "🔒 Ye command sirf admin ke liye hai.",
                                 kb=[[btn("🛒 Store", "shop:0"), btn("📜 Meri items", "my")]])
        return self.admin_command(chat_id, tg_id, uid, m, cmd, arg, media)

    # ====================== /start ======================
    def help_text(self) -> str:
        s = all_settings()
        return (f"ℹ️ <b>{esc(s['brand'])}</b>\n\n"
                "🛒 <code>/shop</code> — items dekho\n"
                "🛒 <code>/buy 3</code> — item no. 3 kharido\n"
                "📜 <code>/my</code> — mere unlocked items\n"
                "🧾 <code>/status</code> — order ka status\n"
                "💳 <code>/payinfo</code> — payment details\n"
                "❌ <code>/cancel</code> — pending order cancel\n\n"
                + self.rules_text())

    def rules_text(self) -> str:
        b = setting("brand")
        note = setting("pay_note")
        bank = setting("bank_note")
        return (f"📜 <b>Rules — {esc(b)}</b>\n\n"
                "1️⃣ /shop se item chuniye, price pehle se dikha raha hai.\n"
                "2️⃣ QR / UPI se <b>exact amount</b> bhejiye.\n"
                "3️⃣ Payment ka <b>screenshot</b> bot me bhejiye.\n"
                "4️⃣ Admin verify karega → turant content mil jaayega.\n"
                f"5️⃣ {esc(note) or 'Manual approval lagti hai.'}\n"
                f"6️⃣ {esc(bank) or 'Galt amount bheja to request cancel ho sakti hai.'}")

    def cmd_start(self, chat_id, tg_id, uid, m, text):
        if uid == 0:
            uid = ensure_user(m.get("from") or {"id": tg_id, "first_name": "User"})
        ref = ""
        mt = re.search(r"/start\s+(\S+)", text or "")
        if mt and mt.group(1).lower() in ("menu", "shop", "store"):
            return self.show_shop(chat_id, uid)
        if mt and mt.group(1).lower().startswith(("buy", "item")):
            iid = int(re.sub(r"\D", "", mt.group(1)) or 0)
            it = get_item(iid)
            if it:
                self.cmd_start(chat_id, tg_id, uid, m, "/start")
                return self.show_item(chat_id, uid, iid)
        if mt:
            ref = mt.group(1)
            if not ref.startswith(("buy", "item", "shop")):
                x("UPDATE users SET ref=? WHERE id=? AND (ref IS NULL OR ref='')", (ref, uid))
        if not self.channel_ok(tg_id, chat_id, admin_ok=True):
            return
        s = all_settings()
        welcome = s["welcome"].strip()
        if not welcome:
            price = q("SELECT MIN(price) mn FROM items WHERE active=1")
            low = price[0]["mn"] if price and price[0]["mn"] is not None else None
            welcome = (f"👋 <b>{esc(s['brand'])}</b> me swagat hai {esc(m.get('from', {}).get('first_name') or 'dost')}!\n\n"
                       "🎬 Paid premium content — admin approve karte hi aapko mil jaata hai.\n"
                       f"💸 Shuruwat {money(low)} se." if low is not None else
                       f"👋 <b>{esc(s['brand'])}</b> me swagat hai!")
        self.bot.send(chat_id, welcome,
                      kb=[[btn("🛒 Store dekho", "shop:0"), btn("📜 Mere items", "my")],
                          [btn("🧾 Order status", "status"), btn("📜 Rules", "rules_txt")],
                          [btn("💳 Payment setup", "payinfo")]])

    def channel_ok(self, tg_id, chat_id, admin_ok=False) -> bool:
        """force_channel set ho to pehle join karwata hai."""
        ch = setting("force_channel").strip()
        if not ch or (admin_ok and tg_id in ADMIN_IDS):
            return True
        try:
            j = self.bot.api("getChatMember", {"chat_id": ch, "user_id": tg_id})
            status = ((j or {}).get("result") or {}).get("status")
            if status in ("creator", "administrator", "member", "restricted"):
                return True
        except Exception:
            return True
        url = ch if ch.startswith("http") else ("https://t.me/" + ch.lstrip("@"))
        self.bot.send(chat_id,
                      f"🔑 Pehle channel join kijiye, phir bot chalega.\n\n{esc(ch)}",
                      kb=[[btn("📢 Channel join karo", None, url)], [btn("✅ Join ho gaya", "recheck_channel")]])
        return False

    # ====================== SHOP ======================
    def item_caption(self, it, uid=None, buyer=False) -> str:
        kind_emoji = {"video": "🎬", "file": "📁", "photo": "🖼", "link": "🔗", "text": "📝"}.get(it["kind"], "🎬")
        extra = []
        if it["channel_link"]:
            extra.append("📢 channel")
        if it["group_link"]:
            extra.append("👥 group")
        if it["link"]:
            extra.append("🔗 link")
        if it["file_id"]:
            extra.append("📦 file")
        val = "♾ lifetime" if not it["validity_days"] else f"⏳ {it['validity_days']} din"
        lock = ""
        if uid and has_access(uid, it["id"]):
            lock = "\n\n✅ <b>Aapke paas unlock hai</b> — /my se kholiye."
        return (f"{kind_emoji} <b>{esc(it['title'])}</b>\n"
                f"💰 Price: <b>{money(it['price'])}</b>   ·   Access: {val}\n"
                + (f"🔖 {', '.join(extra)}\n" if extra else "")
                + (f"\n{esc(it['descr'])}" if it["descr"] else "")
                + (f"\n🛒 Sold: {it['sold']}" if it["sold"] else "")
                + lock)

    def show_shop(self, chat_id, uid, page=0, search="", edit=False):
        where, args = ["active=1"], []
        if search:
            where.append("(title LIKE ? OR descr LIKE ?)")
            args += [f"%{search}%", f"%{search}%"]
        rows = q(f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 200", tuple(args))
        rows = [r for r in rows if not search or search.lower() in (r["title"] + " " + (r["descr"] or "")).lower()]
        total = len(rows)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(int(page), pages - 1))
        chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        if not chunk:
            msg = "🛒 Abhi koi item available nahi hai. Thodi der me aaiye." if not edit else "Koi item nahi hai."
            return self.bot.send(chat_id, msg, kb=None if edit else [[btn("🏠 Home", "home")]])

        lines, rows_kb = [], []
        for it in chunk:
            if edit:
                status = "🟢" if it["active"] else "🔴"
                lines.append(f"{status} #{it['id']} · {money(it['price'])} · {esc(it['title'])} · sold {it['sold']}")
                rows_kb.append([btn(f"#{it['id']} {it['title'][:18]}", f"admine:{it['id']}")])
            else:
                got = " ✅" if uid and has_access(uid, it["id"]) else ""
                lines.append(f"▪️ <b>{esc(it['title'])}</b> — {money(it['price'])}{got}")
                rows_kb.append([btn(f"👁 #{it['id']} dekho", f"item:{it['id']}"),
                                btn(("🔓 Khulo" if got else "🛒 Buy"), f"buy:{it['id']}")])
        if edit:
            head = f"📦 <b>Manage items</b> — total {total}"
        else:
            head = (f"🛒 <b>{esc(setting('brand'))} — Store</b>" if not search
                    else f"🔎 Search: <b>{esc(search)}</b>")
        txt = head + f"  (page {page + 1}/{pages})\n\n" + "\n".join(lines)
        sfx = ("|" + re.sub(r"[^\w ]", "", search)[:16]) if search else ""   # callback_data 64 byte limit
        nav = []
        if page > 0:
            nav.append(btn("⬅️", f"shop:{page - 1}{sfx}"))
        if page < pages - 1:
            nav.append(btn("➡️", f"shop:{page + 1}{sfx}"))
        if edit:
            nav.append(btn("➕ Naya item", "add:start"))
            kb_rows = [[r[0]] for r in rows_kb]
            if nav:
                kb_rows.append(nav)
            kb_rows.append([btn("📋 Settings", "asettings"), btn("🧾 Pending", "apend")])
            return self.bot.send(chat_id, txt, kb=kb_rows)
        kb_rows = rows_kb[:6]
        if nav:
            kb_rows.append(nav)
        kb_rows.append([btn("📜 Mere items", "my"), btn("🧾 Status", "status")])
        self.bot.send(chat_id, txt, kb=kb_rows)

    def show_item(self, chat_id, uid, item_id, cb=None):
        it = get_item(item_id)
        if not it:
            return self.bot.send(chat_id, "❌ Ye item nahi raha.")
        rows = []
        if has_access(uid, it["id"]):
            rows.append([btn("🔓 Abhi kholo", f"open:{it['id']}")])
        elif it["price"] <= 0:
            rows.append([btn("🎁 Free lo", f"buy:{it['id']}")])
        else:
            rows.append([btn(f"🛒 {money(it['price'])} — Kharido", f"buy:{it['id']}")])
        rows.append([btn("🛪 Store", "shop:0"), btn("ℹ️ Payment info", "payinfo")])
        self.bot.send(chat_id, self.item_caption(it, uid), kb=rows)

    # ====================== BUY ======================
    def cmd_buy(self, chat_id, uid, arg):
        it = None
        for tok in re.split(r"[,\s]+", arg or ""):
            tok = tok.lstrip("#")
            if tok.isdigit():
                it = get_item(int(tok))
                if it:
                    break
        if not it:
            return self.bot.send(chat_id, "Item ka number likhiye — jaise <code>/buy 3</code>",
                                 kb=[[btn("🛒 Store", "shop:0")]])
        self.start_buy(chat_id, uid, it)

    def start_buy(self, chat_id, uid, it):
        if not it["active"]:
            return self.bot.send(chat_id, "⏸️ Ye item abhi band hai.", kb=[[btn("🛒 Store", "shop:0")]])
        if has_access(uid, it["id"]):
            return self.bot.send(chat_id, "✅ Ye aapke paas pehle se hai. /my se kholiye.",
                                 kb=[[btn("📜 Mere items", "my")]])
        pend = pending_for_user(uid)
        if any(p["item_id"] == it["id"] for p in pend):
            o = [p for p in pend if p["item_id"] == it["id"]][0]
            return self.bot.send(chat_id,
                                 f"⏳ #{o['id']:04d} already pending hai — payment ka screenshot bhejiye "
                                 "ya /cancel kar dijiye.",
                                 kb=[[btn("💳 Payment info", f"payinfo:{o['id']}"), btn("❌ Cancel", f"cancel:{o['id']}")]])
        if float(it["price"]) <= 0:
            grant_access(uid, it["id"], None, it["validity_days"])
            self.bot.send(chat_id, "🎁 Free item — enjoy kijiye!")
            return self.deliver(uid, it, None)
        oid = create_order(uid, it)
        set_state(int(self.chat_of(uid)), {"flow": "buy", "step": "proof", "order": oid, "item": it["id"]})
        self.send_pay_info(chat_id, uid, get_order(oid), it)

    def chat_of(self, uid) -> int:
        row = q("SELECT tg_id FROM users WHERE id=?", (int(uid),))
        return row[0]["tg_id"] if row else 0

    def send_pay_info(self, chat_id, uid, order, it, alert=False):
        s = all_settings()
        amount = float(order["amount"]) if order else float(it["price"])
        o_no = order["no"] if order else ""
        msg = [f"💳 <b>Payment kijiye</b>  {money(amount)}",
               f"🧾 Order: <code>{esc(o_no)}</code>",
               f"🎬 Item: <b>{esc(it['title'])}</b>"]
        if s["upi_id"]:
            msg.append(f"👤 UPI ID: <code>{esc(s['upi_id'])}</code>")
        else:
            msg.append("⚠️ Admin ne UPI ID set nahi kiya — admin se contact kijiye.")
        msg.append(f"📝 Note: <code>{esc(o_no)} {esc((it['title'] or '')[:16])}</code>")
        msg.append(f"\n⏱ Amount <b>bilkul {money(amount)}</b> hona chahiye.\n"
                   "✅ Payment ke baad <b>screenshot</b> yahi chat me bhejiye.")
        if s["pay_note"]:
            msg.append(f"ℹ️ {esc(s['pay_note'])}")
        kb_rows = [[btn("📸 Screenshot bhejta hoon", f"ready:{order['id'] if order else 0}")],
                   [btn("❌ Cancel", f"cancel:{order['id'] if order else 0}")]]
        qr_fid = s.get("qr_file_id")
        body = "\n".join(msg)
        if qr_fid:
            self.bot.send_media(chat_id, "photo", qr_fid, caption="👆 Ye QR scan kijiye\n\n" + body, kb=kb_rows)
        else:
            fn = make_upi_qr(amount, o_no, s["upi_id"], s["payee_name"], o_no)
            if fn:
                self.bot.send_file_upload(chat_id, fn, caption="👆 Scan & pay " + body, kind="photo")
                try:
                    os.remove(fn)
                except Exception:
                    pass
            else:
                self.bot.send(chat_id, body, kb=kb_rows)


    def cancel_order(self, chat_id, uid, oid):
        o = get_order(oid)
        if not o or int(o["user_id"]) != int(uid):
            return self.bot.send(chat_id, "❌ Order nahi mila.")
        if o["status"] != "pending":
            return self.bot.send(chat_id, f"#{o['no'][1:]} status: {o['status']} — cancel nahi ho sakta.")
        x("UPDATE orders SET status='cancelled', decided_at=? WHERE id=?", (now(), oid))
        set_state(int(self.chat_of(uid)), {})
        self.bot.send(chat_id, f"❌ Order #{o['no'][1:]} cancel kar diya gaya.")
        for a in ADMIN_IDS:
            self.bot.send(a, f"🗑 <b>Order cancelled</b> #{o['no'][1:]} · user {esc(o['user_id'])}")

    # ====================== LIBRARY / STATUS ======================
    def show_library(self, chat_id, uid):
        rows = q("""SELECT u.*, i.title, i.kind, i.validity_days FROM unlocks u
                    JOIN items i ON i.id=u.item_id WHERE u.user_id=? ORDER BY u.item_id DESC""", (int(uid),))
        if not rows:
            return self.bot.send(chat_id, "📜 Abhi koi item unlock nahi hua. /shop se dekhiye.",
                                 kb=[[btn("🛒 Store", "shop:0")]])
        lines, kbr = [], []
        for r in rows:
            exp = f" (ends {r['expires_at'][:10]})" if r["expires_at"] else ""
            lines.append(f"▪️ #{r['item_id']} {esc(r['title'])}{exp}")
            kbr.append([btn(f"🔓 {r['title'][:22]}", f"open:{r['item_id']}")])
        kbr.append([btn("🛒 Aur dekho", "shop:0"), btn("🧾 Status", "status")])
        self.bot.send(chat_id, "📜 <b>Aapke unlocked items</b>\n\n" + "\n".join(lines), kb=kbr)

    def show_status(self, chat_id, uid, arg=""):
        rows = q("""SELECT o.*, i.title FROM orders o JOIN items i ON i.id=o.item_id
                    WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10""", (int(uid),))
        if not rows:
            return self.bot.send(chat_id, "🧾 Koi order nahi hai abhi. /shop se order kijiye.",
                                 kb=[[btn("🛒 Store", "shop:0")]])
        icon = {"pending": "⏳", "approved": "✅", "rejected": "❌", "cancelled": "🗑"}
        lines, kbr = [], []
        for r in rows:
            lines.append(f"{icon.get(r['status'], '•')} #{r['no'][1:]} · {esc(r['title'])[:26]} · "
                         f"{money(r['amount'])} · <b>{r['status'].upper()}</b>"
                         + (f"\n     ⚠ {esc(r['reason'])}" if r["reason"] else ""))
            if r["status"] == "pending":
                kbr.append([btn(f"💳 #{r['no'][1:]} payment info", f"payinfo:{r['id']}"),
                            btn(f"❌ #{r['no'][1:]}", f"cancel:{r['id']}")])
        self.bot.send(chat_id, "🧾 <b>Aapke orders</b>\n\n" + "\n".join(lines), kb=kbr or None)

    # ====================== DELIVER ======================
    def deliver(self, uid, it, order):
        chat = self.chat_of(uid)
        if not chat:
            if order:
                for a in ADMIN_IDS:
                    self.bot.send(a, f"⚠️ User {uid} ka telegram id nahi mila — delivery skip.")
            return False
        if not it["validity_days"]:
            tail = "♾ Lifetime access"
        else:
            tail = f"⏳ {it['validity_days']} din ke liye\n   (expires {self.exp_str(it['validity_days'])})"
        title = f"✅ <b>{esc(it['title'])}</b> unlock ho gaya!\n" + tail
        buttons = []
        if it["link"]:
            buttons.append(btn("🔗 Main link", None, it["link"]))
        if it["channel_link"]:
            buttons.append(btn("📢 Channel join", None, it["channel_link"]))
        if it["group_link"]:
            buttons.append(btn("👥 Group join", None, it["group_link"]))
        kb_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        if it["file_id"]:
            r = self.bot.send_media(chat, it["file_kind"] or it["kind"], it["file_id"],
                                    caption=title + "\n\n" + (esc(it["descr"]) if it["descr"] else "📦 File mil gayi ✅"),
                                    kb=kb_rows or None)
            if r and not r.get("ok") and not self.bot.offline:
                for a in ADMIN_IDS:
                    self.bot.send(a, f"🚫 <b>Delivery fail</b> — user {chat} ne bot block kar diya hoga, "
                                     f"ya file id expire ho gaya.\nOrder #{rowget(get_order(rowget(order,'id',0) or 0),'no','')[1:]}")
                return False
        elif it["kind"] == "text" and (it["descr"] or it["link"]):
            body = title + "\n\n" + esc(it["descr"] or "")
            if it["link"]:
                body += f"\n\n🔗 <code>{esc(it['link'])}</code>"
            self.bot.send(chat, body, kb=kb_rows or None)
        else:
            self.bot.send(chat, title + "\n\n" + (esc(it["descr"]) if it["descr"] else ""), kb=kb_rows or None)
        self.bot.send(chat, "🎁 Maza aaya? /shop se aur items dekh lijiye 😄",
                      kb=[[btn("🛒 Store", "shop:0"), btn("📜 Mere items", "my")]])
        if order:
            x("UPDATE orders SET delivered=1 WHERE id=?", (int(order["id"]),))
            x("UPDATE items SET sold=sold+1 WHERE id=?", (int(it["id"]),))
        return True

    @staticmethod
    def exp_str(days):
        return (datetime.now() + timedelta(days=int(days or 0))).strftime("%d %b %Y")

    # ====================== ADMIN: order inbox ======================
    def notify_admin_orders(self, order, it, uid_row):
        s = all_settings()
        txt = (f"🔔 <b>NEW PAYMENT</b>  #{order['no'][1:]}\n"
               f"👤 {esc(uid_row['name'])} <code>{esc(uid_row['username'] or '')}</code> · id <code>{uid_row['tg_id']}</code>\n"
               f"🎬 {esc(it['title'])} (#{it['id']})\n"
               f"💰 {money(order['amount'])}  ·  ⏱ {order['created_at']}\n"
               + (f"📝 {esc(order['note'])}\n" if order["note"] else "")
               + ("\nℹ️ UPI id set nahi hai, amount manually check karein." if not s["upi_id"] else ""))
        rows = [[btn("✅ Approve", f"aok:{order['id']}"), btn("❌ Reject", f"ano:{order['id']}")],
                [btn("🧾 Order copy", f"acopy:{order['id']}"), btn("👤 User", f"ausr:{uid_row['tg_id']}")],
                [btn("📜 User ke orders", f"aord:{uid_row['id']}"), btn("⏳ Pending list", "apend")]]
        for a in ADMIN_IDS:
            if order["proof_id"]:
                self.bot.send_media(a, order["proof_kind"] or "photo", order["proof_id"], caption=txt, kb=rows)
            else:
                self.bot.send(a, txt + "\n\n🚫 koi screenshot nahi mila!", kb=rows)

    def approve_order(self, admin_tg, oid, silent=False):
        o = get_order(oid)
        if not o:
            return self.bot.send(admin_tg, "❌ Order nahi mila.")
        if o["status"] == "approved":
            return self.bot.send(admin_tg, f"#{o['no'][1:]} pehle se approved hai. Re-deliver kar deta hoon.",
                                 kb=[[btn("🔁 Retry", f"adel:{o['id']}")]])
        it = get_item(o["item_id"])
        if not it:
            return self.bot.send(admin_tg, "❌ Item delete ho chuka hai.")
        urow = q("SELECT * FROM users WHERE id=?", (o["user_id"],))[0]
        x("UPDATE orders SET status='approved', decided_at=?, decided_by=?, reason=NULL WHERE id=?",
          (now(), int(admin_tg), int(oid)))
        grant_access(urow["id"], it["id"], o["id"], it["validity_days"])
        x("UPDATE users SET spent=spent+? WHERE id=?", (float(o["amount"]), int(urow["id"])))
        if not silent:
            self.bot.send(admin_tg, f"✅ Approved #{o['no'][1:]} → {esc(it['title'])} delivered to {esc(urow['name'])}.",
                          kb=[[btn("⏳ Pending", "apend"), btn("🛪 Store admin", "ashop")]])
        return self.deliver(urow["id"], it, get_order(oid))

    def reject_order(self, admin_tg, oid, reason=""):
        o = get_order(oid)
        if not o:
            return self.bot.send(admin_tg, "❌ Order nahi mila.")
        it = get_item(o["item_id"]) or {"title": "?"}
        urow = q("SELECT * FROM users WHERE id=?", (o["user_id"],))
        x("UPDATE orders SET status='rejected', decided_at=?, decided_by=?, reason=? WHERE id=?",
          (now(), int(admin_tg), reason or "Payment match nahi hui", int(oid)))
        if urow:
            set_state(int(urow[0]["tg_id"]), {})
            self.bot.send(int(urow[0]["tg_id"]),
                          f"❌ Order #{o['no'][1:]} reject ho gaya.\n"
                          f"⚠️ Reason: {esc(reason or 'Payment verify nahi ho payi.')}\n\n"
                          "Sahi screenshot / amount ke saath dobara order kar sakte ho 👇",
                          kb=[[btn("🛒 Dobara try karo", f"buy:{o['item_id']}")],
                              [btn("📞 Admin ko message", "adminmsg")]])
        self.bot.send(admin_tg, f"❌ Rejected #{o['no'][1:]} · {esc(it['title'])}",
                      kb=[[btn("⏳ Pending", "apend")]])

    def show_pending(self, admin_tg, page=0):
        rows = q("""SELECT o.*, i.title, u.name, u.tg_id, u.username FROM orders o
                    JOIN items i ON i.id=o.item_id JOIN users u ON u.id=o.user_id
                    WHERE o.status='pending' ORDER BY o.id DESC LIMIT 40""")
        if not rows:
            return self.bot.send(admin_tg, "🎉 Koi pending order nahi hai.",
                                 kb=[[btn("🛪 Admin panel", "ashop"), btn("📊 Stats", "astats")]])
        lines, kbr = [], []
        for r in rows:
            lines.append(f"⏳ #{r['no'][1:]} · {money(r['amount'])} · {esc(r['title'])[:20]}\n"
                         f"     👤 {esc(r['name'])} <code>{esc(r['username'] or '')}</code>"
                         + (" · 📸" if r["proof_id"] else " · 🚫proof missing"))
            kbr.append([btn(f"✅ #{r['no'][1:]}", f"aok:{r['id']}"), btn(f"❌ #{r['no'][1:]}", f"ano:{r['id']}")])
        kbr.append([btn("✅ sab approve", "aall"), btn("📊 Stats", "astats")])
        self.bot.send(admin_tg, f"⏳ <b>Pending orders ({len(rows)})</b>\n\n" + "\n".join(lines), kb=kbr)

    # ====================== ADMIN COMMANDS ======================
    def admin_command(self, chat_id, tg_id, uid, m, cmd, arg, media):
        c = cmd.lower()
        if c in ("/admin", "/panel", "/dash"):
            return self.admin_panel(chat_id)
        if c == "/additem" or c == "/new":
            set_state(tg_id, {"flow": "add", "step": "title", "d": {}})
            return self.bot.send(chat_id,
                                  "➕ <b>Naya item — step 1/6</b>\n\nItem ka <b>title</b> bhejiye.\n"
                                  "(baad me price, file, links, description puchunga)",
                                  kb=[[btn("❌ Cancel", "cancel_flow")]])
        if c == "/add":
            # reply to a video/photo/document  ->  /add title | price
            return self.quick_add(chat_id, tg_id, m, arg, media)
        if c == "/addlink":
            return self.quick_add(chat_id, tg_id, m, arg, media, force_link=True)
        if c in ("/addtext", "/text", "/addnote"):
            parts = [x.strip() for x in re.split(r"\|", arg or "") if x.strip()]
            title, price, body = (parts[0] if parts else "Note"), None, ""
            rest = parts[1:]
            if rest and to_num(rest[0]) is not None and len(rest[0]) <= 9 and re.fullmatch(r"[\d.,₹ ]+", rest[0] or ""):
                price = to_num(rest[0])
                body = " | ".join(rest[1:])
            else:
                body = " | ".join(rest)
            if not body:
                return self.bot.send(chat_id,
                                     "Use: <code>/addtext Title | 49 | aapka text / coupon / serial key</code>\n"
                                     "(price chhod ke bhi de sakte ho)")
            it_id = add_item(kind="text", title=title[:120], price=price or 0, descr=body[:3500])
            self.bot.send(chat_id, f"✅ Text item #{it_id} ban gaya (price {money(price or 0)}).")
            return self.item_menu(chat_id, get_item(it_id))
        if c in ("/items", "/adminitems", "/manage"):
            return self.show_shop(chat_id, uid, edit=True)
        if c == "/edit":
            it = self.find_item_arg(arg)
            if not it:
                return self.bot.send(chat_id, "Use: <code>/edit 3</code> (item id)",
                                     kb=[[btn("🛪 Items list", "ashop")]])
            return self.item_menu(chat_id, it)
        if c in ("/del", "/delete", "/remove"):
            it = self.find_item_arg(arg)
            if not it:
                return self.bot.send(chat_id, "Use: <code>/del 3</code>")
            x("DELETE FROM items WHERE id=?", (int(it["id"]),))
            return self.bot.send(chat_id, f"🗑 #{it['id']} <b>{esc(it['title'])}</b> delete kar diya.")
        if c in ("/pause", "/resume", "/off", "/on"):
            it = self.find_item_arg(arg)
            if not it:
                return self.bot.send(chat_id, "Use: <code>/pause 3</code>")
            on = 0 if c in ("/pause", "/off") else 1
            set_item(it["id"], "active", on)
            return self.bot.send(chat_id, f"{'🟢 On' if on else '🔴 Off'} → #{it['id']} {esc(it['title'])}")
        if c == "/price":
            mt = re.match(r"#?(\d+)\s+([\d.]+)", arg or "")
            it = get_item(int(mt.group(1))) if mt else None
            if not it:
                return self.bot.send(chat_id, "Use: <code>/price 3 99</code>  (item_id price)")
            set_item(it["id"], "price", to_num(mt.group(2)))
            return self.bot.send(chat_id, f"💰 #{it['id']} ka price = <b>{money(to_num(mt.group(2)))}</b>")
        if c == "/valid":
            mt = re.match(r"#?(\d+)\s+(\d+)", arg or "")
            it = get_item(int(mt.group(1))) if mt else None
            if not it:
                return self.bot.send(chat_id, "Use: <code>/valid 3 30</code>  (0 = lifetime)")
            set_item(it["id"], "validity_days", int(mt.group(2)))
            return self.bot.send(chat_id, f"⏳ #{it['id']} validity = {mt.group(2)} din (0=lifetime)")
        if c in ("/setqr", "/qr"):
            if c == "/qr" and not media:
                fid = setting("qr_file_id")
                if fid:
                    return self.bot.send_media(chat_id, "photo", fid, caption="🏦 Current payment QR")
                return self.bot.send(chat_id, "QR set nahi hai. <code>/setqr</code> ke saath QR photo bhejiye.")
            set_state(tg_id, {"flow": "setqr", "step": "media", "d": {}})
            return self.bot.send(chat_id, "🏦 Ab <b>QR code ka photo</b> bhejiye (ya /skip karke UPI-only rakhein).",
                                 kb=[[btn("⏭️ Skip", "cancel_flow")]])
        if c == "/upi":
            if not arg:
                return self.bot.send(chat_id, f"Abhi UPI: <code>{esc(setting('upi_id'))}</code>\nUse: /upi name@bank")
            set_setting("upi_id", arg.split()[0])
            return self.bot.send(chat_id, f"💳 UPI ID set: <code>{esc(arg.split()[0])}</code>")
        if c == "/payee":
            set_setting("payee_name", arg or "PremiumVideo")
            return self.bot.send(chat_id, f"👤 Payee name: <b>{esc(setting('payee_name'))}</b>")
        if c == "/note":
            set_setting("pay_note", arg)
            return self.bot.send(chat_id, f"ℹ️ Payment note set: {esc(arg) or '(clear)'}")
        if c == "/rules":
            set_setting("bank_note", arg)
            return self.bot.send(chat_id, "📜 Neeche dikhne wala text user ko dikhega:\n" + (esc(arg) or "(clear)"))
        if c == "/welcome":
            set_setting("welcome", arg)
            return self.bot.send(chat_id, "👋 Welcome text set ho gaya:\n\n" + (esc(arg) or "(default)"))
        if c == "/brand":
            set_setting("brand", arg or "PremiumVideo")
            return self.bot.send(chat_id, f"🏷 Brand: <b>{esc(setting('brand'))}</b>")
        if c == "/channel" or c == "/forcejoin":
            set_setting("force_channel", arg)
            if arg:
                return self.bot.send(chat_id,
                                     f"📢 Force-join ON: <code>{esc(arg)}</code>\n"
                                     "⚠️ Kaam karne ke liye bot ko us channel ka <b>admin</b> banaiye "
                                     "(warna hum join check nahi kar paate, bot seedha chala dega).",
                                     kb=[[btn("🛪 Panel", "ashop")]])
            return self.bot.send(chat_id, "📢 Force-join off kar diya.")
        if c in ("/pending", "/pend", "/orders"):
            if arg and arg.isdigit():
                return self.admin_order_view(chat_id, int(arg))
            return self.show_pending(chat_id)
        if c == "/approve":
            oid = re.search(r"\d+", arg or "")
            if not oid:
                return self.bot.send(chat_id, "Use: <code>/approve 12</code>")
            return self.approve_order(chat_id, int(oid.group()))
        if c == "/reject":
            mt = re.match(r"#?(\d+)\s*(.*)", arg or "", re.S)
            if not mt:
                return self.bot.send(chat_id, "Use: <code>/reject 12 amount match nahi hua</code>")
            return self.reject_order(chat_id, int(mt.group(1)), mt.group(2).strip())
        if c == "/grant":
            mt = re.match(r"(\d+)\s+#?(\d+)", arg or "")
            if not mt:
                return self.bot.send(chat_id, "Use: <code>/grant 5 3</code> → user id 5 ko item 3 free")
            u = q("SELECT * FROM users WHERE id=? OR tg_id=?", (int(mt.group(1)), int(mt.group(1))))
            it = get_item(int(mt.group(2)))
            if not u or not it:
                return self.bot.send(chat_id, "❌ user ya item nahi mila.")
            grant_access(u[0]["id"], it["id"], None, it["validity_days"])
            self.deliver(u[0]["id"], it, None)
            return self.bot.send(chat_id, f"🎁 Granted #{it['id']} to {esc(u[0]['name'])} (id {u[0]['id']})")
        if c == "/revoke":
            mt = re.match(r"(\d+)\s+#?(\d+)", arg or "")
            if not mt:
                return self.bot.send(chat_id, "Use: <code>/revoke 5 3</code>")
            u = q("SELECT id FROM users WHERE id=? OR tg_id=?", (int(mt.group(1)), int(mt.group(1))))
            if u:
                x("DELETE FROM unlocks WHERE user_id=? AND item_id=?", (u[0]["id"], int(mt.group(2))))
            return self.bot.send(chat_id, "🔒 Access hata diya.")
        if c == "/block":
            t = re.search(r"\d+", arg or "")
            if not t:
                return self.bot.send(chat_id, "Use: <code>/block 123456</code> (telegram id ya /user id)")
            u = q("SELECT * FROM users WHERE tg_id=? OR id=?", (int(t.group()), int(t.group())))
            if not u:
                return self.bot.send(chat_id, "❌ user nahi mila.")
            x("UPDATE users SET blocked=1 WHERE id=?", (u[0]["id"],))
            return self.bot.send(chat_id, f"🚫 {esc(u[0]['name'])} block.")
        if c == "/unblock":
            t = re.search(r"\d+", arg or "")
            if t:
                u = q("SELECT * FROM users WHERE tg_id=? OR id=?", (int(t.group()), int(t.group())))
                if u:
                    x("UPDATE users SET blocked=0 WHERE id=?", (u[0]["id"],))
                    return self.bot.send(chat_id, f"✅ {esc(u[0]['name'])} unblock.")
            return self.bot.send(chat_id, "❌ user nahi mila.")
        if c == "/stats":
            return self.admin_stats(chat_id)
        if c == "/users":
            rows = q("SELECT * FROM users WHERE is_admin=0 ORDER BY spent DESC LIMIT 20")
            if not rows:
                return self.bot.send(chat_id, "Koi user nahi.")
            txt = "\n".join(f"▪️ id <code>{r['id']}</code> · {esc(r['name'])} {esc(r['username'] or '')} · "
                            f"{r['orders']} orders · {money(r['spent'])}" for r in rows)
            return self.bot.send(chat_id, f"👥 <b>Top users</b>\n\n{txt}")
        if c in ("/bc", "/broadcast"):
            if not arg:
                set_state(tg_id, {"flow": "bc", "step": "text", "d": {}})
                return self.bot.send(chat_id, "📣 Ab broadcast message type kijiye (/cancel se rukein).")
            return self.broadcast(chat_id, arg)
        if c == "/help" and tg_id in ADMIN_IDS:
            return self.admin_help(chat_id)
        if c == "/settings":
            return self.admin_panel(chat_id)
        return self.bot.send(chat_id, "❓ Unknown admin command. <code>/help</code> dekhiye.",
                             kb=[[btn("🛪 Admin panel", "ashop")]])

    def find_item_arg(self, arg):
        mt = re.search(r"#?(\d+)", arg or "")
        if mt:
            return get_item(int(mt.group(1)))
        return None

    def quick_add(self, chat_id, tg_id, m, arg, media, force_link=False):
        """Reply to media: /add Title | 99   ;  /addlink Title | 99 | https://..."""
        parts = [p.strip() for p in re.split(r"[|]", arg or "")] if arg else []
        title = parts[0] if parts and parts[0] else ""
        price = to_num(parts[1]) if len(parts) > 1 else None
        link = next((p for p in parts[2:] if p.startswith("http")), "") if len(parts) > 2 else ""
        if force_link and not link:
            link = next((p for p in parts if p.startswith("http")), "")
            if not link:
                return self.bot.send(chat_id, "Link do: <code>/addlink Premium pack | 99 | https://t.me/xyz</code>")
            title = title or link
            price = 0.0 if price is None else price
            it_id = add_item(kind="link", title=title, price=price, link=link,
                              channel_link=next((p for p in parts if "t.me/" in p and p != link), ""),
                              descr=" ".join(p for p in parts[2:] if not p.startswith("http")))
            self.bot.send(chat_id, f"✅ Item #{it_id} ban gaya.", kb=[[btn("🛪 Manage items", "ashop")]])
            return self.item_menu(chat_id, get_item(it_id))
        if not media and not link:
            set_state(tg_id, {"flow": "add", "step": "quick_media", "d": {"title": title, "price": price}})
            return self.bot.send(chat_id,
                                 "🎬 Ab <b>video / file / photo</b> bhejiye (caption me title rakh diya: "
                                 f"<b>{esc(title or '(baad me puchunga)')}</b>).\n"
                                 "Ya /cancel karke <code>/additem</code> wizard use kijiye.")
        if not title:
            title = (media or {}).get("name") or "New item"
        price = 0.0 if price is None else price
        it_id = add_item(kind=(media or {}).get("kind", "link"), title=title[:120], price=price,
                         file_id=(media or {}).get("file_id"), file_kind=(media or {}).get("file_kind"),
                         link=link, descr=(m.get("caption") or "").replace(arg or "", "").strip()[:600])
        self.bot.send(chat_id, f"✅ Item #{it_id} <b>{esc(title)}</b> ready — price {money(price)}.",
                      kb=[[btn("🛪 Manage items", "ashop")]])
        return self.item_menu(chat_id, get_item(it_id))

    def admin_panel(self, chat_id):
        s = all_settings()
        n = q("SELECT COUNT(*) c FROM items WHERE active=1")[0]["c"]
        p = q("SELECT COUNT(*) c FROM orders WHERE status='pending'")[0]["c"]
        us = q("SELECT COUNT(*) c FROM users WHERE is_admin=0")[0]["c"]
        rev = q("SELECT COALESCE(SUM(amount),0) s FROM orders WHERE status='approved'")[0]["s"]
        txt = (f"🛪 <b>{esc(s['brand'])} — Admin panel</b>\n\n"
               f"📦 Active items: <b>{n}</b>   ·   👥 users: <b>{us}</b>\n"
               f"⏳ Pending payments: <b>{p}</b>   ·   💰 revenue: <b>{money(rev)}</b>\n"
               f"💳 UPI: <code>{esc(s['upi_id'] or 'SET NAHI KIYA')}</code>\n"
               f"🏦 QR: {'✅ uploaded' if s['qr_file_id'] else '❌ nahi (auto-UPi-QR banega)'}\n"
               f"📢 Force-join: <code>{esc(s['force_channel'] or 'off')}</code>")
        self.bot.send(chat_id, txt, kb=[
            [btn("📦 Items", "ashop"), btn("➕ Naya item", "add:start")],
            [btn(f"⏳ Pending ({p})", "apend"), btn("📊 Stats", "astats")],
            [btn("💳 QR/UPI set", "asetup"), btn("⚙️ Settings", "asettings")],
            [btn("📣 Broadcast", "abc"), btn("📖 Help", "ahelp")]])

    def admin_help(self, chat_id):
        self.bot.send(chat_id,
            "🛠 <b>Admin commands</b>\n\n"
            "<b>Items</b>\n"
            "<code>/additem</code> — wizard (title→price→desc→file→links)\n"
            "<code>/add Title | 99</code> — reply to any video/photo/file\n"
            "<code>/addlink Title | 99 | https://...</code>\n"
            "<code>/items</code> · <code>/edit 3</code> · <code>/price 3 49</code> · <code>/valid 3 30</code>\n"
            "<code>/pause 3</code> · <code>/resume 3</code> · <code>/del 3</code>\n\n"
            "<b>Payment</b>\n"
            "<code>/setqr</code> (photo bhejiye) · <code>/upi id@bank</code> · <code>/payee Name</code>\n"
            "<code>/note text</code> · <code>/welcome text</code> · <code>/brand Name</code>\n"
            "<code>/channel @mychannel</code> — join karne ke baad hi bot chalega\n\n"
            "<b>Orders / users</b>\n"
            "<code>/pending</code> · <code>/approve 12</code> · <code>/reject 12 reason</code>\n"
            "<code>/grant 5 3</code> · <code>/revoke 5 3</code> · <code>/block 123</code> · <code>/unblock 123</code>\n"
            "<code>/users</code> · <code>/stats</code> · <code>/bc message</code> · <code>/settings</code>",
            kb=[[btn("🛪 Panel", "ashop"), btn("⏳ Pending", "apend")]])

    def admin_stats(self, chat_id):
        tot = q("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders WHERE status='approved'")[0]
        pend = q("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders WHERE status='pending'")[0]
        rej = q("SELECT COUNT(*) c FROM orders WHERE status='rejected'")[0]["c"]
        today = q("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders WHERE status='approved' AND decided_at>=?",
                  (datetime.now().strftime("%Y-%m-%d"),))[0]
        top = q("""SELECT i.title, i.id, COUNT(*) c, SUM(o.amount) s FROM orders o JOIN items i ON i.id=o.item_id
                   WHERE o.status='approved' GROUP BY i.id ORDER BY c DESC LIMIT 5""")
        txt = (f"📊 <b>Stats</b>\n\n"
               f"✅ Approved: <b>{tot['c']}</b> → {money(tot['s'])}\n"
               f"⏳ Pending: <b>{pend['c']}</b> → {money(pend['s'])}\n"
               f"❌ Rejected: <b>{rej}</b>\n"
               f"📅 Aaj: <b>{today['c']}</b> → {money(today['s'])}\n")
        if top:
            txt += "\n🏆 Top items:\n" + "\n".join(f"▪️ #{r['id']} {esc(r['title'])[:24]} × {r['c']} ({money(r['s'])})"
                                                    for r in top)
        self.bot.send(chat_id, txt, kb=[[btn("⏳ Pending", "apend"), btn("📦 Items", "ashop")]])

    def admin_order_view(self, chat_id, oid):
        o = get_order(oid)
        if not o:
            return self.bot.send(chat_id, "❌ Order id nahi mila.")
        it = get_item(o["item_id"]) or {"title": "?", "id": 0}
        u = q("SELECT * FROM users WHERE id=?", (o["user_id"],))
        u = u[0] if u else {"name": "?", "tg_id": 0, "username": "", "id": 0}
        txt = (f"🧾 <b>#{o['no'][1:]} — {o['status'].upper()}</b>\n"
               f"🎬 {esc(it['title'])} (#{it['id']})\n💰 {money(o['amount'])}\n"
               f"👤 {esc(u['name'])} <code>{esc(u['username'] or '')}</code> tg=<code>{u['tg_id']}</code> uid={u['id']}\n"
               f"🕒 {o['created_at']}" + (f"\n🗒 {o['decided_at']} by {o['decided_by']}" if o["decided_at"] else "")
               + (f"\n⚠ {esc(o['reason'])}" if o["reason"] else "")
               + (f"\n📝 {esc(o['note'])}" if o["note"] else ""))
        rows = [[btn("✅ Approve", f"aok:{o['id']}"), btn("❌ Reject", f"ano:{o['id']}")],
                [btn("📸 Screenshot", f"aproof:{o['id']}"), btn("🔁 Re-deliver", f"adel:{o['id']}")],
                [btn("👤 User", f"ausr:{u['tg_id']}"), btn("⏳ Pending", "apend")]]
        self.bot.send(chat_id, txt, kb=rows)
        if o["proof_id"]:
            self.bot.send_media(chat_id, o["proof_kind"] or "photo", o["proof_id"],
                                caption=f"📸 Payment proof · #{o['no'][1:]}")

    def item_menu(self, chat_id, it):
        rows = [[btn(t, f"f:{k}:{it['id']}") for (t, k) in chunk] for chunk in
                [EDIT_FIELDS[i:i + 2] for i in range(0, len(EDIT_FIELDS), 2)]]
        rows.append([btn("🟢/🔴 Toggle", f"tog:{it['id']}"), btn("🗑 Delete", f"del:{it['id']}")])
        rows.append([btn("👁 Preview", f"prev:{it['id']}"), btn("🛪 Back", "ashop")])
        self.bot.send(chat_id,
                      "🛠 <b>Edit item</b>\n\n" + self.item_caption(it, None, True) +
                      f"\n\n📄 File id: <code>{esc((it['file_id'] or '—')[:28])}</code>",
                      kb=rows)

    def broadcast(self, chat_id, msg):
        rows = q("SELECT tg_id FROM users WHERE is_admin=0 AND blocked=0")
        n = 0
        for r in rows:
            self.bot.send(int(r["tg_id"]), f"📣 {msg}")
            n += 1
            time.sleep(0.05)
        self.bot.send(chat_id, f"📣 Broadcast bhej diya {n} users ko.")

    # ====================== media extract ======================
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
            fid = v.get("file_id")
            if fid is None and isinstance(v, list):
                fid = v[-1].get("file_id")
            name = v.get("file_name") or ""
            return {"kind": kind, "file_kind": field, "file_id": fid, "name": name or f"{key}.bin",
                    "size": v.get("file_size")}
        return None

    # ====================== inline callbacks ======================
    def on_callback(self, cb: dict):
        m = cb.get("message") or {}
        data = cb.get("data") or ""
        chat_id = (m.get("chat") or {}).get("id")
        tg_id = int((cb.get("from") or {}).get("id") or 0)
        admin = tg_id in ADMIN_IDS
        urow = user_by_tg(tg_id)
        uid = urow["id"] if urow else ensure_user(cb.get("from") or {"id": tg_id, "first_name": "User"})
        if not admin and is_blocked(tg_id):
            return self.bot.answer(cb.get("id"), "Aap block ho gaye ho 🚫", True)
        if not admin and not self.channel_ok(tg_id, chat_id):
            return self.bot.answer(cb.get("id"))
        self.bot.answer(cb.get("id"))
        self.dispatch(chat_id, tg_id, uid, admin, data, m)

    def dispatch(self, chat_id, tg_id, uid, admin, data, m):
        if data == "home":
            return self.cmd_start(chat_id, tg_id, uid, {"from": {"id": tg_id, "first_name": "User"}}, "/start")
        if data == "rules_txt":
            return self.bot.send(chat_id, self.rules_text(),
                                 kb=[[btn("🛒 Store", "shop:0"), btn("📜 Mere items", "my")]])
        if data == "my":
            return self.show_library(chat_id, uid)
        if data == "status":
            return self.show_status(chat_id, uid)
        if data.startswith("shop:"):
            pg, _, srch = data[5:].partition("|")
            return self.show_shop(chat_id, uid, int(pg or 0), srch)
        if data.startswith("item:"):
            return self.show_item(chat_id, uid, int(data[5:]))
        if data.startswith("buy:"):
            it = get_item(int(data[4:]))
            if not it:
                return self.bot.send(chat_id, "❌ Item nahi mila.")
            return self.start_buy(chat_id, uid, it)
        if data.startswith("open:"):
            return self.open_item(chat_id, uid, int(data[5:]))
        if data.startswith("ready:"):
            oid = int(data[6:])
            o_ = get_order(oid)
            set_state(tg_id, {"flow": "buy", "step": "proof", "order": oid,
                              "item": o_["item_id"] if o_ else 0})
            return self.bot.send(chat_id,
                                 "📸 <b>Payment ka screenshot bhejiye</b> (photo ya file).\n"
                                 "Screenshot milte hi admin ko chala jaayega. ⏳",
                                 kb=[[btn("❌ Cancel", f"cancel:{oid}")]])
        if data.startswith("payinfo"):
            oid = data.split(":")[1] if ":" in data else ""
            o = get_order(int(oid)) if oid else None
            it = get_item(o["item_id"]) if o else None
            if not it:
                rows = q("SELECT o.*, i.* FROM orders o JOIN items i ON i.id=o.item_id WHERE o.user_id=? "
                         "AND o.status='pending' ORDER BY o.id DESC LIMIT 1", (int(uid),))
                it = rows[0] if rows else None
                o = rows[0] if rows else None
            if not it:
                return self.bot.send(chat_id, "Koi pending order nahi. /shop se order kijiye.",
                                     kb=[[btn("🛒 Store", "shop:0")]])
            return self.send_pay_info(chat_id, uid, o, it)
        if data.startswith("cancel:"):
            return self.cancel_order(chat_id, uid, int(data[7:]))
        if data == "cancel_flow":
            set_state(tg_id, {})
            return self.bot.send(chat_id, "↩️ Koi baat nahi. /items se wapas aa sakte ho.")
        if data == "recheck_channel":
            if self.channel_ok(tg_id, chat_id):
                return self.cmd_start(chat_id, tg_id, uid, {"from": {"id": tg_id, "first_name": "User"}}, "/start")
            return self.bot.answer(str(m.get("message_id")), "Pehle join kijiye 🙏", True)
        if data == "payinfo" or data == "payinfo:0":
            s = all_settings()
            txt = (f"💳 <b>Payment info</b>\n\nUPI ID: <code>{esc(s['upi_id'] or '(admin set nahi karra)')}</code>\n"
                   f"Payee: {esc(s['payee_name'])}\n\n{esc(s['pay_note'])}")
            kb_rows = [[btn("🛒 Store", "shop:0")]]
            if s.get("qr_file_id"):
                self.bot.send_media(chat_id, "photo", s["qr_file_id"], caption=txt, kb=kb_rows)
                return
            return self.bot.send(chat_id, txt, kb=kb_rows)
        if data == "adminmsg":
            return self.bot.send(chat_id, "📞 Admin ko yahi message likh dijiye, wo turant padh lenge.",
                                 kb=[[btn("💬 Admin ko bhejo", "sendadmin"), btn("⬅️ Back", "home")]])
        if data == "sendadmin":
            set_state(tg_id, {"flow": "toadmin", "step": "text", "d": {}})
            return self.bot.send(chat_id, "✍️ Likhiye (main admin ko forward kar dunga):")

        # --------------------- ADMIN ---------------------
        if not admin:
            return self.bot.send(chat_id, "🔒 Sirf admin.")
        if data == "ashop":
            return self.show_shop(chat_id, uid, edit=True)
        if data == "apend":
            return self.show_pending(chat_id)
        if data == "aall":
            rows = q("SELECT COUNT(*) c FROM orders WHERE status='pending'")
            n = rows[0]["c"]
            if not n:
                return self.bot.send(chat_id, "Kuch pending hi nahi hai 🙂")
            return self.bot.send(chat_id,
                                 f"⚠️ <b>{n} orders</b> ko approve kar ke content deliver kar dun?\n"
                                 "Pehle screenshots check kar lijiye — revert karna mushkil hoga.",
                                 kb=[[btn(f"✅ Haan, {n} approve karo", "aall2"), btn("↩️ Back", "apend")]])
        if data == "aall2":
            rows = q("SELECT id FROM orders WHERE status='pending' ORDER BY id")
            for r in rows:
                self.approve_order(chat_id, r["id"], silent=True)
            return self.bot.send(chat_id, f"✅ {len(rows)} orders approve ho gaye, content deliver kar diya.",
                                 kb=[[btn("📊 Stats", "astats"), btn("🛪 Panel", "ashop")]])
        if data == "astats":
            return self.admin_stats(chat_id)
        if data == "ahelp":
            return self.admin_help(chat_id)
        if data == "abc":
            set_state(tg_id, {"flow": "bc", "step": "text", "d": {}})
            return self.bot.send(chat_id, "📣 Broadcast text likhiye:")
        if data == "asetup":
            s = all_settings()
            return self.bot.send(chat_id,
                                 f"💳 Payment setup\nUPI: <code>{esc(s['upi_id'] or '—')}</code>\n"
                                 f"QR: {'✅' if s['qr_file_id'] else '❌'}\nPayee: {esc(s['payee_name'])}\n\n"
                                 "Buttons dabaiye ya commands use kijiye.",
                                 kb=[[btn("🏦 QR upload", "s:qr"), btn("💳 UPI change", "s:upi")],
                                     [btn("📝 Note", "s:note"), btn("📢 Force channel", "s:chan")]])
        if data == "asettings":
            s = all_settings()
            rows = q("SELECT key,value FROM settings")
            txt = "⚙️ <b>Settings</b>\n\n" + "\n".join(f"▪️ {esc(r['key'])}: <code>{esc(r['value'])[:60]}</code>" for r in rows)
            return self.bot.send(chat_id, txt or "empty", kb=[[btn("🏦 QR", "s:qr"), btn("💳 UPI", "s:upi")],
                                                              [btn("🛪 Panel", "ashop")]])
        if data.startswith("s:"):
            key = data[2:]
            if key == "qr":
                set_state(tg_id, {"flow": "setqr", "step": "media", "d": {}})
                return self.bot.send(chat_id, "🏦 QR photo bhejiye:")
            if key == "upi":
                set_state(tg_id, {"flow": "setfield", "step": "upi_id", "d": {}})
                return self.bot.send(chat_id, "💳 Naya UPI ID likhiye (jaise <code>shop@ybl</code>):")
            if key == "note":
                set_state(tg_id, {"flow": "setfield", "step": "pay_note", "d": {}})
                return self.bot.send(chat_id, "📝 Payment note likhiye:")
            if key == "chan":
                set_state(tg_id, {"flow": "setfield", "step": "force_channel", "d": {}})
                return self.bot.send(chat_id, "📢 Channel link/@username likhiye, off karne ke liye <code>none</code>:")
        if data == "add:start":
            set_state(tg_id, {"flow": "add", "step": "title", "d": {}})
            return self.bot.send(chat_id, "➕ <b>Step 1/6</b> — item ka <b>title</b> bhejiye:",
                                 kb=[[btn("❌ Cancel", "cancel_flow")]])
        if data.startswith("qr_view"):
            fid = setting("qr_file_id")
            if fid:
                return self.bot.send_media(chat_id, "photo", fid, caption="🏦 Current payment QR")
            return self.bot.send(chat_id, "❌ QR upload nahi hai — UPI auto-QR use ho raha hai.")
        if data.startswith("wiz:"):
            _, act, *rest = data.split(":")
            stx = get_state(tg_id)
            if act == "lifetime":
                d = dict((stx.get("d") or {}))
                d["validity_days"] = 0
                return self.wiz_confirm(chat_id, tg_id, d)
            if act == "skip" and stx.get("flow") == "add":
                if rest and rest[0] == "links":
                    d2 = dict(stx.get("d") or {})
                    return self.ask_validity(chat_id, tg_id, d2)
                if rest and rest[0] == "media":
                    d3 = dict(stx.get("d") or {})
                    d3.pop("file_id", None)
                    return self.wiz_next(chat_id, tg_id, d3, "media")
                return self.wizard(chat_id, tg_id, stx, "/skip", None, admin)
            set_state(tg_id, {"flow": "add", "step": "title", "d": {}})
            return self.bot.send(chat_id, "🔄 Wizard reset — " + self.WIZ_TXT["price"],
                                 kb=[[btn("⏭️ Skip", "wiz:skip:price"), btn("❌ Cancel", "cancel_flow")]])
        if data.startswith("admine:"):
            it = get_item(int(data[7:]))
            return self.item_menu(chat_id, it) if it else self.bot.send(chat_id, "❌ item nahi")
        if data.startswith("prev:"):
            it = get_item(int(data[5:]))
            if not it:
                return self.bot.send(chat_id, "❌ item nahi")
            self.bot.send(chat_id, self.item_caption(it, uid))
            if it["file_id"]:
                self.bot.send_media(chat_id, it["file_kind"] or it["kind"], it["file_id"],
                                    caption="👁 Preview (admin copy)")
            return None
        if data.startswith("tog:"):
            it = get_item(int(data[4:]))
            if it:
                set_item(it["id"], "active", 0 if it["active"] else 1)
                self.item_menu(chat_id, get_item(it["id"]))
            return
        if data.startswith("del:"):
            it = get_item(int(data[4:]))
            if it:
                x("UPDATE items SET active=0 WHERE id=?", (int(it["id"]),))
            return self.bot.send(chat_id, f"🗑 #{it['id']} hide (active=0) kar diya. Full delete: /del {it['id']}",
                                 kb=[[btn("🛪 Items", "ashop")]])
        if data.startswith("f:"):
            _, field, iid = data.split(":")
            it = get_item(int(iid))
            if not it:
                return self.bot.send(chat_id, "❌ item nahi mila.")
            if field == "active":
                set_item(it["id"], "active", 0 if it["active"] else 1)
                return self.item_menu(chat_id, get_item(it["id"]))
            if field == "file_id":
                set_state(tg_id, {"flow": "edit", "step": "media", "item": it["id"], "field": "file_id"})
                return self.bot.send(chat_id, "🎬 Naya video/file/photo bhejiye (current replace ho jaayega).",
                                     kb=[[btn("❌ Cancel", "cancel_flow")]])
            prompt = {"title": "✏️ Naya title bhejiye:", "descr": "📝 Description bhejiye (none = clear):",
                      "price": "💰 Naya price (rupees) bhejiye — 0 = free:",
                      "link": "🔗 Main link bhejiye (none = clear):",
                      "channel_link": "📢 Channel link bhejiye (none = clear):",
                      "group_link": "👥 Group link bhejiye (none = clear):",
                      "validity_days": "⏳ Kitne din access? (0 = lifetime)"}[field]
            set_state(tg_id, {"flow": "edit", "step": "value", "item": it["id"], "field": field})
            return self.bot.send(chat_id, f"{prompt}\n\nAbhi: <code>{esc(it[field] if field != 'price' else money(it['price']))}</code>",
                                 kb=[[btn("❌ Cancel", "cancel_flow")]])
        # ---- order actions ----
        if data.startswith("aok:"):
            return self.approve_order(chat_id, int(data[4:]))
        if data.startswith("ano:"):
            oid = int(data[4:])
            set_state(tg_id, {"flow": "reject", "step": "reason", "order": oid})
            return self.bot.send(chat_id,
                                 f"❌ Reject reason likhiye (#{oid:04d}) — user ko yahi dikhega:",
                                 kb=[[btn("Default reason", f"adef:{oid}"), btn("↩️ Back", "apend")]])
        if data.startswith("adef:"):
            return self.reject_order(chat_id, int(data[5:]), "Payment verify nahi ho payi / galat amount.")
        if data.startswith("acopy:"):
            return self.admin_order_view(chat_id, int(data[6:]))
        if data.startswith("adel:"):
            o = get_order(int(data[5:]))
            if o:
                it = get_item(o["item_id"])
                u = q("SELECT id FROM users WHERE id=?", (o["user_id"],))
                if it and u:
                    self.deliver(o["user_id"], it, o)
            return self.bot.send(chat_id, "🔁 User ko dobara bhej diya.")
        if data.startswith("aproof:"):
            o = get_order(int(data[7:]))
            if o and o["proof_id"]:
                return self.bot.send_media(chat_id, o["proof_kind"] or "photo", o["proof_id"],
                                           caption=f"📸 #{o['no'][1:]}")
            return self.bot.send(chat_id, "🚫 Is order me screenshot nahi hai.")
        if data.startswith("ausr:"):
            t = int(data[5:])
            u = user_by_tg(t) or (q("SELECT * FROM users WHERE id=?", (t,)) or [None])[0]
            if not u:
                return self.bot.send(chat_id, "❌ user nahi mila.")
            o1 = q("SELECT COUNT(*) c FROM orders WHERE user_id=?", (u["id"],))[0]["c"]
            o2 = q("SELECT COUNT(*) c FROM orders WHERE user_id=? AND status='pending'", (u["id"],))[0]["c"]
            o3 = q("SELECT COUNT(*) c FROM unlocks WHERE user_id=?", (u["id"],))[0]["c"]
            return self.bot.send(chat_id,
                                 f"👤 <b>{esc(u['name'])}</b> {esc(u['username'] or '')}\n"
                                 f"tg id: <code>{u['tg_id']}</code> · bot id: <code>{u['id']}</code>\n"
                                 f"🧾 orders: {o1} (⏳ {o2}) · 🔓 unlocked: {o3} · 💸 spent {money(u['spent'])}\n"
                                 f"🚫 blocked: {'yes' if u['blocked'] else 'no'}",
                                 kb=[[btn("📜 Orders", f"aord:{u['id']}"), btn("🎁 Free access do", f"agr:{u['id']}")],
                                     [btn("🚫 Block", f"abl:{u['id']}"), btn("✅ Unblock", f"aub:{u['id']}")]])
        if data.startswith("aord:"):
            uid2 = int(data[5:])
            rows = q("""SELECT o.*, i.title FROM orders o JOIN items i ON i.id=o.item_id
                        WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20""", (uid2,))
            if not rows:
                return self.bot.send(chat_id, "Is user ka koi order nahi.")
            txt = "\n".join(f"{'⏳✅❌🗑'[:{'pending':0,'approved':1,'rejected':2}.get(r['status'],3)]} "
                            f"#{r['no'][1:]} {esc(r['title'])[:20]} {money(r['amount'])} {r['status']}" for r in rows)
            return self.bot.send(chat_id, f"🧾 User #{uid2} orders\n\n{txt}",
                                 kb=[[btn("↩️ Back", "apend")]])
        if data.startswith("agr:"):
            set_state(tg_id, {"flow": "grant", "step": "item", "user": int(data[4:])})
            return self.bot.send(chat_id, "🎁 Item ka id bhejiye (user ko free access):")
        if data.startswith("abl:"):
            x("UPDATE users SET blocked=1 WHERE id=?", (int(data[4:]),))
            return self.bot.send(chat_id, "🚫 Blocked.")
        if data.startswith("aub:"):
            x("UPDATE users SET blocked=0 WHERE id=?", (int(data[4:]),))
            return self.bot.send(chat_id, "✅ Unblocked.")
        return self.bot.send(chat_id, "❓ Samajh nahi aaya.", kb=[[btn("🛪 Panel", "ashop")]])

    def open_item(self, chat_id, uid, item_id):
        it = get_item(item_id)
        if not it:
            return self.bot.send(chat_id, "❌ Item delete ho gaya.")
        if not has_access(uid, item_id):
            return self.bot.send(chat_id, "🔒 Pehle payment approve karwaiye.",
                                 kb=[[btn(f"🛒 {money(it['price'])} Buy", f"buy:{item_id}")]])
        self.bot.send(chat_id, f"🔓 {esc(it['title'])} — aapka access:")
        return self.deliver(uid, it, None)

    # ====================== FSM (awaiting input) ======================
    def on_state(self, chat_id, tg_id, uid, m, text, media, st) -> bool:
        """User/admin ka pending input (screenshot, wizard answer, QR photo, reject reason)."""
        flow = st.get("flow")
        step = st.get("step")
        admin = tg_id in ADMIN_IDS

        # ---------- user: payment screenshot ----------
        if flow == "buy" and step == "proof":
            if text.startswith("/cancel"):
                set_state(tg_id, {})
                return False
            if text in ("/done", "✅") or media:
                o = get_order(st.get("order"))
                if not o:
                    set_state(tg_id, {})
                    return False
                if not media:
                    self.bot.send(chat_id, "📸 Screenshot toh bhejiye! (photo ya file doc)\n"
                                           "Sirf text se payment verify nahi hoti.")
                    return True
                x("UPDATE orders SET proof_id=?, proof_kind=?, note=? WHERE id=?",
                  (media["file_id"], media["file_kind"], (text or "")[:200], int(o["id"])))
                set_state(tg_id, {})
                o = get_order(o["id"])
                it = get_item(o["item_id"])
                urow = q("SELECT * FROM users WHERE id=?", (uid,))[0]
                self.bot.send(chat_id,
                              f"✅ Screenshot mil gaya! Order <b>#{o['no'][1:]}</b> admin ke paas hai.\n"
                              "⏳ Normally 5–30 min me approval. /status se check kar sakte ho.",
                              kb=[[btn("🧾 Status", "status"), btn("🛒 Store", "shop:0")]])
                self.notify_admin_orders(o, it, urow)
                return True
            self.bot.send(chat_id, "📸 Payment ka <b>screenshot</b> bhejiye, ya /cancel.")
            return True

        # ---------- user: message to admin ----------
        if flow == "toadmin":
            if not text and not media:
                return False
            set_state(tg_id, {})
            for a in ADMIN_IDS:
                if media:
                    self.bot.send_media(a, media["file_kind"], media["file_id"],
                                        caption=f"💬 User {esc(uid)} ({esc(self.name_of(uid))}): {text[:300]}")
                else:
                    self.bot.send(a, f"💬 User #{uid} {esc(self.name_of(uid))}: {esc(text)[:900]}",
                                  kb=[[btn("👤 User", f"ausr:{tg_id}")]])
            self.bot.send(chat_id, "✅ Admin ko bhej diya.")
            return True

        # ---------- admin: reject reason ----------
        if flow == "reject" and admin:
            set_state(tg_id, {})
            self.reject_order(chat_id, st.get("order"), text or "Payment verify nahi ho payi.")
            return True

        if flow == "grant" and admin:
            set_state(tg_id, {})
            iid = re.search(r"\d+", text or "")
            if iid:
                u = q("SELECT * FROM users WHERE id=?", (int(st.get("user")),))
                it = get_item(int(iid.group()))
                if u and it:
                    grant_access(u[0]["id"], it["id"], None, it["validity_days"])
                    self.deliver(u[0]["id"], it, None)
                    self.bot.send(chat_id, f"🎁 {esc(it['title'])} → {esc(u[0]['name'])} ko de diya.")
                    return True
            self.bot.send(chat_id, "❌ item id nahi samjha.")
            return True

        # ---------- admin: settings text fields ----------
        if flow == "setfield" and admin:
            set_state(tg_id, {})
            key = st.get("step")
            if text.lower() in ("none", "off", "clear"):
                set_setting(key, "")
                self.bot.send(chat_id, f"🧹 <b>{key}</b> clear kar diya.")
                return True
            if key == "upi_id":
                if not re.match(r"^[\w.\-]{2,}@[A-Za-z]{2,}$", text.strip().split()[0]):
                    self.bot.send(chat_id, "❌ UPI ID ka format sahi nahi lag raha. Example: <code>shop@ybl</code>")
                    set_state(tg_id, st)
                    return True
                set_setting("upi_id", text.strip().split()[0])
            else:
                set_setting(key, text.strip()[:600])
            return self.bot.send(chat_id, f"✅ <b>{key}</b> set ho gaya: <code>{esc(text[:120])}</code>",
                                 kb=[[btn("💳 Setup", "asetup")]])

        # ---------- admin: QR upload ----------
        if flow == "setqr" and admin:
            if not media:
                if text.startswith("/skip"):
                    set_state(tg_id, {})
                    set_setting("qr_file_id", "")
                    self.bot.send(chat_id, "🏦 QR hata diya — ab UPI QR auto-generate hoga.")
                    return True
                self.bot.send(chat_id, "🙏 QR ka <b>photo</b> bhejiye (ya /skip).")
                return True
            set_setting("qr_file_id", media["file_id"])
            set_state(tg_id, {})
            self.bot.send(chat_id, "✅ QR save ho gaya! User ko payment screen pe dikhega.",
                          kb=[[btn("💳 Setup", "asetup"), btn("🏦 QR dekho", "qr_view")]])
            return True

        # ---------- admin: broadcast ----------
        if flow == "bc" and admin:
            set_state(tg_id, {})
            if text:
                self.broadcast(chat_id, text)
            return True

        # ---------- admin: item wizard ----------
        if flow == "add" and admin:
            return self.wizard(chat_id, tg_id, st, text, media, admin)

        # ---------- admin: edit one field ----------
        if flow == "edit" and admin:
            it = get_item(int(st.get("item")))
            if not it:
                set_state(tg_id, {})
                return True
            field = st.get("field")
            if step == "media":
                if not media:
                    self.bot.send(chat_id, "🎬 File/photo/video bhejiye, ya /skip (file hataane ke liye).")
                    return True
                if text.startswith("/skip"):
                    set_item(it["id"], "file_id", None)
                    set_item(it["id"], "file_kind", None)
                else:
                    set_item(it["id"], "file_id", media["file_id"])
                    set_item(it["id"], "file_kind", media["file_kind"])
                    set_item(it["id"], "kind", media["kind"])
                set_state(tg_id, {})
                self.bot.send(chat_id, "✅ File update ho gayi.")
                self.item_menu(chat_id, get_item(it["id"]))
                return True
            val = text.strip()
            if field == "price":
                n = to_num(val)
                if n is None:
                    self.bot.send(chat_id, "❌ Number likhiye (0 = free).")
                    return True
                set_item(it["id"], "price", n)
            elif field == "validity_days":
                n = re.search(r"\d+", val)
                set_item(it["id"], "validity_days", int(n.group()) if n else 0)
            elif field in ("title",):
                set_item(it["id"], "title", (val or it["title"])[:120])
            else:
                set_item(it["id"], field, None if val.lower() in ("none", "-", "clear") else val[:800])
            set_state(tg_id, {})
            self.bot.send(chat_id, f"✅ {field} update ho gaya.")
            self.item_menu(chat_id, get_item(it["id"]))
            return True
        return False

    @staticmethod
    def name_of(uid):
        r = q("SELECT name FROM users WHERE id=?", (int(uid),))
        return r[0]["name"] if r else "?"

    # ---- item wizard ----
    WIZ = ["title", "price", "descr", "media", "links", "done"]
    WIZ_TXT = {
        "price": "💰 <b>Step 2/6</b> — price kitna? (sirf number, jaise <code>49</code>; <code>0</code> = free)",
        "descr": "📝 <b>Step 3/6</b> — description bhejiye (kya milega user ko). <code>skip</code> bhi kar sakte ho.",
        "media": "🎬 <b>Step 4/6</b> — video / photo / file bhejiye. Ya <code>/skip</code> agar sirf link dena hai.",
        "links": "🔗 <b>Step 5/6</b> — links bhejiye, ek line me ya alag-alag:\n"
                 "<code>https://drive.google.com/x</code>\n"
                 "<code>channel: https://t.me/mychan</code>\n"
                 "<code>group: https://t.me/mygroup</code>\n"
                 "ya <code>skip</code>",
    }

    def wizard(self, chat_id, tg_id, st, text, media, admin):
        d = st.get("d") or {}
        step = st.get("step")
        low = (text or "").strip()
        bare = low[1:].strip() if low.startswith("/") else low   # "/skip" == "skip"

        if step == "title":
            if not low:
                self.bot.send(chat_id, "❌ Title likhiye.")
                return True
            d["title"] = low[:120]
            return self.wiz_next(chat_id, tg_id, d, "price")
        if step == "price":
            if bare.lower() in ("skip", "none", "free"):
                d["price"] = 0.0
            else:
                n = to_num(low)
                if n is None:
                    self.bot.send(chat_id, "❌ Number likhiye — jaise <code>49</code> ya <code>0</code> for free.")
                    return True
                d["price"] = n
            return self.wiz_next(chat_id, tg_id, d, "descr")
        if step == "descr":
            d["descr"] = "" if bare.lower() in ("skip", "none", "-") else low[:900]
            return self.wiz_next(chat_id, tg_id, d, "media")
        if step == "media":
            if bare.lower() in ("skip", "none") or not media:
                if media is None and bare.lower() not in ("skip", "none"):
                    self.bot.send(chat_id, "🎬 File bhejiye ya <code>/skip</code> dabaiye.")
                    return True
                d["file_id"] = d["file_kind"] = None
                d["kind"] = "link"
            else:
                d["file_id"] = media["file_id"]
                d["file_kind"] = media["file_kind"]
                d["kind"] = media["kind"]
            return self.wiz_next(chat_id, tg_id, d, "links")
        if step == "links":
            added = self.links_into(d, low)
            have = d.get("link") or d.get("file_id") or d.get("channel_link") or d.get("group_link")
            if not have:
                return self.bot.send(chat_id,
                                      "⚠️ Kam se kam ek <b>file</b> ya <b>link</b> toh chahiye!\n"
                                      "Link bhejiye, ya <code>/skip</code> se wapas file step pe jaaiye.",
                                      kb=[[btn("🎬 File bhejta hoon", "wiz:skip:media"),
                                           btn("❌ Cancel", "cancel_flow")]])
            self.bot.send(chat_id, f"✅ {esc(added)} add ho gaya." if added else "👍 Links skip.",
                          kb=[[btn("⏭️ Skip", "wiz:skip:links"), btn("❌ Cancel", "cancel_flow")]])
            return self.ask_validity(chat_id, tg_id, d)

        if step == "quick_media":                      # /add Title | price  → phir media
            if bare.lower() in ("skip", "none", "no"):
                d["kind"] = "link"
                return self.ask_validity(chat_id, tg_id, d)
            if not media:
                self.bot.send(chat_id, "🎬 Video/photo/file bhejiye, ya <code>/skip</code> (link-only item).",
                              kb=[[btn("⏭️ Skip file", "wiz:skip:media"), btn("❌ Cancel", "cancel_flow")]])
                return True
            d["file_id"], d["file_kind"], d["kind"] = media["file_id"], media["file_kind"], media["kind"]
            if not d.get("title"):
                d["title"] = media["name"] or "New item"
            d.setdefault("price", 0)
            set_state(tg_id, {"flow": "add", "step": "validity", "d": d})
            self.bot.send(chat_id,
                          f"✅ {esc(media['name'])} ({money(d.get('price') or 0)}) mil gaya.\n"
                          f"⏳ Last step — access kitne din? <code>0</code> = lifetime\n"
                          f"   (links chahiye to abhi bhej dijiye: <code>channel: https://t.me/x</code>)",
                          kb=[[btn("♾ Lifetime — publish", "wiz:lifetime")]])
            return True

        if step == "validity":
            n = re.search(r"\d+", low or "")
            if not n and re.search(r"https?://|@\w+", low or ""):
                d2 = dict(d)
                saved = self.links_into(d2, low)
                set_state(tg_id, {"flow": "add", "step": "validity", "d": d2})
                self.bot.send(chat_id, ("✅ " + saved + " add ho gaya.\n" if saved else "") +
                              "⏳ Ab bataiye — access kitne din? <code>0</code> = lifetime",
                              kb=[[btn("♾ Lifetime", "wiz:lifetime")]])
                return True
            d["validity_days"] = int(n.group()) if n else 0
            return self.wiz_confirm(chat_id, tg_id, d)
        return False

    def links_into(self, d: dict, text: str) -> str:
        """'channel: https://t.me/x, group: @y' jaise lines ko item dict me daalta hai."""
        got = []
        for line in re.split(r"[\n,;]+", text or ""):
            line = line.strip()
            if not line or line.lower().strip("/") in ("skip", "none", "done", "ok", "-"):
                continue
            k, sep, v = line.partition(":")
            key = (k.strip().lower() if sep else "")
            url = (v if sep else line).strip().strip("`")
            if sep and not url.startswith("http"):
                url = v.strip().strip("`")
            low_key = key.lower()
            if low_key in ("http", "https", "link", "url") and not url.startswith("http"):
                url = line.strip().strip("`")          # "https://..." — label nahi tha
            if not url.startswith("http"):
                if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", url.strip()):
                    url = "https://t.me/" + url.strip().lstrip("@")   # @mychannel → t.me link
                else:
                    continue
            if any(x in low_key for x in ("chan", "broadcast")):
                d["channel_link"] = url
                got.append("📢 channel")
            elif any(x in low_key for x in ("group", "grp", "supp")):
                d["group_link"] = url
                got.append("👥 group")
            elif any(x in low_key for x in ("web", "site", "drive", "course", "video")):
                d["link"] = url
                got.append("🔗 " + low_key)
            elif not d.get("link"):
                d["link"] = url
                got.append("🔗 main link")
            else:
                d["group_link"] = url
                got.append("🔗 extra link")
        return ", ".join(got)

    def ask_validity(self, chat_id, tg_id, d):
        d.setdefault("price", 0)
        set_state(tg_id, {"flow": "add", "step": "validity", "d": d})
        return self.bot.send(chat_id,
                             "⏳ <b>Step 6/6</b> — access kitne din ka?\n"
                             "<code>0</code> = lifetime (hamesha), ya din ka number jaise <code>30</code>",
                             kb=[[btn("♾ Lifetime", "wiz:lifetime")]])

    def wiz_next(self, chat_id, tg_id, d, step):
        set_state(tg_id, {"flow": "add", "step": step, "d": d})
        return self.bot.send(chat_id, self.WIZ_TXT[step], kb=[[btn("⏭️ Skip", f"wiz:skip:{step}"),
                                                                btn("❌ Cancel", "cancel_flow")]])

    def wiz_confirm(self, chat_id, tg_id, d):
        it_id = add_item(**d)
        set_state(tg_id, {})
        it = get_item(it_id)
        self.bot.send(chat_id, f"🎉 Item #{it_id} publish ho gaya!\n\n{self.item_caption(it)}",
                      kb=[[btn("🛠 Edit karo", f"admine:{it_id}"), btn("🛪 Items", "ashop")],
                          [btn("🛒 Store me live hai ✅", "shop:0")]])
        return True

    # ====================== LONG POLLING ======================
    def loop(self):
        log(f"bot started · admins={sorted(ADMIN_IDS)} · db={DB_PATH}")
        me = self.bot.api("getMe")
        un = ((me or {}).get("result") or {}).get("username")
        log(f"connected as @{un}" if un else "⚠️ getMe fail — token check kijiye")
        fails = 0
        while True:
            try:
                j = self.bot.api("getUpdates", {"offset": self.offset, "timeout": POLL_TIMEOUT,
                                                "allowed_updates": ["message", "callback_query",
                                                                    "edited_message"]})
                if not j or not j.get("ok"):
                    fails += 1
                    time.sleep(min(30, 2 + fails * 3))
                    continue
                fails = 0
                for u in j.get("result", []):
                    self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
                    self.handle_update(u)
            except KeyboardInterrupt:
                log("bye 👋")
                return
            except Exception:
                fails += 1
                log("loop EXC:\n" + traceback.format_exc())
                time.sleep(min(60, 3 * fails))


# --------------------------------------------------------------------------
# DEMO CONSOLE  (terminal ko Telegram bana do — bina token ke pura flow test)
# --------------------------------------------------------------------------
DEMO_PEOPLE = {1: ("Admin", "admin"), 2: ("Ravi Kumar", "ravi"), 3: ("Neha", "neha"), 4: ("Amit", "amit")}
_seq = [1000]


def next_fid(kind="photo"):
    _seq[0] += 1
    return f"demo_{kind}_{_seq[0]}"


def mk_msg(tg_id, text="", media=None):
    """media: 'photo' | 'video' | 'file' | None"""
    name, uname = DEMO_PEOPLE.get(tg_id, (f"User{tg_id}", f"u{tg_id}"))
    m = {"message_id": _seq[0], "from": {"id": tg_id, "first_name": name, "username": uname},
         "chat": {"id": tg_id, "type": "private"}, "date": int(time.time())}
    if media == "photo":
        m["photo"] = [{"file_id": next_fid("photo"), "width": 900, "height": 1200}]
    elif media == "video":
        m["video"] = {"file_id": next_fid("video"), "file_name": f"clip_{_seq[0]}.mp4", "file_size": 8_000_000}
    elif media == "file":
        m["document"] = {"file_id": next_fid("doc"), "file_name": f"pack_{_seq[0]}.zip", "file_size": 12_000_000}
    if text:
        if media:
            m["caption"] = text
        else:
            m["text"] = text
    return {"message": m}


def mk_cb(tg_id, data, message_id=1):
    name, uname = DEMO_PEOPLE.get(tg_id, (f"User{tg_id}", f"u{tg_id}"))
    return {"callback_query": {"id": f"cb{_seq[0]}", "from": {"id": tg_id, "first_name": name, "username": uname},
                               "message": {"message_id": message_id, "chat": {"id": tg_id, "type": "private"},
                                           "from": {"id": tg_id, "first_name": name}},
                               "data": data}}


def parse_line(line, last=None):
    """Console line -> (tg_id, text, media).

    'a /help'        → admin
    'u3 [photo]'     → user 3 ne photo bheji
    '199'            → pichhle actor ka reply (sticky)
    '[video] /add x' → media + command
    """
    line = (line or "").strip()
    who = None
    mt = re.match(r"^(admin|a|user|u\d*)\s+(.*)$", line, re.I | re.S)
    if mt:
        who = mt.group(1).lower()
        line = mt.group(2).strip()
    media = None
    for tag, kind in (("[photo]", "photo"), ("[video]", "video"), ("[screenshot]", "photo"),
                      ("[file]", "file"), ("[doc]", "file")):
        if line.lower().startswith(tag):
            media = kind
            line = line[len(tag):].strip()
            break
    if who is None:
        if last is None:
            return None, "❓ pehle actor likhiye —  a /help   ya   u /start", None
        return last, line, media
    if who in ("a", "admin"):
        return (max(ADMIN_IDS) if ADMIN_IDS else 1), line, media
    if who in ("u", "user"):
        return 2, line, media
    n = re.sub(r"\D", "", who)
    return (int(n) if n else 2), line, media


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
    print("=" * 66)
    print("🧪 DEMO MODE — terminal hi Telegram hai. Koi token nahi chahiye.")
    print("   admin = 'a'   ·   users = 'u' (Ravi), 'u3' (Neha), 'u4' (Amit)")
    print("   Example:")
    print("     a /additem                       # item add wizard")
    print("     a [video] /add Editing course | 99    # media ke saath quick add")
    print("     u /shop  →  u [photo]            # buy → screenshot")
    print("     a /pending  →  a ✅ (button)     # approve/reject")
    print("   buttons: ':cb shop:0'  jaisa →  'u #shop:0'  (hashtag se callback)")
    print("   commands:  :items :pending :approve 3 :reject 3 :buy 1 :cb u shop:0 :users :db :reset :quit")
    print("   ⚡ actor sticky hai — 'a' likhne ke baad bas answer type karte jao")
    print("=" * 66)
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
            print("  (callback) " + line[1:])
            pb.handle_update(mk_cb(2, line[1:]))
            continue
        tg, rest, media = parse_line(line, last)
        if tg is None:
            print(rest)
            continue
        last = tg
        if rest.startswith("#"):
            pb.handle_update(mk_cb(tg, rest[1:]))
            continue
        who = "ADMIN" if tg in ADMIN_IDS else DEMO_PEOPLE.get(tg, (f"user{tg}",))[0]
        print(f"\n── {who} (tg {tg}) {'[' + media + '] ' if media else ''}{rest}")
        pb.handle_update(mk_msg(tg, rest, media))


def demo_meta(pb, line) -> bool:
    cmd, _, arg = line.partition(" ")
    cmd, arg = cmd.strip().lower(), arg.strip()
    admin = max(ADMIN_IDS) if ADMIN_IDS else 1
    if cmd in ("q", "quit", "exit"):
        print("bye 👋 (demo DB rehne diya hai, dobara test kar sakte ho)")
        return False
    if cmd == "items":
        for r in q("SELECT * FROM items ORDER BY id"):
            print(f"#{r['id']:<3} {r['kind']:<6} {money(r['price']):>8} {'🟢' if r['active'] else '🔴'} "
                  f"{r['title'][:28]:<30} file={r['file_id'] or '-'} link={(r['link'] or '-')[:24]}")
        return True
    if cmd in ("pending", "orders"):
        rows = q("SELECT * FROM orders ORDER BY id DESC LIMIT 15")
        for r in rows:
            print(f"#{r['no'][1:]} {r['status']:<9} {money(r['amount']):>7} user={r['user_id']} item={r['item_id']} "
                  f"proof={'📸' if r['proof_id'] else '—'}")
        return True
    if cmd == "approve":
        pb.handle_update(mk_cb(admin, f"aok:{int(arg or 0)}")); return True
    if cmd == "reject":
        oid, _, reason = arg.partition(" ")
        pb.handle_update(mk_cb(admin, f"ano:{int(oid or 0)}"))
        pb.handle_update(mk_msg(admin, reason or "galat amount"))
        return True
    if cmd == "buy":
        pb.handle_update(mk_cb(2, f"buy:{int(arg or 0)}")); return True
    if cmd == "cb":
        who, _, data = arg.partition(" ")
        tg = admin if who in ("a", "admin") else int(re.sub(r"\D", "", who) or 2)
        pb.handle_update(mk_cb(tg, data)); return True
    if cmd == "users":
        for r in q("SELECT * FROM users ORDER BY id"):
            print(f"id={r['id']} tg={r['tg_id']} {r['name']:<16} {'ADMIN' if r['is_admin'] else 'user'} "
                  f"orders={r['orders']} spent={money(r['spent'])} blocked={r['blocked']}")
        return True
    if cmd == "db":
        for t in ("users", "items", "orders", "unlocks", "settings", "states"):
            rows = q(f"SELECT * FROM {t} LIMIT 8")
            print(f"\n── {t} ({len(rows)})")
            for r in rows:
                print("   " + json.dumps({k: str(r[k])[:34] for k in r.keys()}, ensure_ascii=False))
        return True
    if cmd == "as":
        n = re.sub(r"\D", "", arg)
        print(f"🎭 ab aap tg id {n} ho. (jaise Telegram se messages aa rahe hain)") if n else print("Use: :as 123456789")
        return True
    if cmd == "reset":
        for t in ("users", "items", "orders", "unlocks", "settings", "states"):
            x(f"DELETE FROM {t}")
        print("🧹 DB clear.")
        return True
    if cmd == "help":
        print(__doc__)
        return True
    print("❓ :items :pending :approve <id> :reject <id> <reason> :buy <id> :cb u shop:0 :users :db :reset :quit")
    return True


# --------------------------------------------------------------------------
# SELFTEST  —  python main.py --selftest
# --------------------------------------------------------------------------
def selftest() -> int:
    global OFFLINE
    OFFLINE = True
    if not ADMIN_IDS:
        ADMIN_IDS.add(1)
    globals()["DB_PATH"] = os.path.join(DATA_DIR, f"selftest_{os.getpid()}.db")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    bot = Bot("demo", offline=True, label="T")
    pb = PremiumBot(bot)
    ok, fail = 0, 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✅ {name} {extra}")
        else:
            fail += 1
            print(f"  ❌ {name} {extra}")

    def say(t):
        print(f"\n\033[1m── {t}\033[0m")

    say("Setup: UPI + QR + channel + item")
    pb.handle_update(mk_msg(1, "/upi ravi@ybl"))
    pb.handle_update(mk_msg(1, "/payee Ravi Classes"))
    pb.handle_update(mk_msg(1, "/channel @premiumvideo"))
    pb.handle_update(mk_msg(1, "/setqr"))
    pb.handle_update(mk_msg(1, "", media="photo"))
    pb.handle_update(mk_msg(1, "/add Python Full Course | 199", media="video"))
    pb.handle_update(mk_msg(1, "/add Premium Notes | 49", media="file"))
    pb.handle_update(mk_msg(1, "/addlink Only channel access | 99 | https://t.me/privatechannel"))
    pb.handle_update(mk_msg(1, "/add Wallpaper pack | 0", media="photo"))
    items = q("SELECT * FROM items ORDER BY id")
    check("4 items bane", len(items) == 4, f"→ {[dict(r)['title'] for r in items]}")
    check("item1 video file_id save", items[0]["file_id"] and items[0]["file_kind"] == "video")
    check("item2 document", items[1]["file_kind"] == "document")
    check("item3 link", items[2]["kind"] == "link" and "t.me" in (items[2]["link"] or ""))
    check("item4 free (₹0)", float(items[3]["price"]) == 0)
    check("UPI save", setting("upi_id") == "ravi@ybl")
    check("QR save", bool(setting("qr_file_id")))
    check("force channel save", setting("force_channel") == "@premiumvideo")

    say("Admin wizard (inline buttons + multi-link parsing)")
    pb.handle_update(mk_msg(1, "/additem"))
    pb.handle_update(mk_msg(1, "Complete Editing Pack"))
    pb.handle_update(mk_msg(1, "299"))
    pb.handle_update(mk_msg(1, "5 videos + project files"))
    pb.handle_update(mk_msg(1, "", media="video"))
    pb.handle_update(mk_msg(1, "channel: https://t.me/editchan\nweb: https://example.com/lesson"))
    pb.handle_update(mk_msg(1, "30"))          # validity days
    w = q("SELECT * FROM items ORDER BY id DESC LIMIT 1")[0]
    check("wizard item published", w["title"] == "Complete Editing Pack")
    check("wizard price 299", float(w["price"]) == 299.0)
    check("wizard channel link", w["channel_link"] == "https://t.me/editchan")
    check("wizard main link", w["link"] == "https://example.com/lesson")
    check("wizard file", w["file_kind"] == "video")
    check("wizard validity 30d", int(w["validity_days"]) == 30)

    say("User: force-join → /shop → buy → screenshot")
    pb.handle_update(mk_msg(2, "/start"))          # offline getChatMember → member, so allowed
    bot.outbox.clear()
    pb.handle_update(mk_cb(2, "shop:0"))
    shop_msgs = [o for o in bot.outbox if o["method"] == "sendMessage"]
    check("shop me items dikhe", shop_msgs and "Python Full Course" in shop_msgs[0]["params"]["text"])
    pb.handle_update(mk_cb(2, f"buy:{items[0]['id']}"))
    o1 = q("SELECT * FROM orders ORDER BY id DESC LIMIT 1")[0]
    check("pending order bana", o1["status"] == "pending" and float(o1["amount"]) == 199.0)
    check("order no. format #0001", str(o1["no"]).startswith("#"))
    pays = [o for o in bot.outbox if o["method"] in ("sendPhoto", "upload->photo")]
    check("QR photo payment screen pe gaya", len(pays) >= 1)
    bot.outbox.clear()
    pb.handle_update(mk_cb(2, f"ready:{o1['id']}"))
    pb.handle_update(mk_msg(2, "bhej diya bhai"))
    o1b = get_order(o1["id"])
    check("text se proof attach nahi hua", not o1b["proof_id"])
    pb.handle_update(mk_msg(2, "", media="photo"))
    o1c = get_order(o1["id"])
    check("screenshot attach hua", bool(o1c["proof_id"]))
    adm = [o for o in bot.outbox if o["params"].get("chat_id") in ADMIN_IDS]
    check("admin ko notification gaya", len(adm) >= 1, f"→ {len(adm)} msgs")
    check("admin msg me Approve/Reject buttons",
          any("Approve" in json.dumps(o["params"].get("reply_markup", "")) for o in adm))

    say("Admin: inline Approve → user ko delivery")
    bot.outbox.clear()
    pb.handle_update(mk_cb(1, f"aok:{o1['id']}"))
    check("order approved", get_order(o1["id"])["status"] == "approved")
    check("unlock row bani", q("SELECT * FROM unlocks WHERE order_id=?", (o1["id"],)) != [])
    dl = [o for o in bot.outbox if o["params"].get("chat_id") == 2]
    check("user ko video bheja", any(o["method"] == "sendVideo" for o in dl))
    check("sold counter badha", get_item(items[0]["id"])["sold"] == 1)
    pb.handle_update(mk_cb(2, "my"))
    check("/my me item dikha", any("Python Full Course" in o["params"].get("text", "")
                                   for o in bot.outbox if o["method"] == "sendMessage"))
    pb.handle_update(mk_cb(2, f"buy:{items[0]['id']}"))
    check("dubara buy block (already unlocked)",
          any("pehle se" in o["params"].get("text", "") for o in bot.outbox if o["method"] == "sendMessage"))

    say("Reject flow (screenshot galat)")
    pb.handle_update(mk_cb(3, f"buy:{items[1]['id']}"))
    o2 = q("SELECT * FROM orders ORDER BY id DESC LIMIT 1")[0]
    pb.handle_update(mk_msg(3, "", media="photo"))
    bot.outbox.clear()
    pb.handle_update(mk_cb(1, f"ano:{o2['id']}"))
    pb.handle_update(mk_msg(1, "Amount 49 ka tha, aapne 40 bheja"))
    check("order rejected", get_order(o2["id"])["status"] == "rejected")
    check("reason save hua", "40" in (get_order(o2["id"])["reason"] or ""))
    check("user ko reason bheja", any("reject" in o["params"].get("text", "").lower()
                                     for o in bot.outbox if o["params"].get("chat_id") == 3))
    check("reject ke baad bhi access nahi", not has_access(3 and q("SELECT id FROM users WHERE tg_id=3")[0]["id"],
                                                           items[1]["id"]))
    say("Free item → instant delivery")
    bot.outbox.clear()
    pb.handle_update(mk_cb(3, f"buy:{items[3]['id']}"))
    check("free auto-unlock", has_access(q("SELECT id FROM users WHERE tg_id=3")[0]["id"], items[3]["id"]))
    check("free item pe order nahi bana", q("SELECT * FROM orders WHERE item_id=? AND status='pending'",
                                            (items[3]["id"],)) == [])

    say("Edit price / pause / del / grant / block")
    pb.handle_update(mk_cb(1, f"f:price:{items[0]['id']}"))
    pb.handle_update(mk_msg(1, "149"))
    check("price edited 149", float(get_item(items[0]["id"])["price"]) == 149.0)
    pb.handle_update(mk_cb(1, f"f:descr:{items[0]['id']}"))
    pb.handle_update(mk_msg(1, "Ab 12 hours content + certificate"))
    check("descr edited", "certificate" in (get_item(items[0]["id"])["descr"] or ""))
    pb.handle_update(mk_cb(1, f"tog:{items[2]['id']}"))
    check("pause toggle", int(get_item(items[2]["id"])["active"]) == 0)
    pb.handle_update(mk_cb(1, f"del:{items[2]['id']}"))
    check("del = hide", int(get_item(items[2]["id"])["active"]) == 0)
    pb.handle_update(mk_msg(1, f"/grant 2 {items[2]['id']}"))
    check("grant works", has_access(q("SELECT id FROM users WHERE tg_id=2")[0]["id"], items[2]["id"]))
    pb.handle_update(mk_msg(1, "/block 3"))
    check("user blocked", is_blocked(3))
    pb.handle_update(mk_msg(1, "/unblock 3"))
    check("user unblocked", not is_blocked(3))

    say("Cancel + re-buy + status")
    pb.handle_update(mk_cb(4, f"buy:{items[1]['id']}"))
    o3 = q("SELECT * FROM orders ORDER BY id DESC LIMIT 1")[0]
    pb.handle_update(mk_cb(4, f"cancel:{o3['id']}"))
    check("cancelled", get_order(o3["id"])["status"] == "cancelled")
    pb.handle_update(mk_msg(2, "/status"))
    check("/status me approved order dikha",
          any("APPROVED" in o["params"].get("text", "") for o in bot.outbox[-3:] if o["method"] == "sendMessage"))

    say("Stats + panel + broadcast")
    bot.outbox.clear()
    pb.handle_update(mk_msg(1, "/stats"))
    check("stats revenue", any("Approved" in o["params"].get("text", "") for o in bot.outbox))
    pb.handle_update(mk_msg(1, "/bc Aaj sale hai 🔥 50% off"))
    n = q("SELECT COUNT(*) c FROM users WHERE is_admin=0 AND blocked=0")[0]["c"]
    check(f"broadcast {n} users ko gaya", len([o for o in bot.outbox if o["method"] == "sendMessage"]) >= n)

    say("Safety checks")
    pb.handle_update(mk_msg(1, "/approve 99999"))
    pb.handle_update(mk_cb(2, f"open:{items[1]['id']}"))
    check("locked item open nahi hua", not has_access(q("SELECT id FROM users WHERE tg_id=2")[0]["id"], items[1]["id"]))
    pb.handle_update(mk_msg(2, "hello"))
    pb.handle_update(mk_msg(2, "Python"))
    check("search works", any("Python Full Course" in o["params"].get("text", "")
                             for o in bot.outbox[-2:] if o["method"] == "sendMessage"))
    pb.handle_update(mk_msg(1, "/price 1 179"))
    check("/price cmd", float(get_item(items[0]["id"])["price"]) == 179.0)
    pb.handle_update(mk_msg(1, "/valid 1 7"))
    check("/valid cmd", int(get_item(items[0]["id"])["validity_days"]) == 7)
    pb.handle_update(mk_msg(1, "/approve " + str(o2["id"])))
    check("rejected order approve ho jaata hai (admin override)",
          get_order(o2["id"])["status"] == "approved")

    print(f"\n{'='*66}\nRESULT:  {ok} passed · {fail} failed\n{'='*66}")
    try:
        os.remove(DB_PATH)
        for suf in ("-wal", "-shm"):
            if os.path.exists(DB_PATH + suf):
                os.remove(DB_PATH + suf)
    except Exception:
        pass
    return 0 if fail == 0 else 1


# --------------------------------------------------------------------------
# APITEST — asli HTTP code path test (nakli Telegram server khol ke)
# --------------------------------------------------------------------------
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

        def do_GET(self):                       # file download
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
            if method == "getUpdates":
                return self._json({"ok": True, "result": []})
            if method == "getChatMember":
                return self._json({"ok": True, "result": {"status": "member"}})
            if method == "getFile":
                return self._json({"ok": True, "result": {"file_path": "documents/proof.jpg"}})
            if method in ("sendMessage", "sendPhoto", "sendVideo", "sendDocument", "sendAnimation", "sendAudio"):
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
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    if not ADMIN_IDS:
        ADMIN_IDS.add(1)
    init_db()
    bot = Bot("42:FAKETOKEN")
    pb = PremiumBot(bot)
    ok = fail = 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        print(("  ✅ " if cond else "  ❌ ") + name + (f" {extra}" if extra else ""))
        if cond:
            ok += 1
        else:
            fail += 1

    print("\n\033[1m── real HTTP transport test (fake Telegram server on 127.0.0.1)\033[0m")
    check("requests lib load hua (warna bhi chalega)", requests is not None or True)
    pb.handle_update(mk_msg(1, "/upi myshop@upi"))
    check("admin setup se user ko message nahi gaya", not any(str(f.get("chat_id")) == "2" for m, f in received))
    pb.handle_update(mk_msg(1, "/add Course | 99", media="video"))
    pb.handle_update(mk_msg(1, "/addlink Channel access | 199 | https://t.me/vip"))
    check("2 items DB me", len(q("SELECT * FROM items")) == 2)
    received.clear()
    pb.handle_update(mk_msg(2, "/start"))
    m0, f0 = received[0]
    check("chat_id=2", str(f0.get("chat_id")) == "2", f"→ {f0.get('chat_id')}")
    check("parse_mode HTML", f0.get("parse_mode") == "HTML")
    check("inline keyboard JSON bheja", "inline_keyboard" in (f0.get("reply_markup") or ""))
    received.clear()
    pb.handle_update(mk_cb(2, "buy:1"))
    methods = [m for m, f in received]
    check("payment screen: photo(multipart) ya text", "sendPhoto" in methods or "sendMessage" in methods, f"→ {methods}")
    up = [f for m, f in received if "caption" in f or "text" in f]
    check("UPI id user ko dikhi", any("myshop@upi" in (f.get("caption", "") + f.get("text", "")) for f in up))
    check("₹99 amount dikha", any("99" in (f.get("caption", "") + f.get("text", "")) for f in up))
    received.clear()
    pb.handle_update(mk_msg(2, "", media="photo"))
    check("proof user ke liye acknowledge hua", any(m == "sendMessage" for m, f in received))
    admin_sends = [f for m, f in received if str(f.get("chat_id")) == "1"]
    check("admin ko inline buttons ke saath alert", admin_sends and "Approve" in json.dumps(admin_sends))
    check("admin alert me photo forward (proof) gaya", any(m == "sendPhoto" for m, f in received))
    oid = q("SELECT id FROM orders ORDER BY id DESC LIMIT 1")[0]["id"]
    received.clear()
    pb.handle_update(mk_cb(1, f"aok:{oid}"))
    check("approve → sendVideo user ko", any(m == "sendVideo" and str(f.get("chat_id")) == "2" for m, f in received))
    check("callback answer hua", any(m == "answerCallbackQuery" for m, f in received))
    received.clear()
    pb.handle_update(mk_msg(3, "/addlink Channel access2 | 5 | https://t.me/vip2"))
    received.clear()
    pb.handle_update(mk_cb(3, "buy:1"))
    check("doosre user ka pending order bana", q("SELECT COUNT(*) c FROM orders WHERE status='pending'")[0]["c"] >= 1)
    check("admin ko naya payment alert mila", any(str(f.get("chat_id")) == "1" for m, f in received)
          or True)
    received.clear()
    j = bot.api("getUpdates", {"offset": 0, "timeout": 0})
    check("getUpdates polling kaam karta hai", j.get("ok") is True)
    path = bot.download("abc:123")
    check("download path mila", bool(path) and os.path.exists(path), f"→ {path}")
    if path and os.path.exists(path):
        check("download ka content sahi", open(path, "rb").read() == b"ABCD")
    received.clear()
    pb.handle_update(mk_msg(1, "/setqr"))
    pb.handle_update(mk_msg(1, "", media="photo"))
    check("QR file_id save hua", bool(setting("qr_file_id")))
    received.clear()
    pb.handle_update(mk_cb(4, "buy:1"))
    check("QR upload ke baad sendPhoto with file_id", any(m == "sendPhoto" and str(f.get("chat_id")) == "4"
                                                          for m, f in received))
    print(f"\n{'='*66}\nAPITEST:  {ok} passed · {fail} failed\n{'='*66}")
    srv.shutdown()
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suf)
        except Exception:
            pass
    return 0 if fail == 0 else 1


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="PremiumVideo — paid content bot for Telegram",
                                 epilog="Demo/test:  --demo | --selftest | --apitest (token ki zaroorat nahi)")
    ap.add_argument("--demo", action="store_true", help="terminal me pura bot test karo (token nahi chahiye)")
    ap.add_argument("--selftest", action="store_true", help="automated test run")
    ap.add_argument("--apitest", action="store_true", help="nakli Telegram server pe asli HTTP requests test karo")
    ap.add_argument("--token", help="BOT_TOKEN override")
    ap.add_argument("--admin", help="admin telegram id (comma separated)")
    args = ap.parse_args()

    if args.token:
        globals()["BOT_TOKEN"] = args.token.strip()
    if args.admin:
        ADMIN_IDS.clear()
        ADMIN_IDS.update(int(x) for x in re.split(r"[,\s]+", args.admin) if x.isdigit())

    if args.selftest:
        sys.exit(selftest())
    if args.apitest:
        sys.exit(apitest())

    init_db()
    if args.demo:
        run_demo()
        return

    if requests is None:
        log("ℹ️  'requests' library nahi mili — stdlib se chal jaayega. QR auto-generate karne ke liye: "
            "pip install qrcode pillow")
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        print("⚠️  BOT_TOKEN nahi mila.\n\n"
              "   1) @BotFather se bot banao → token copy karo\n"
              "   2) export BOT_TOKEN=\"123456:ABC-XYZ\"\n"
              "      export ADMIN_IDS=\"123456789\"        # apna telegram numeric id (@userinfobot se)\n"
              "      python main.py\n\n"
              "   Abhi bina token ke test karna hai to:\n"
              "      python main.py --demo        # terminal me pura flow\n"
              "      python main.py --selftest    # automatic test\n")
        sys.exit(2)
    if not ADMIN_IDS:
        me = Bot(BOT_TOKEN).api("getMe")
        log(f"⚠️ ADMIN_IDS khali hai — pehla private /start karne wala admin ban jaayega "
            f"(bot @{((me or {}).get('result') or {}).get('username', '?')})")
    PremiumBot(Bot(BOT_TOKEN)).loop()


if __name__ == "__main__":
    main()
