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
python main.py --selftest       # 122 checks over the whole lifecycle (SQLite backend)
python main.py --selftest-mongo # the same 122 checks against the MongoDB backend
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
/shop     every item is its own button — "Title (₹199)" — plus Prev/Next paging, 💦 marks owned
💦 Buy    tapping an item goes STRAIGHT to the checkout: QR image, UPI id, exact amount,
          order id note — and a single button: [I paid — submit screenshot]
📸 Proof  the bot only accepts an actual photo/file — text is refused
⏳         "Proof received — waiting for admin approval"
💦         a professional PURCHASE SUCCESSFUL receipt: item, 💦 amount paid, 🍒 order id + date,
          💦 access validity — and the links written out in full (no hidden "Join channel" text):

          🥵 PURCHASE SUCCESSFUL 💦
          ──────────────────────────
          🍆 Rody

          💦 You paid: ₹400
          🍒 Order ID: #0001 · 18 Sep, 11:16
          💦 Access: Lifetime
          ──────────────────────────
          👅 Main link: https://t.me/yourchannel/123
          👅 Channel: https://t.me/yourchannel

          😘 Thank you for shopping with Premium Video!
          Your content is ready — enjoy 💦
          ▸ [Open link · ₹400] [Join channel · ₹400] [My library] [Buy something else]
```
Free items (`price = 0`) unlock instantly with no order. Optional `⏱ validity` per item auto-expires access.
If `force channel join` is set, the customer must join before the bot works (make the bot an admin of that channel).

### Premium emoji (custom emoji + colored buttons)

Every message and every button icon uses **Telegram premium (custom) emoji** — in message text
(`<tg-emoji>`) and as **button icons** (`icon_custom_emoji_id`), plus **colored buttons** everywhere
(`style`: `success` green / `primary` blue / `danger` red — Bot API 9.4). This needs the **bot owner
to have Telegram Premium** (or a Fragment username on the bot). `PREMIUM_EMOJI=0` forces plain emoji.

**Customer side — only ten animated emoji, everywhere.** Whatever the screen, a buyer only ever sees:

| emoji | used for | emoji | used for |
|---|---|---|---|
| 💦 | paid · success · instant · money | 🍒 | orders · library · receipt |
| 🍑 | store · browse · price | 🍆 | video · item · file · content |
| 🥵 | hot · premium · warning · locked | 🍭 | free · bonus · note · waiting |
| 🌸 | profile · account · neutral | 😘 | thanks · support · friendly |
| 👅 | links · external · preview | 😄 | help · how-to · welcome |

Any other emoji is swapped for the closest one from this set automatically (`ue()` in `main.py`), so
the shop can never look inconsistent again — this includes emoji that came from an item title.
The admin's own **welcome text** and **broadcast text** go through the same swap, so even a
message the owner typed by hand arrives in the shop's own animated set.

**Admin side — no plain emoji left.** Admin screens use their own animated set (📦 ⚙️ 💳 👥 ✅ ❌ 📣 📊…)
and every emoji that has no id in the folder gets a *similar* animated one instead
(`ADMIN_EMOJI_FALLBACK` in `main.py`), so a panel never shows a static emoji next to animated ones.

**Emoji IDs** come from the `ADMIN PANEL EMOJI ID/` folder — every JSON `.txt` / `.json` file there
(`[{"emoji": "📦", "custom_emoji_id": "…"}]`) is read at startup:

* a **normal file** (`KripanshEmojis…`, `ToastEmoji`, `tgiosicons`…) → admin + shared screens
* a file whose name starts with **`UserSide`** (`UserSideEmojis.txt` = the owner's own list) →
  the **customer side**, and it always wins over the other files, so the shop keeps exactly the
  animated emoji you picked. Delete it and the built-in set in `main.py` is used instead.

**If Telegram rejects a custom emoji** (bot lost Premium, stale id…) the message is retried once with
plain emoji and then custom emoji stay off for `PREMIUM_EMOJI_COOLDOWN` seconds (default 600) — so a
single rejection never turns into a per-message retry that makes the bot feel slow.

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
| env `PREMIUM_EMOJI_COOLDOWN` | seconds to stop sending custom emoji after Telegram rejects one (default 600, 0 = never back off) |
| store caches | settings snapshot 60 s + access rows 5 s (`SETTINGS_TTL` / `UNLOCK_TTL` on the store class) — writes invalidate them instantly |
| `PremiumBot.MEMBER_TTL` | how long a force-join check (`getChatMember`) is cached (default 120 s) |
| `PremiumBot.SLOW_UPDATE` | log a `🐢 slow update` line when one update takes longer than this (default 2.5 s) |
| top of `main.py` | `PEMOJI` (premium emoji → id map), `CURRENCY`, `PAGE_SIZE` (items per page), `STATE_TTL_HOURS`, `DEFAULTS` (all default texts) |
| `SCHEMA` / `add_item()` | add your own item fields (e.g. `offer_price`, `sample_file_id`) |
| `item_caption()` | how the item page looks |
| `deliver()` | how content is sent (file + link buttons + library footer) |

Notes: the bot reuses Telegram `file_id`s, so a file is uploaded **once** and delivered forever (fast, no
storage cost). Bot upload limit is 50 MB — bigger items: sell the channel/drive link instead.
`/api.telegram.org` must be reachable from the server running the bot. The storage layer is pluggable
(`SQLiteStore` / `MongoStore` at the top of `main.py`) — every screen works identically on both backends.

**Speed (why it feels fast):** one keep-alive `requests.Session` for all Telegram calls (no fresh TLS
handshake per message), a cached settings snapshot instead of 5-10 setting queries per screen, cached
access rows, a cached force-join check (no `getChatMember` on every tap), one shared SQLite connection
(WAL + `synchronous=NORMAL`) or one settings/access cache for MongoDB, and the premium-emoji cooldown
above. If a reply still feels slow, the log prints `🐢 slow update took 3.1s` — that is the network to
Telegram (or a far-away MongoDB), not the bot logic.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `BOT_TOKEN is missing` | export the token, or `python main.py --token "123:abc"` |
| bot silent | restart; check that the host can reach `api.telegram.org` (proxy/VPN can block it) |
| customer got no content after approval | they blocked the bot — the admin sees a "Delivery failed" notice, use 🔁 Re-deliver later |
| QR image missing at checkout | `pip install qrcode pillow`, or upload your own with **💳 Payment setup → 🖼 Upload QR** |
| stuck in a wizard step | `/cancel` (customer) or `/unstick <telegram id>` (admin) |
| bot replies feel slow | check `🐢 slow update` lines in the log; keep-alive + the caches above are already on — a VPS closer to Telegram (or SQLite instead of a distant MongoDB) is the next step |
| emoji show as plain ones | the bot owner needs Telegram Premium (or a Fragment username); after a rejection the bot goes plain for `PREMIUM_EMOJI_COOLDOWN` seconds |
| but I want the old hidden "Join channel" link | `deliver()` in `main.py` — the `link_lines` block is the only place that decides how links are printed |

```bash
python main.py --selftest && python main.py --apitest     # both green ⇒ the bot is healthy
```
