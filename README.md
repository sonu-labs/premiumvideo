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
pip install -r requirements.txt     # optional — only needed to auto-generate the UPI QR image
export BOT_TOKEN="123456789:AA_your_token_from_BotFather"
export ADMIN_IDS="123456789"        # your numeric Telegram id (from @userinfobot), comma separated for 2-3 admins
python main.py
```

* Token: [@BotFather](https://t.me/BotFather) → `/newbot`.
* `ADMIN_IDS` empty → **the first person who sends /start becomes the admin** (then set the env var).
* No `requests` installed? It still runs (urllib fallback). `qrcode` + `pillow` are only for the auto QR image.
* Storage: SQLite file `premiumvideo.db` — that single file *is* your database (backup = copy it).

### Test it right now (no token, no internet)

```bash
python main.py --demo        # interactive simulator: the terminal becomes Telegram
python main.py --selftest    # 89 checks over the whole lifecycle
python main.py --apitest     # 16 checks: real HTTP calls verified against a fake Telegram server
```

In `--demo`, `a` = admin, `u` / `u3` / `u4` = customers, `[photo]` `[video]` `[file]` = media messages,
`#` presses an inline button, and the actor is sticky (type only the answer after `a /additem`):

```
a #s:brand                →  Ravi Premium
a #s:upi_id               →  ravi@ybl
a #setqr                  →  a [photo]            ← your real UPI QR image
a #setwelcomephoto        →  a [photo]            ← photo on the /start screen
a #newitem                                        ← or: a /additem
   Python Full Course
   199
   40 hours of lessons + project files
   a [video]
   channel: https://t.me/ravipremium
   0                                                 ← 0 = lifetime, 30 = 30 days
u /shop → u #buy:1 → u #ready:1 → u [photo] paid
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
| **🏪 Store settings** | brand name, **welcome text**, **welcome photo**, force-channel-join, empty-store note, welcome preview |
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
/start    welcome photo + text + [🛍 Browse store] [📚 My library] [🧾 My orders] [💳 Payment info] [❓ Help]
/shop     item list with prices, ✅ marks owned items, ◀️/▶️ paging, tap for the item page
💵 Buy    order created (#0001) → checkout screen: QR image, UPI id, exact amount, order id note
📸 Proof  the bot only accepts an actual photo/file — text is refused
⏳         "Proof received — waiting for admin approval"
✅         video/document delivered + link buttons (join channel / group / open link) + added to 📚 library
```
Free items (`price = 0`) unlock instantly with no order. Optional `⏱ validity` per item auto-expires access.
If `force channel join` is set, the customer must join before the bot works (make the bot an admin of that channel).

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
| top of `main.py` | `CURRENCY`, `PAGE_SIZE` (items per page), `STATE_TTL_HOURS`, `DEFAULTS` (all default texts) |
| `SCHEMA` / `add_item()` | add your own item fields (e.g. `offer_price`, `sample_file_id`) |
| `item_caption()` | how the item page looks |
| `deliver()` | how content is sent (file + link buttons + library footer) |

Notes: the bot reuses Telegram `file_id`s, so a file is uploaded **once** and delivered forever (fast, no
storage cost). Bot upload limit is 50 MB — bigger items: sell the channel/drive link instead.
`/api.telegram.org` must be reachable from the server running the bot.

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
