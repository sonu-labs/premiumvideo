# 🎬 PremiumVideo — paid content bot for Telegram

**One file: `main.py`.** The admin sells anything — video, photo, document, private channel/group invite,
website link or plain text (coupon / serial key) — each item with its own price. The admin sets the payment
QR + UPI id and a welcome photo. The user pays, sends a **screenshot**, and the admin gets an inline
**Approve / Decline** notification; approval delivers the content instantly.

```
admin: /admin  →  everything is a button (items, prices, QR, approvals, stats, broadcast)
user : /start  →  🛍 store  →  💵 pay  →  📸 screenshot  →  ⏳  →  ✅ delivered + 📚 library
```

---

## 1. Run it

```bash
pip install -r requirements.txt     # optional — only needed for the UPI QR image + MongoDB backend
export BOT_TOKEN="123456789:AA_your_token_from_BotFather"
export ADMIN_IDS="123456789"        # your numeric Telegram id (from @userinfobot), comma separated for 2-3 admins
python main.py
```

* Token: [@BotFather](https://t.me/BotFather) → `/newbot`.
* Secrets stay in the environment: export them as above, **or** put `BOT_TOKEN=…` / `ADMIN_IDS=…`
  in a `.env` file next to `main.py` (it is git-ignored; real env vars always win).
* `ADMIN_IDS` empty → **the first person who sends /start becomes the admin** (then set the env var).
* No `requests` installed? It still runs (urllib fallback). `qrcode` + `pillow` are only for the auto QR image.
* Storage: SQLite file `premiumvideo.db` by default — or **MongoDB** for keeping the data safe (below).

### MongoDB — recommended for data safety

Set `MONGO_URI` and the whole shop (users, items, orders, payments, settings) lives in MongoDB
instead of the local file — use [MongoDB Atlas](https://www.mongodb.com/atlas) free tier for automatic
backups & replicas:

```bash
pip install pymongo
export MONGO_URI="mongodb+srv://user:pass@cluster0.xxxx.mongodb.net"
export MONGO_DB="premiumvideo"      # optional, this is the default
python main.py
```

* Already have a shop on SQLite? Migrate it once: `python main.py --migrate`
  (add `--force` to overwrite an existing MongoDB database). Keep the old `premiumvideo.db` as backup.
* If `MONGO_URI` is set but unreachable the bot stops with a clear error — it never silently
  falls back to SQLite, so data always goes where you expect.
* `--demo` / `--selftest` / `--apitest` always run on throw-away local SQLite databases.

### Test it right now (no token, no internet)

```bash
python main.py --demo           # interactive simulator: the terminal becomes Telegram
python main.py --selftest       # 102 checks over the whole lifecycle (SQLite backend)
python main.py --selftest-mongo # the same 102 checks against the MongoDB backend
python main.py --apitest        # 16 checks: real HTTP calls verified against a fake Telegram server
```

In `--demo`, `a` = admin, `u` / `u3` / `u4` = customers, `[photo]` `[video]` `[file]` = media messages,
`#` presses an inline button, and the actor is sticky (type only the answer after `a /additem`):

```
a #s:brand                →  Ravi Premium
a #s:upi_id               →  ravi@ybl
a #setqr                  →  a [photo]            ← your real UPI QR image
a #setwelcomephoto        →  a [photo]            ← photo on the /start screen
a #newitem                                        ← or: a /additem
   a #wtype:video                                 ← pick the type (video / photo / file / link / text)
   Python Full Course                             ← name
   199                                            ← price (0 = free)
   a [video]                                      ← the content itself
u /shop → u #item:1 → u #ready:1 → u [photo] paid
a #pend → ✅ Approve
u /library                                          ← content is there
```
Console shortcuts: `:items :pend :orders :approve 1 :decline 1 wrong amount :users :db :reset :quit`

---

## 2. Admin panel (all buttons)

`/admin` (or `⚙️` buttons everywhere) opens:

| Button | What it does |
|---|---|
| **📦 Items** | list → tap an item → edit menu (title, description, price, file, links, validity, publish, delete, preview, buyers) |
| **➕ New item** | 6-step wizard: title → price → description → file → links → validity |
| **⏳ Approvals** | pending payments with proof screenshot, ✅ Approve & deliver / ❌ Decline (reason asked), 🗑 Approve all |
| **🧾 All orders** | every order with status; tap one for details, 🖼 screenshot, 🔁 re-deliver, ↩️ re-open |
| **💳 Payment setup** | UPI id, payee name, **upload / remove QR**, checkout note, refund note, extra instructions, checkout preview |
| **🏪 Store settings** | brand name, **welcome text** (+ **🔄 Reset to default** — restores the built-in professional welcome), **welcome photo**, force-channel-join, empty-store note, **📹 free-demo link**, **📢 proofs channel**, **🚨 support link**, **display stats** (joined / month / today), welcome preview |
| **📊 Sales report** | revenue, today, 7 days, best sellers |
| **👥 Customers** | profile → 🎁 grant free access, 📣 private message, 🚫 block, 🧾 their orders |
| **📣 Broadcast** | text or image to all customers |
| **ⓘ Commands** | the shortcut list below |

Text-command shortcuts (same result, for power users):

```
items      /additem · /add Title | 199 (reply to a video/file) · /addlink Title | 99 | https://t.me/vip
           /addtext Coupon | 25 | RAVI25 · /items · /edit 3 · /price 3 49 · /valid 3 30
           /pause 3 · /resume 3 · /del 3
payments   /setqr · /welcomephoto · /welcome <text> · /upi id@bank · /payee <name> · /note <text>
           /brand <name> · /channel @yourchannel (force join) · /settings
orders     /pending · /orders 12 · /approve 12 · /stats · /users · /bc <message>
           /grant 5 3 · /revoke 5 3 · /block 123456 · /unblock 123456 · /unstick 123456
```

**Auto QR:** if you never upload a QR image, the bot generates a `upi://pay?pa=…&am=199&tr=0001` QR at
checkout, so the amount and order id are already filled when the customer scans it.

---

## 3. Customer flow

```
/start    welcome photo + professional welcome text (blockquote formatting, "why buy from us"
          bullets, TECH SUPPORT line, 👥🔥 counters the admin can set)
          + [Buy Premium Videos] [Free demo ↗] [Proofs ↗]
          [My profile] [Support] [How to use] — premium emoji icons + colored buttons
/shop     every item is its own button — "Title (₹199)" — plus Prev/Next paging, ✅ marks owned
💦 Buy    tapping an item goes STRAIGHT to the checkout: QR image, UPI id, exact amount,
          order id note — and a single button: [I paid — submit screenshot]
📸 Proof  the bot only accepts an actual photo/file — text is refused
⏳         "Proof received — waiting for admin approval"
✅         a professional PURCHASE SUCCESSFUL receipt: item, 💰 amount paid, 🧾 order id + date,
          ♾️ access validity — the file (or [Open link · ₹199] buttons) + [My library] [Store]
```
Free items (`price = 0`) unlock instantly with no order. Optional `⏱ validity` per item auto-expires access.
If `force channel join` is set, the customer must join before the bot works (make the bot an admin of that channel).

### Premium emoji (custom emoji + colored buttons)

**Both sides** ship with **Telegram premium (custom) emoji** — animated 💦 🍑 🥵 🍭 🍆 🍒 🌸 😘 👅 😄 on the
user side and 📦 ⚙️ 💳 👥 ✅ ❌ 📣 📊 🖼 🎁 🚫… on the **admin side** — in messages (`<tg-emoji>`) and as
**button icons** (`icon_custom_emoji_id`), plus **colored buttons** everywhere (`style`: `success`
green / `primary` blue / `danger` red — Bot API 9.4). This needs the **bot owner to have Telegram
Premium** (or a Fragment username on the bot). If Telegram ever rejects them, the bot automatically
retries with plain emoji, so nothing breaks. Set `PREMIUM_EMOJI=0` to force plain emoji.

**Emoji IDs** come from the `ADMIN PANEL EMOJI ID/` folder — every JSON `.txt` file there
(`[{"emoji": "📦", "custom_emoji_id": "…"}]`) is loaded at startup and overrides the built-in set.
Drop your own files in that folder (or edit the existing ones) to change any icon.

---

## 4. Deploy 24/7

```bash
# VPS
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
BOT_TOKEN="..." ADMIN_IDS="..." ./venv/bin/python main.py
```

`/etc/systemd/system/premiumvideo.service`
```ini
[Unit]
Description=PremiumVideo bot
After=network-online.target

[Service]
WorkingDirectory=/opt/premiumvideo
Environment=BOT_TOKEN=123:abc
Environment=ADMIN_IDS=123456789
ExecStart=/usr/bin/python3 /opt/premiumvideo/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now premiumvideo && journalctl -u premiumvideo -f
```
Railway / Render / Koyeb: start command `python main.py`, env `BOT_TOKEN` + `ADMIN_IDS` — it uses **long
polling**, so no public URL or webhook is needed.

---

## 5. Tuning

| Where | What |
|---|---|
| env `MONGO_URI` / `MONGO_DB` | store everything in MongoDB instead of the local SQLite file (see section 1) |
| env `PREMIUM_EMOJI=0` | turn off premium custom emoji / button icons (plain emoji fallback) |
| top of `main.py` | `PEMOJI` (premium emoji → id map), `CURRENCY`, `PAGE_SIZE` (items per page), `STATE_TTL_HOURS`, `DEFAULTS` (all default texts) |
| `SCHEMA` / `add_item()` | add your own item fields (e.g. `offer_price`, `sample_file_id`) |
| `item_caption()` | how the item page looks |
| `deliver()` | how content is sent (file + link buttons + library footer) |

Notes: the bot reuses Telegram `file_id`s, so a file is uploaded **once** and delivered forever (fast, no
storage cost). Bot upload limit is 50 MB — bigger items: sell the channel/drive link instead.
`/api.telegram.org` must be reachable from the server running the bot. The storage layer is pluggable
(`SQLiteStore` / `MongoStore` at the top of `main.py`) — every screen works identically on both backends.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `BOT_TOKEN is missing` | export the token, or `python main.py --token "123:abc"` |
| bot silent | restart; check that the host can reach `api.telegram.org` (proxy/VPN can block it) |
| customer got no content after approval | they blocked the bot — the admin sees a "Delivery failed" notice, use 🔁 Re-deliver later |
| QR image missing at checkout | `pip install qrcode pillow`, or upload your own with **💳 Payment setup → 🖼 Upload QR** |
| stuck in a wizard step | `/cancel` (customer) or `/unstick <telegram id>` (admin) |

```bash
python main.py --selftest && python main.py --apitest     # both green ⇒ the bot is healthy
```
