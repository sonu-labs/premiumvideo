# 🎬 PremiumVideo — paid content bot (Telegram)

**Ek hi file:** `main.py`. Admin video / file / photo / channel link / group link / website link / text — kuch bhi daalta hai, apna price set karta hai, apna payment QR + UPI set karta hai. User paisa bhej ke **screenshot** daalta hai → admin ke paas **Approve / Reject** button aata hai → approve karte hi content user ko.

```
Admin: /additem → price → file/link  ·  User: /shop → buy → pay → screenshot → ⏳ → ✅ content
```

---

## 1. Chalana (2 minute)

```bash
# optional: sirf QR auto-generate ke liye chahiye, bagair bhi bot chalega (stdlib only)
pip install -r requirements.txt

export BOT_TOKEN="123456789:AA_your_token_from_BotFather"
export ADMIN_IDS="123456789"          # apna Telegram numeric id (comma se 2-3 admin de sakte ho)
python main.py
```

- Token: [@BotFather](https://t.me/BotFather) → `/newbot` → token copy.
- Apna numeric id: [@userinfobot](https://t.me/userinfobot) se pata chalta hai.
- `ADMIN_IDS` na bhi do to **jo pehla insaan bot ko /start karega wahi admin ban jaayega** (phir `.env` me daal dijiye).
- DB file `premiumvideo.db` ban jaati hai (SQLite) — backup karne ke liye bas ye file copy kijiye.

### Bina token ke test (abhi)
```bash
python main.py --demo        # terminal hi Telegram: admin/user ban ke pura flow test karo
python main.py --selftest    # 50 automatic tests (buy→screenshot→approve→delivery)
python main.py --apitest     # asli HTTP requests ek nakli Telegram server pe jaanchta hai
```

`--demo` me:
```
a /brand Mera Store          ← 'a' = admin, 'u' = Ravi, 'u3' = Neha
a /upi shop@ybl
a /setqr   → phir  a [photo]  ← QR photo bhejo
a /add Python Course | 199   → phir  a [video]  → phir  0
u /shop    →  u #buy:1  →  u [photo]      ← payment screenshot
a /pending →  ✅ button (ya  :approve 1)
u /my                                      ← content mil gaya
:items :pending :users :db :reset :quit
```
(`[photo]` `[video]` `[file]` message ka type hai, `#` se inline button press hota hai, `a`/`u` likhna sticky hai — ek baar likho, aage answer type karte jao.)

---

## 2. Admin commands (poora list)

### 💳 Payment setup (ek baar)
| Command | Kaam |
|---|---|
| `/setqr` | QR ka **photo** reply kijiye → user ko payment screen pe dikhega |
| `/upi shop@ybl` | UPI ID (na ho to user ko text dikh jaata hai) |
| `/payee Ravi Classes` | UPI name (auto-QR me use hota hai) |
| `/note text` | Payment ke neeche dikhne wala note (e.g. "5 min me approval") |
| `/rules text` | User ke payment screen ka extra line |
| `/welcome text` | /start ka welcome message |
| `/brand Naam` | Store ka naam |
| `/channel @urchannel` | **Force-join** — join na kiya to bot kaam nahi karega |
| `/qr` | current QR dekho |

> Agar aapne QR upload nahi kiya, bot **UPI QR khud bana deta hai** — aur usme amount + order number bhi bhara hota hai (`upi://pay?pa=...&am=199&tr=0007`). Iske liye `qrcode` + `pillow` chahiye.

### 📦 Items (video / file / link — kuch bhi)
| Command | Kaam |
|---|---|
| `/additem` | Wizard: title → price → description → file → links → validity |
| `/add Title \| 199` | kisi **video/photo/file ke reply** me ye likho → item ban gaya |
| `/addlink Title \| 99 \| https://t.me/vip` | sirf link wala item (channel/website) |
| `/addtext Coupon \| 25 \| SERI-2026` | custom **text / serial key / note** becho |
| `/items` | manage list (tap → edit/pause/delete) |
| `/edit 3` | item #3 edit menu (title, desc, price, file, links, validity) |
| `/price 3 49` · `/valid 3 30` | price / kitne din access (0 = lifetime) |
| `/pause 3` · `/resume 3` · `/del 3` | band karo / chalu karo / hatao |

### 🧾 Orders & users
| Command | Kaam |
|---|---|
| `/pending` | pending payments ki list (✅ / ❌ buttons ke saath) |
| `/approve 12` | order #12 approve → turant content user ko |
| `/reject 12 reason likho` | reject + reason user ko dikhega |
| `/orders 12` | pura order (screenshot + buttons) |
| `/grant 5 3` | user 5 ko item 3 free de do |
| `/revoke 5 3` | access wapas lo |
| `/block 123` / `/unblock 123` | user ka access rokna |
| `/users` · `/stats` | user list, revenue/report |
| `/bc message` | sab users ko broadcast |
| `/unstick 123` | user ka atka hua step reset |
| `/settings` | current settings dump |

---

## 3. User kya karega

```
/start            → store khulta hai
/shop             → items + price (✅ = already unlocked)
buy               → QR + UPI ID + exact amount + order no.
[ screenshot ]    → user photo/file bhejta hai
⏳                → admin ke paas: user, item, amount, screenshot, [✅ Approve] [❌ Reject]
✅                → video/file + links user ko, /my me hamesha ke liye
/status  /my  /help  /cancel
```

---

## 4. Deploy (24×7)

```bash
# VPS / small server
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
BOT_TOKEN="..." ADMIN_IDS="..." ./venv/bin/python main.py
```

`systemd` (`/etc/systemd/system/premiumvideo.service`):
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
sudo systemctl enable --now premiumvideo
journalctl -u premiumvideo -f
```
Railway/Render/Koyeb pe: `python main.py` start command, `BOT_TOKEN` + `ADMIN_IDS` variables — **long polling** use hota hai, public URL/webhook ki zaroorat nahi.

---

## 5. Customization

Sab kuch `main.py` ke top pe hai:
```python
BOT_TOKEN / ADMIN_IDS / CURRENCY = "₹"   # env ya yahin edit
DEFAULTS = {...}                          # brand, notes, welcome text
PAGE_SIZE = 6                             # ek page me kitne items
MEDIA_FIELDS = {...}                      # kaunse file type support
```
- Naya field chahiye (e.g. "offer price") → `items` table me column daalo, `add_item()` + `EDIT_FIELDS` me entry.
- Telegram ko zyada bhaari file bhej sakti hai (bot limit **50 MB**). Usse badi file ho to Drive/channel link bechiye (`/addlink`).
- File ka `file_id` Telegram ka hota hai — baar-baar upload nahi hota, isliye delivery fast hai.

## 6. Troubleshooting
| Problem | Solution |
|---|---|
| `BOT_TOKEN nahi mila` | token export kijiye (ya `python main.py --token "123:abc"`) |
| Bot reply nahi karta | polling restart karo; proxy/VPN se `api.telegram.org` block ho sakta hai |
| User ko message nahi gaya | user ne bot **block** kar diya hoga — admin ko "Delivery fail" ping aata hai |
| 429 flood control | bot khud retry karta hai; `/bc` me zyada users ho to thoda ruk-ruk ke bhejte hain |
| Payment screen me QR nahi | `pip install qrcode pillow` ya `/setqr` se photo daaliye |

Flow test: `python main.py --selftest && python main.py --apitest` — dono pass ho jaayein to bot theek hai. 🚀
