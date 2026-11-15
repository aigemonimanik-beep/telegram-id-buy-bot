#!/usr/bin/env python3
# HOSTING COPY — for Render (free) hosting. Same bot as ~/workspace/telegram-bot-buy/bot.py,
# with one change only: a tiny health server answers / and /ping on $PORT so Render +
# UptimeRobot can keep the free service awake. The vault import was already optional
# in the buy bot (Render has no vault; the real token comes from the
# TELEGRAM_BUY_BOT_TOKEN env var set in Render's dashboard).
# Never put the token in this file — set it in Render.
"""Telegram ID-BUYING bot (MarKetNova) — the other side of the ID business.

The user BUYS IDs from sellers (users) through this bot. Sellers submit
IDs (Google Sheet CSV/.xlsx, or pasted text lines); the admin checks them
and marks how many are good. Good IDs are paid for at the category price
per ID and the seller's balance is credited automatically.

Stdlib-only polling bot. The BotFather token is NEVER stored in a file:
it comes from the environment (TELEGRAM_BUY_BOT_TOKEN) or, when set up,
from Muse's Secure Vault as the custom.telegram_buy connection, reached
through the dynamic-credential surrogate, which authd swaps for the real
token on approved egress to api.telegram.org. The bot does NOT start
polling when no token is configured.

How it works (Bengali UI, persistent reply keyboard):
  Sellers (home page buttons, always visible):
    Sell IDs       - pick an ACTIVE category, then send a CSV/.xlsx file
                     or paste lines (user | password); one submission
                     (SUB-1001, ...) is created and waits for review
    My balance     - balance, total earned, total IDs sold, good/bad
    My submissions - their submissions with status + good/bad counts
    Rules          - rules text set by the admin
    Help
  Paused category: not listed for selling; selling there is refused with
  "এই ক্যাটাগরির আইডি নেওয়া এখন বন্ধ আছে".
  Admin (separate Admin Panel button, admin only):
    Add category   - name + price per ID (what a seller is paid per good ID)
    Edit category  - change name / price
    Pause/resume   - stop or restart taking IDs for a category
    Set rules      - the rules text sellers see
    View pending   - pending submissions, then review one
    Review         - admin enters the good count (bad = total - good),
                     seller is credited good x price and notified
    Give balance   - credit a seller's balance manually
    Master sheet   - totals per category + grand total
    Today's report - today's submissions, good/bad, paid total
    Export master  - the master sheet as a CSV file (admin only)

SECURITY — passwords:
  Passwords live ONLY in master.json on disk and in the admin-only CSV
  export. They are NEVER shown in any chat message: the review screen
  shows usernames only, and the seller's report shows counts only.

REVIEW RULE (important):
  The bot cannot tell WHICH submitted IDs are good — only HOW MANY.
  So when the admin says N of M are good, the FIRST N rows in sheet
  order are marked good and the rest are marked bad. Tell the admin
  this whenever a review is confirmed.

Data files (in this folder):
  categories.json  - buy categories: {id, name, price, active}
  submissions.json - one record per seller submission (SUB-1001, ...)
  master.json      - one row per submitted ID (username, password,
                     seller, category, date, status, price paid)
  balances.json    - seller balances: {chat_id: amount}
  config.json      - shop name, rules text, admin_chat_id
  state.json       - per-user sell/admin steps + seen users
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

# The vault helper is optional: the bot must import and run its offline
# tests with no token and no vault at all. When the helper exists, the
# surrogate path below is used as a fallback to the env token.
try:
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import dynamic_credential_entry  # noqa: E402
    _VAULT_AVAILABLE = True
except Exception:  # pragma: no cover - vault helper missing
    dynamic_credential_entry = None  # type: ignore[assignment]
    _VAULT_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATEGORIES_FILE = os.path.join(BASE_DIR, "categories.json")
SUBMISSIONS_FILE = os.path.join(BASE_DIR, "submissions.json")
MASTER_FILE = os.path.join(BASE_DIR, "master.json")
BALANCES_FILE = os.path.join(BASE_DIR, "balances.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

CREDENTIAL_NAME = "custom.telegram_buy"
ALLOWED_HOSTS = ["api.telegram.org"]
ADMIN_CHAT_ID = 7570899892
DHAKA = timezone(timedelta(hours=6))

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


# ---------------------------------------------------------------- API layer

_TOKEN_RE = re.compile(r"/bot[^/\s]+/")


def _redact(text: str) -> str:
    """Strip any bot token / surrogate from text before it is logged."""
    return _TOKEN_RE.sub("/bot***/", text)


_ENV_TOKEN = os.environ.get("TELEGRAM_BUY_BOT_TOKEN", "").strip()


def _token_or_surrogate() -> str:
    """The token (env) or the vault surrogate (unquoted, never encoded).

    Sentinel detects the literal ``hsurr:...`` token in the path and swaps
    in the real credential on approved egress. Percent-encoding it breaks
    detection and Telegram then answers 404, so the surrogate goes in raw.
    Raises RuntimeError when no token source is configured at all.
    """
    if _ENV_TOKEN:
        return _ENV_TOKEN
    if _VAULT_AVAILABLE and dynamic_credential_entry is not None:
        entry = dynamic_credential_entry(CREDENTIAL_NAME)
        return str(entry["surrogate"]).strip()
    raise RuntimeError(
        "কোনো টোকেন নেই: TELEGRAM_BUY_BOT_TOKEN সেট করুন অথবা "
        "custom.telegram_buy ভল্ট সংযোগ তৈরি করুন।"
    )


def _api_base() -> str:
    return f"https://api.telegram.org/bot{_token_or_surrogate()}"


def _http_post(url: str, data: bytes, content_type: str, timeout: float = 60.0) -> dict:
    """One POST to Telegram; returns the parsed JSON. Raises on failure."""
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", content_type)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Telegram HTTP {exc.code}: {_redact(body)}"
        ) from exc
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API failed: {_redact(str(result))}")
    return result["result"]


def api(method: str, payload: dict | None = None, timeout: float = 40.0) -> dict:
    """Call one Telegram Bot API method. Raises on failure."""
    url = f"{_api_base()}/{method}"
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    return _http_post(url, data or b"", "application/json", timeout=timeout)


def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api("sendMessage", payload)


def send_document(chat_id: int, filename: str, data: bytes,
                  caption: str | None = None) -> dict:
    """Send a file (e.g. the master-sheet CSV) to one chat (admin only)."""
    boundary = "----buybot" + uuid.uuid4().hex
    body = io.BytesIO()

    def field(name: str, value: str) -> None:
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.write(f"{value}\r\n".encode())

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption)
        field("parse_mode", "HTML")
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="document"; '
        f'filename="{filename}"\r\n'.encode()
    )
    body.write(b"Content-Type: text/csv\r\n\r\n")
    body.write(data)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    url = f"{_api_base()}/sendDocument"
    return _http_post(url, body.getvalue(),
                      f"multipart/form-data; boundary={boundary}", timeout=60.0)


def answer_callback(callback_query_id: str, text: str = "") -> None:
    try:
        api("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
    except Exception:
        pass  # non-fatal


def edit_message(chat_id: int, message_id: int, text: str,
                 reply_markup: dict | None = None) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        api("editMessageText", payload)
    except Exception:
        send_message(chat_id, text, reply_markup)


def download_file(file_id: str) -> bytes:
    """Download a file the user sent to the bot (via getFile + file URL)."""
    info = api("getFile", {"file_id": file_id})
    path = info["file_path"]
    url = f"https://api.telegram.org/file/bot{_token_or_surrogate()}/{path}"
    request = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=60) as response:
        return response.read()


# ---------------------------------------------------------------- data layer

def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, value) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_categories() -> list[dict]:
    return load_json(CATEGORIES_FILE, [])


def save_categories(cats: list[dict]) -> None:
    save_json(CATEGORIES_FILE, cats)


def active_categories() -> list[dict]:
    """Categories sellers may sell into (paused ones are hidden)."""
    return [c for c in load_categories() if c.get("active", True)]


def load_submissions() -> list[dict]:
    return load_json(SUBMISSIONS_FILE, [])


def save_submissions(subs: list[dict]) -> None:
    save_json(SUBMISSIONS_FILE, subs)


def load_master() -> list[dict]:
    return load_json(MASTER_FILE, [])


def save_master(rows: list[dict]) -> None:
    save_json(MASTER_FILE, rows)


def load_balances() -> dict:
    return load_json(BALANCES_FILE, {})


def save_balances(balances: dict) -> None:
    save_json(BALANCES_FILE, balances)


def get_balance(chat_id: int) -> int:
    return int(load_balances().get(str(chat_id), 0))


def add_balance(chat_id: int, amount: int) -> int:
    balances = load_balances()
    balances[str(chat_id)] = int(balances.get(str(chat_id), 0)) + amount
    save_balances(balances)
    return balances[str(chat_id)]


def load_config() -> dict:
    cfg = load_json(CONFIG_FILE, {})
    cfg.setdefault("admin_chat_id", ADMIN_CHAT_ID)
    cfg.setdefault("shop_name", "MarKetNova — আইডি কেনা")
    cfg.setdefault("rules", "")
    return cfg


def save_config(config: dict) -> None:
    save_json(CONFIG_FILE, config)


def load_state() -> dict:
    return load_json(STATE_FILE, {"sell": {}, "admin": {}, "users": {}})


def save_state(state: dict) -> None:
    save_json(STATE_FILE, state)


def now_dhaka() -> datetime:
    return datetime.now(DHAKA)


def today_str() -> str:
    return now_dhaka().strftime("%Y-%m-%d")


def stamp() -> str:
    return now_dhaka().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- helpers

def esc(text) -> str:
    return html.escape(str(text))


def tk(amount) -> str:
    return f"৳{amount:,}"


def parse_number(text: str) -> int | None:
    cleaned = text.strip().translate(BN_DIGITS).replace(",", "").replace(" ", "")
    if not cleaned.isdigit():
        return None
    return int(cleaned)


_HEADER_WORDS = {
    "user", "username", "user name", "email", "mail", "id", "account",
    "userid", "user id", "login", "password", "pass", "pwd",
    "ইউজার", "পাসওয়ার্ড",
}


def _is_header_row(cells: list[str]) -> bool:
    lowered = [c.strip().lower() for c in cells[:2]]
    return len(lowered) >= 2 and all(w in _HEADER_WORDS for w in lowered)


def parse_account_line(line: str) -> dict | None:
    """One account from a line: 'user | pass', 'user,pass', 'user:pass'
    or 'user pass'. Returns None when the line has no usable pair."""
    line = line.strip()
    if not line:
        return None
    if "," in line:
        try:
            parts = next(csv.reader([line]))
        except Exception:
            parts = line.split(",")
    elif "|" in line:
        parts = line.split("|")
    elif ":" in line:
        parts = line.split(":", 1)
    else:
        parts = line.split()
    parts = [p.strip() for p in parts]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return {"user": parts[0], "pass": parts[1]}


def parse_accounts_text(text: str) -> list[dict]:
    """Accounts from plain text (CSV / TXT): one account per line."""
    accounts: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        if _is_header_row([p.strip() for p in line.replace("|", ",").split(",")][:2]):
            continue
        acc = parse_account_line(line)
        if acc:
            key = (acc["user"], acc["pass"])
            if key not in seen:
                seen.add(key)
                accounts.append(acc)
    return accounts


def parse_xlsx_rows(data: bytes) -> list[list[str]]:
    """Rows of the first sheet of an .xlsx file (stdlib only)."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    zf = zipfile.ZipFile(io.BytesIO(data))
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.findall(".//m:t", ns)))
    sheets = sorted(n for n in zf.namelist()
                    if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    if not sheets:
        return []
    root = ET.fromstring(zf.read(sheets[0]))
    rows: list[list[str]] = []
    for row in root.findall(".//m:row", ns):
        cells: dict[str, str] = {}
        for c in row.findall("m:c", ns):
            ref = c.get("r", "")
            col = re.sub(r"\d+$", "", ref)
            v = c.find("m:v", ns)
            is_elem = c.find("m:is", ns)
            if c.get("t") == "s" and v is not None and v.text:
                val = shared[int(v.text)] if v.text.isdigit() and int(v.text) < len(shared) else ""
            elif is_elem is not None:
                val = "".join(t.text or "" for t in is_elem.findall(".//m:t", ns))
            elif v is not None:
                val = v.text or ""
            else:
                val = ""
            cells[col] = val
        if cells:
            rows.append([cells[k] for k in sorted(cells)])
    return rows


def parse_accounts(data: bytes, filename: str = "") -> list[dict]:
    """Accounts from an uploaded file: .xlsx (Google Sheets export),
    .csv / .tsv / .txt. First column = user/email, second = password.
    A header row is skipped; duplicate pairs are dropped."""
    name = (filename or "").lower()
    if name.endswith(".xlsx") or data[:2] == b"PK":
        accounts: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for cells in parse_xlsx_rows(data):
            if len(cells) < 2:
                continue
            if _is_header_row(cells):
                continue
            user, password = cells[0].strip(), cells[1].strip()
            if user and password and (user, password) not in seen:
                seen.add((user, password))
                accounts.append({"user": user, "pass": password})
        return accounts
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return parse_accounts_text(text)


def is_admin(chat_id: int, config: dict) -> bool:
    admin_id = config.get("admin_chat_id") or ADMIN_CHAT_ID
    return bool(admin_id) and int(admin_id) == int(chat_id)


# ---------------------------------------------------------------- core logic
# These functions only touch the JSON data files — they never call the
# Telegram API, so the offline tests exercise them directly.

def next_category_id(cats: list[dict]) -> int:
    return max([c.get("id", 0) for c in cats], default=0) + 1


def add_category(name: str, price: int) -> dict:
    cats = load_categories()
    cat = {"id": next_category_id(cats), "name": name.strip(),
           "price": int(price), "active": True}
    cats.append(cat)
    save_categories(cats)
    return cat


def set_category_active(cat_id: int, active: bool) -> dict | None:
    cats = load_categories()
    for c in cats:
        if c["id"] == cat_id:
            c["active"] = active
            save_categories(cats)
            return c
    return None


def category_sellable(cat_id: int) -> tuple[dict | None, str | None]:
    """Return (category, error). A paused category cannot take IDs."""
    cat = next((c for c in load_categories() if c["id"] == cat_id), None)
    if not cat:
        return None, "এই ক্যাটাগরিটি আর নেই।"
    if not cat.get("active", True):
        return None, "এই ক্যাটাগরির আইডি নেওয়া এখন বন্ধ আছে।"
    return cat, None


def next_submission_id(subs: list[dict]) -> str:
    nums = []
    for s in subs:
        m = re.match(r"SUB-(\d+)$", s.get("id", ""))
        if m:
            nums.append(int(m.group(1)))
    return f"SUB-{max(nums, default=1000) + 1}"


def next_master_id(rows: list[dict]) -> int:
    return max([r.get("id", 0) for r in rows], default=0) + 1


def create_submission(seller_id: int, seller_name: str, seller_username: str,
                      cat: dict, accounts: list[dict]) -> dict:
    """Save one pending submission and one master row per ID.

    Passwords are stored in master.json only — never in the submission
    record and never in any chat message.
    """
    subs = load_submissions()
    master = load_master()
    sub_id = next_submission_id(subs)
    created = stamp()
    sub = {
        "id": sub_id,
        "seller_id": int(seller_id),
        "seller_name": seller_name,
        "seller_username": seller_username or "",
        "category_id": cat["id"],
        "category_name": cat["name"],
        "price": int(cat["price"]),
        "total": len(accounts),
        "created_at": created,
        "date": created[:10],
        "status": "pending",
        "good": None,
        "bad": None,
        "paid": 0,
    }
    subs.append(sub)
    save_submissions(subs)
    mid = next_master_id(master)
    for acc in accounts:
        master.append({
            "id": mid,
            "submission_id": sub_id,
            "username": acc["user"],
            "pass": acc["pass"],
            "seller_id": int(seller_id),
            "seller_name": seller_name,
            "category_id": cat["id"],
            "category_name": cat["name"],
            "date": created[:10],
            "created_at": created,
            "status": "pending",
            "price_paid": 0,
        })
        mid += 1
    save_master(master)
    return sub


def apply_review(sub_id: str, good_count: int) -> dict:
    """Review a pending submission.

    good_count must be between 0 and the submission total. The seller is
    credited good x category price. Master rows are updated in SHEET
    ORDER: the first `good_count` rows become good, the rest become bad.
    The bot cannot know WHICH specific IDs are good — only how many —
    so this ordering is the documented rule (see module docstring).

    Returns the updated submission. Raises ValueError on bad input.
    """
    subs = load_submissions()
    sub = next((s for s in subs if s["id"] == sub_id), None)
    if not sub:
        raise ValueError("এই জমাটি পাওয়া যায়নি।")
    if sub.get("status") != "pending":
        raise ValueError("এই জমাটি আগেই যাচাই করা হয়েছে।")
    total = int(sub["total"])
    if good_count < 0 or good_count > total:
        raise ValueError(f"ভালো আইডির সংখ্যা ০ থেকে {total}-এর মধ্যে হতে হবে।")
    bad_count = total - good_count
    paid = good_count * int(sub["price"])

    master = load_master()
    rows = [r for r in master if r.get("submission_id") == sub_id]
    rows.sort(key=lambda r: r.get("id", 0))
    for i, row in enumerate(rows):
        if i < good_count:
            row["status"] = "good"
            row["price_paid"] = int(sub["price"])
        else:
            row["status"] = "bad"
            row["price_paid"] = 0
    save_master(master)

    sub["status"] = "reviewed"
    sub["good"] = good_count
    sub["bad"] = bad_count
    sub["paid"] = paid
    save_submissions(subs)

    if paid > 0:
        add_balance(int(sub["seller_id"]), paid)
    return sub


def give_balance_to(user_id: int, amount: int) -> int:
    """Credit a seller's balance manually (admin). Returns new balance."""
    if amount <= 0:
        raise ValueError("টাকার পরিমাণ ০-এর বেশি হতে হবে।")
    return add_balance(int(user_id), int(amount))


def seller_stats(seller_id: int) -> dict:
    """Balance, total earned (review payouts), IDs sold, good/bad totals."""
    master = load_master()
    rows = [r for r in master if int(r.get("seller_id", 0)) == int(seller_id)]
    subs = load_submissions()
    earned = sum(int(s.get("paid", 0)) for s in subs
                 if int(s.get("seller_id", 0)) == int(seller_id)
                 and s.get("status") == "reviewed")
    return {
        "balance": get_balance(seller_id),
        "earned": earned,
        "total_ids": len(rows),
        "good": sum(1 for r in rows if r.get("status") == "good"),
        "bad": sum(1 for r in rows if r.get("status") == "bad"),
        "pending": sum(1 for r in rows if r.get("status") == "pending"),
    }


def master_totals() -> dict:
    """Per-category totals + grand total for the master sheet."""
    master = load_master()
    per_cat: dict[str, dict] = {}
    for r in master:
        name = r.get("category_name", "?")
        entry = per_cat.setdefault(name, {
            "total": 0, "good": 0, "bad": 0, "pending": 0, "paid": 0,
        })
        entry["total"] += 1
        entry[r.get("status", "pending")] += 1
        entry["paid"] += int(r.get("price_paid", 0))
    grand = {"total": 0, "good": 0, "bad": 0, "pending": 0, "paid": 0}
    for entry in per_cat.values():
        for key in grand:
            grand[key] += entry[key]
    return {"per_category": per_cat, "grand": grand}


def today_report() -> dict:
    """Today's submissions (Dhaka date): counts, good/bad, paid total."""
    today = today_str()
    subs = [s for s in load_submissions() if s.get("date") == today]
    return {
        "date": today,
        "submissions": len(subs),
        "total_ids": sum(int(s.get("total", 0)) for s in subs),
        "good": sum(int(s.get("good") or 0) for s in subs
                    if s.get("status") == "reviewed"),
        "bad": sum(int(s.get("bad") or 0) for s in subs
                   if s.get("status") == "reviewed"),
        "paid": sum(int(s.get("paid", 0)) for s in subs
                    if s.get("status") == "reviewed"),
        "pending": sum(1 for s in subs if s.get("status") == "pending"),
    }


MASTER_CSV_HEADERS = [
    "তারিখ", "জমা নম্বর", "বিক্রেতা", "বিক্রেতা আইডি", "ক্যাটাগরি",
    "ইউজারনেম", "পাসওয়ার্ড", "অবস্থা", "পাওনা (৳)",
]

STATUS_BN = {"pending": "যাচাই চলছে", "good": "ভালো", "bad": "খারাপ"}


def master_csv_bytes() -> bytes:
    """The full master sheet as CSV (all columns, admin export only).

    UTF-8 with BOM so Excel opens the Bengali headers correctly.
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(MASTER_CSV_HEADERS)
    for r in sorted(load_master(), key=lambda x: x.get("id", 0)):
        writer.writerow([
            r.get("date", ""),
            r.get("submission_id", ""),
            r.get("seller_name", ""),
            r.get("seller_id", ""),
            r.get("category_name", ""),
            r.get("username", ""),
            r.get("pass", ""),
            STATUS_BN.get(r.get("status", "pending"), r.get("status", "")),
            r.get("price_paid", 0),
        ])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


# ---------------------------------------------------------------- texts & keyboards

BTN_SELL = "📤 আইডি বিক্রি করুন"
BTN_BALANCE = "💰 আমার ব্যালেন্স"
BTN_MYSUBS = "📋 আমার জমা"
BTN_RULES = "📜 নিয়ম"
BTN_HELP = "❓ সাহায্য"
BTN_ADMIN = "👑 অ্যাডমিন প্যানেল"
BTN_HOME = "🏠 হোম"

ABTN_ADD_CAT = "➕ ক্যাটাগরি যোগ করুন"
ABTN_EDIT_CAT = "✏️ ক্যাটাগরি এডিট"
ABTN_TOGGLE = "⏸️ ক্যাটাগরি বন্ধ/চালু"
ABTN_RULES = "📜 নিয়ম বসান"
ABTN_PENDING = "📥 জমা দেখুন"
ABTN_REVIEW = "✅ যাচাই করুন"
ABTN_GIVEBAL = "💰 ব্যালেন্স দিন"
ABTN_MASTER = "📊 মাস্টার শিট"
ABTN_TODAY = "🧾 আজকের হিসাব"
ABTN_EXPORT = "📤 মাস্টার শিট ডাউনলোড"

WELCOME = (
    "👋 <b>আসসালামু আলাইকুম! {shop}–এ স্বাগতম</b>\n\n"
    "এখানে আপনি আপনার আইডি <b>বিক্রি</b> করতে পারবেন।\n"
    "আইডি জমা দিন, যাচাই হলে ভালো আইডির টাকা\n"
    "আপনার ব্যালেন্সে যোগ হয়ে যাবে। 💰\n\n"
    "💰 আপনার ব্যালেন্স: <b>{balance}</b>\n\n"
    "👇 নিচের বাটনগুলো সব সময় থাকবে, বার বার /start দিতে হবে না।\n\n"
    "নিচ থেকে বেছে নিন:"
)

HELP_TEXT = (
    "❓ <b>কীভাবে আইডি বিক্রি করবেন</b>\n\n"
    "1️⃣ 📤 আইডি বিক্রি করুন → ক্যাটাগরি বেছে নিন\n"
    "2️⃣ গুগল শিট থেকে ডাউনলোড করা <b>CSV / Excel (.xlsx) ফাইল</b> পাঠান,\n"
    "অথবা লিখে পাঠান — প্রতি লাইনে একটি করে:\n"
    "<code>username | password</code>\n"
    "3️⃣ জমা নম্বর (যেমন SUB-1001) পাবেন — যাচাই চলছে ⏳\n"
    "4️⃣ যাচাই শেষ হলে ভালো আইডির টাকা আপনার 💰 ব্যালেন্সে যোগ হবে,\n"
    "আর রিপোর্ট মেসেজে জানিয়ে দেওয়া হবে।\n\n"
    "📜 নিয়ম দেখতে 📜 নিয়ম বাটনে চাপ দিন।\n"
    "❓ কোনো সমস্যা হলে 🏠 হোম বাটনে চাপ দিন।"
)

DEFAULT_RULES = "এখনো কোনো নিয়ম বসানো হয়নি।"


def home_text(config: dict, chat_id: int) -> str:
    shop = config.get("shop_name") or "MarKetNova — আইডি কেনা"
    return WELCOME.format(shop=shop, balance=tk(get_balance(chat_id)))


def reply_keyboard_markup(config: dict, chat_id: int) -> dict:
    rows = [
        [{"text": BTN_SELL}],
        [{"text": BTN_BALANCE}, {"text": BTN_MYSUBS}],
        [{"text": BTN_RULES}, {"text": BTN_HELP}],
    ]
    if is_admin(chat_id, config):
        rows.append([{"text": BTN_ADMIN}])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


def admin_keyboard_markup() -> dict:
    return {"keyboard": [
        [{"text": ABTN_ADD_CAT}],
        [{"text": ABTN_EDIT_CAT}, {"text": ABTN_TOGGLE}],
        [{"text": ABTN_RULES}],
        [{"text": ABTN_PENDING}, {"text": ABTN_REVIEW}],
        [{"text": ABTN_GIVEBAL}],
        [{"text": ABTN_MASTER}, {"text": ABTN_TODAY}],
        [{"text": ABTN_EXPORT}],
        [{"text": BTN_HOME}],
    ], "resize_keyboard": True, "is_persistent": True}


def admin_menu() -> dict:
    return {"inline_keyboard": [
        [{"text": "➕ ক্যাটাগরি যোগ করুন", "callback_data": "adm:addcat"}],
        [{"text": "✏️ ক্যাটাগরি এডিট", "callback_data": "adm:editcat"},
         {"text": "⏸️ ক্যাটাগরি বন্ধ/চালু", "callback_data": "adm:toggle"}],
        [{"text": "📜 নিয়ম বসান", "callback_data": "adm:rules"}],
        [{"text": "📥 জমা দেখুন", "callback_data": "adm:pending"},
         {"text": "✅ যাচাই করুন", "callback_data": "adm:review"}],
        [{"text": "💰 ব্যালেন্স দিন", "callback_data": "adm:givebalance"}],
        [{"text": "📊 মাস্টার শিট", "callback_data": "adm:master"},
         {"text": "🧾 আজকের হিসাব", "callback_data": "adm:today"}],
        [{"text": "📤 মাস্টার শিট ডাউনলোড", "callback_data": "adm:export"}],
        [{"text": "🏠 হোম", "callback_data": "home"}],
    ]}


def main_menu(config: dict, chat_id: int) -> dict:
    rows = [
        [{"text": "📤 আইডি বিক্রি করুন", "callback_data": "sell"}],
        [{"text": "💰 আমার ব্যালেন্স", "callback_data": "balance"},
         {"text": "📋 আমার জমা", "callback_data": "mysubs"}],
        [{"text": "📜 নিয়ম", "callback_data": "rules"},
         {"text": "❓ সাহায্য", "callback_data": "help"}],
    ]
    if is_admin(chat_id, config):
        rows.append([{"text": "👑 অ্যাডমিন প্যানেল", "callback_data": "admin"}])
    return {"inline_keyboard": rows}


def pick_category_keyboard(prefix: str, cats: list[dict],
                           show_status: bool = False) -> dict:
    rows = []
    for c in cats:
        label = c["name"]
        if show_status:
            label += " ⏸️ বন্ধ" if not c.get("active", True) else " ▶️ চালু"
        else:
            label += f" — {tk(c['price'])}/পিস"
        rows.append([{"text": label, "callback_data": f"{prefix}:{c['id']}"}])
    rows.append([{"text": "⬅️ অ্যাডমিন প্যানেল", "callback_data": "admin"}])
    return {"inline_keyboard": rows}


def admin_required(chat_id: int, config: dict) -> bool:
    if is_admin(chat_id, config):
        return True
    send_message(chat_id, "⛔ এটা শুধু অ্যাডমিন ব্যবহার করতে পারবেন।")
    return False


def show_admin(chat_id: int, message_id: int | None = None) -> None:
    text = "👑 <b>অ্যাডমিন প্যানেল</b>\n\nনিচ থেকে বেছে নিন:"
    if message_id:
        edit_message(chat_id, message_id, text, admin_menu())
    else:
        send_message(chat_id, text, admin_menu())


# ---------------------------------------------------------------- seller handlers

def show_sell_categories(chat_id: int, message_id: int | None = None) -> None:
    cats = active_categories()
    if not cats:
        text = "😔 এখন কোনো ক্যাটাগরিতে আইডি নেওয়া হচ্ছে না। পরে আবার দেখুন।"
        kb = {"inline_keyboard": [[{"text": "🏠 হোম", "callback_data": "home"}]]}
        if message_id:
            edit_message(chat_id, message_id, text, kb)
        else:
            send_message(chat_id, text, kb)
        return
    text = (
        "📤 <b>আইডি বিক্রি করুন</b>\n\n"
        "কোন ক্যাটাগরির আইডি বিক্রি করবেন? একটিতে চাপ দিন:\n"
        "(প্রতিটি ভালো আইডির জন্য এই দাম পাবেন)"
    )
    rows = [[{"text": f"{c['name']} — {tk(c['price'])}/পিস",
              "callback_data": f"sellcat:{c['id']}"}] for c in cats]
    rows.append([{"text": "🏠 হোম", "callback_data": "home"}])
    kb = {"inline_keyboard": rows}
    if message_id:
        edit_message(chat_id, message_id, text, kb)
    else:
        send_message(chat_id, text, kb)


def start_sell(chat_id: int, message_id: int | None, cat_id: int) -> None:
    cat, err = category_sellable(cat_id)
    if err:
        if message_id:
            edit_message(chat_id, message_id, f"⛔ {err}")
        else:
            send_message(chat_id, f"⛔ {err}")
        return
    state = load_state()
    state.setdefault("sell", {})[str(chat_id)] = {
        "step": "send_ids", "data": {"cat_id": cat_id},
    }
    save_state(state)
    text = (
        f"📤 <b>{esc(cat['name'])}</b> — ভালো প্রতিটি আইডির দাম: <b>{tk(cat['price'])}</b>\n\n"
        "আইডি পাঠান — দুইভাবে পারবেন:\n\n"
        "1️⃣ গুগল শিট থেকে ডাউনলোড করা <b>CSV / Excel (.xlsx) ফাইল</b> পাঠান।\n"
        "ফাইলে প্রথম কলামে ইউজার/ইমেইল, দ্বিতীয় কলামে পাসওয়ার্ড রাখুন।\n\n"
        "2️⃣ লিখে পাঠান, প্রতি লাইনে একটি করে:\n"
        "<code>username | password</code>\n\n"
        "⏳ জমা দিলেই যাচাই শুরু হবে।"
    )
    if message_id:
        edit_message(chat_id, message_id, text)
    else:
        send_message(chat_id, text)


def finish_sell(chat_id: int, msg: dict, accounts: list[dict]) -> bool:
    """Create the submission from parsed accounts. Returns True if saved."""
    state = load_state()
    flow = state.get("sell", {}).get(str(chat_id))
    if not flow or flow.get("step") != "send_ids":
        return False
    cat_id = flow["data"]["cat_id"]
    cat, err = category_sellable(cat_id)
    if err:
        state.get("sell", {}).pop(str(chat_id), None)
        save_state(state)
        send_message(chat_id, f"⛔ {err}")
        return True
    if not accounts:
        send_message(
            chat_id,
            "😔 কোনো আইডি পেলাম না।\n\n"
            "ফাইল পাঠান, অথবা প্রতি লাইনে একটি করে লিখুন:\n"
            "<code>username | password</code>",
        )
        return True
    sender = msg.get("from", {})
    name = sender.get("first_name", "") or str(chat_id)
    if sender.get("last_name"):
        name += " " + sender["last_name"]
    sub = create_submission(chat_id, name, sender.get("username", ""), cat, accounts)
    state.get("sell", {}).pop(str(chat_id), None)
    save_state(state)
    send_message(
        chat_id,
        f"✅ <b>জমা হয়েছে!</b>\n\n"
        f"🧾 জমা নম্বর: <b>{sub['id']}</b>\n"
        f"🆔 ক্যাটাগরি: {esc(cat['name'])}\n"
        f"📦 আইডি: <b>{len(accounts)}টি</b>\n"
        f"💰 ভালো প্রতিটি আইডির দাম: {tk(cat['price'])}\n\n"
        f"⏳ যাচাই চলছে — যাচাই শেষ হলে রিপোর্ট পাবেন।",
        reply_keyboard_markup(load_config(), chat_id),
    )
    return True


def show_balance(chat_id: int, message_id: int | None = None) -> None:
    st = seller_stats(chat_id)
    text = (
        "💰 <b>আমার ব্যালেন্স</b>\n\n"
        f"💰 ব্যালেন্স: <b>{tk(st['balance'])}</b>\n"
        f"📈 মোট আয়: <b>{tk(st['earned'])}</b>\n"
        f"📦 মোট জমা দিয়েছি: <b>{st['total_ids']}টি</b> আইডি\n"
        f"✅ ভালো: <b>{st['good']}টি</b> | ❌ খারাপ: <b>{st['bad']}টি</b>"
        + (f" | ⏳ যাচাই চলছে: <b>{st['pending']}টি</b>" if st["pending"] else "")
    )
    kb = {"inline_keyboard": [[{"text": "🏠 হোম", "callback_data": "home"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, kb)
    else:
        send_message(chat_id, text, kb)


SUB_STATUS_BN = {"pending": "⏳ যাচাই চলছে", "reviewed": "✅ যাচাই শেষ"}


def show_my_submissions(chat_id: int, message_id: int | None = None) -> None:
    subs = [s for s in load_submissions() if int(s.get("seller_id", 0)) == chat_id]
    subs.sort(key=lambda s: s.get("id", ""), reverse=True)
    if not subs:
        text = "📋 আপনি এখনো কোনো আইডি জমা দেননি।\n\n📤 আইডি বিক্রি করুন বাটনে চাপ দিয়ে শুরু করুন।"
    else:
        lines = ["📋 <b>আমার জমা</b>\n"]
        for s in subs[:20]:
            lines.append(
                f"🧾 <b>{s['id']}</b> — {esc(s['category_name'])} — {s['total']}টি\n"
                f"   {SUB_STATUS_BN.get(s['status'], s['status'])}"
                + (f" (✅ ভালো {s['good']}, ❌ খারাপ {s['bad']}) — পাওনা {tk(s['paid'])}"
                   if s.get("status") == "reviewed" else "")
                + f"\n   🕐 {esc(s.get('created_at', ''))}"
            )
        text = "\n".join(lines)
    kb = {"inline_keyboard": [[{"text": "🏠 হোম", "callback_data": "home"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, kb)
    else:
        send_message(chat_id, text, kb)


def show_rules(chat_id: int, message_id: int | None = None) -> None:
    rules = (load_config().get("rules") or "").strip() or DEFAULT_RULES
    text = f"📜 <b>নিয়ম</b>\n\n{esc(rules)}"
    kb = {"inline_keyboard": [[{"text": "🏠 হোম", "callback_data": "home"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, kb)
    else:
        send_message(chat_id, text, kb)


# ---------------------------------------------------------------- admin handlers

def show_pending(chat_id: int, message_id: int | None = None) -> None:
    subs = [s for s in load_submissions() if s.get("status") == "pending"]
    subs.sort(key=lambda s: s.get("id", ""))
    if not subs:
        text = "📥 যাচাই করার মতো কোনো জমা নেই। সব যাচাই শেষ। ✅"
        kb = {"inline_keyboard": [[{"text": "⬅️ অ্যাডমিন প্যানেল", "callback_data": "admin"}]]}
        if message_id:
            edit_message(chat_id, message_id, text, kb)
        else:
            send_message(chat_id, text, kb)
        return
    lines = ["📥 <b>যাচাই বাকি জমা</b>\n"]
    rows: list[list[dict]] = []
    for s in subs[:10]:
        seller = esc(s.get("seller_name", ""))
        if s.get("seller_username"):
            seller += f" (@{esc(s['seller_username'])})"
        lines.append(
            f"🧾 <b>{s['id']}</b> — {esc(s['category_name'])} — {s['total']}টি\n"
            f"   👤 {seller}\n   🕐 {esc(s.get('created_at', ''))}"
        )
        rows.append([{"text": f"✅ যাচাই করুন — {s['id']}",
                      "callback_data": f"adm:reviewpick:{s['id']}"}])
    rows.append([{"text": "⬅️ অ্যাডমিন প্যানেল", "callback_data": "admin"}])
    if len(subs) > 10:
        lines.append(f"\n…আরও {len(subs) - 10}টি জমা আছে।")
    text = "\n".join(lines)
    if message_id:
        edit_message(chat_id, message_id, text, {"inline_keyboard": rows})
    else:
        send_message(chat_id, text, {"inline_keyboard": rows})


def show_review_detail(chat_id: int, message_id: int | None, sub_id: str) -> None:
    sub = next((s for s in load_submissions() if s["id"] == sub_id), None)
    if not sub:
        send_message(chat_id, "এই জমাটি পাওয়া যায়নি।")
        return
    if sub.get("status") != "pending":
        send_message(chat_id, f"{sub_id} আগেই যাচাই করা হয়েছে।")
        return
    master = load_master()
    rows = [r for r in master if r.get("submission_id") == sub_id]
    rows.sort(key=lambda r: r.get("id", 0))
    seller = esc(sub.get("seller_name", ""))
    if sub.get("seller_username"):
        seller += f" (@{esc(sub['seller_username'])})"
    # Usernames ONLY — passwords are never shown in chat.
    names = [esc(r.get("username", "")) for r in rows]
    shown = names[:50]
    name_block = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(shown))
    if len(names) > 50:
        name_block += f"\n…আরও {len(names) - 50}টি"
    state = load_state()
    state.setdefault("admin", {})[str(chat_id)] = {
        "step": "review_good", "data": {"sub_id": sub_id},
    }
    save_state(state)
    text = (
        f"✅ <b>যাচাই করুন — {sub_id}</b>\n\n"
        f"👤 বিক্রেতা: {seller}\n"
        f"🆔 ক্যাটাগরি: {esc(sub['category_name'])} (প্রতি ভালো আইডি {tk(sub['price'])})\n"
        f"📦 মোট আইডি: <b>{sub['total']}টি</b>\n"
        f"🕐 জমা: {esc(sub.get('created_at', ''))}\n\n"
        f"<b>ইউজারনেমগুলো:</b>\n{name_block}\n"
        f"(🔒 পাসওয়ার্ড এখানে দেখানো হয় না)\n\n"
        f"কতটি <b>ভালো</b> আইডি? সংখ্যা লিখুন (০–{sub['total']}):"
    )
    if message_id:
        edit_message(chat_id, message_id, text)
    else:
        send_message(chat_id, text)


def notify_seller_review(sub: dict) -> None:
    """Send the seller their review report (counts only, no passwords)."""
    send_message(
        int(sub["seller_id"]),
        f"📋 <b>যাচাই শেষ — {sub['id']}</b>\n\n"
        f"🆔 ক্যাটাগরি: {esc(sub['category_name'])}\n"
        f"📦 মোট আইডি: {sub['total']}টি\n"
        f"✅ ভালো: <b>{sub['good']}টি</b>\n"
        f"❌ খারাপ: <b>{sub['bad']}টি</b>\n"
        f"💰 পাওনা: <b>{tk(sub['paid'])}</b>\n\n"
        f"💰 আপনার ব্যালেন্স: <b>{tk(get_balance(int(sub['seller_id'])))}</b>\n"
        f"ধন্যবাদ! আবার আইডি জমা দিন। 🙏",
        reply_keyboard_markup(load_config(), int(sub["seller_id"])),
    )


def show_master(chat_id: int, message_id: int | None = None) -> None:
    totals = master_totals()
    lines = ["📊 <b>মাস্টার শিট</b>\n"]
    if not totals["per_category"]:
        lines.append("এখনো কোনো আইডি জমা হয়নি।")
    for name, e in totals["per_category"].items():
        lines.append(
            f"<b>{esc(name)}</b>\n"
            f"📦 মোট: {e['total']} | ✅ ভালো: {e['good']} | "
            f"❌ খারাপ: {e['bad']} | ⏳ যাচাই চলছে: {e['pending']}\n"
            f"💰 পেমেন্ট: {tk(e['paid'])}"
        )
    g = totals["grand"]
    lines.append(
        f"\n<b>মোট:</b> 📦 {g['total']} | ✅ {g['good']} | ❌ {g['bad']} | "
        f"⏳ {g['pending']} | 💰 {tk(g['paid'])}"
    )
    text = "\n".join(lines)
    kb = {"inline_keyboard": [[{"text": "⬅️ অ্যাডমিন প্যানেল", "callback_data": "admin"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, kb)
    else:
        send_message(chat_id, text, kb)


def show_today(chat_id: int, message_id: int | None = None) -> None:
    r = today_report()
    text = (
        f"🧾 <b>আজকের হিসাব ({r['date']})</b>\n\n"
        f"📥 জমা: <b>{r['submissions']}টি</b> ({r['total_ids']}টি আইডি)\n"
        f"✅ ভালো: <b>{r['good']}টি</b> | ❌ খারাপ: <b>{r['bad']}টি</b>\n"
        f"⏳ যাচাই বাকি: <b>{r['pending']}টি</b> জমা\n"
        f"💰 আজ পেমেন্ট: <b>{tk(r['paid'])}</b>"
    )
    kb = {"inline_keyboard": [[{"text": "⬅️ অ্যাডমিন প্যানেল", "callback_data": "admin"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, kb)
    else:
        send_message(chat_id, text, kb)


def start_give_balance(chat_id: int, message_id: int | None = None) -> None:
    state = load_state()
    users = dict(state.get("users", {}))
    balances = load_balances()
    for uid in balances:
        users.setdefault(uid, {"name": f"User {uid}", "username": ""})
    if not users:
        text = "এখনো কোনো ইউজার বটে আসেনি।"
        if message_id:
            edit_message(chat_id, message_id, text)
        else:
            send_message(chat_id, text)
        return
    rows = []
    for uid, u in sorted(users.items(), key=lambda kv: str(kv[1].get("name", ""))):
        label = u.get("name", uid)
        if u.get("username"):
            label += f" (@{u['username']})"
        label += f" — {tk(get_balance(int(uid)))}"
        rows.append([{"text": label, "callback_data": f"adm:givepick:{uid}"}])
    rows.append([{"text": "⬅️ অ্যাডমিন প্যানেল", "callback_data": "admin"}])
    text = "💰 কাকে ব্যালেন্স দেবেন?"
    if message_id:
        edit_message(chat_id, message_id, text, {"inline_keyboard": rows})
    else:
        send_message(chat_id, text, {"inline_keyboard": rows})


def handle_admin_text(chat_id: int, text: str, state: dict) -> bool:
    """One step of an admin flow. Returns True when handled."""
    flow = state.get("admin", {}).get(str(chat_id))
    if not flow:
        return False
    step = flow.get("step")
    data = flow.get("data", {})

    def clear():
        state.get("admin", {}).pop(str(chat_id), None)
        save_state(state)

    if step == "add_name":
        data["name"] = text.strip()
        flow["step"] = "add_price"
        save_state(state)
        send_message(chat_id, f"➕ ক্যাটাগরি: <b>{esc(data['name'])}</b>\n\nপ্রতি ভালো আইডির <b>দাম</b> কত টাকা? সংখ্যায় লিখুন (যেমন: ৫০)")
        return True
    if step == "add_price":
        price = parse_number(text)
        if price is None or price <= 0:
            send_message(chat_id, "❌ সঠিক দাম লিখুন (সংখ্যায়, যেমন: ৫০)।")
            return True
        cat = add_category(data["name"], price)
        clear()
        send_message(
            chat_id,
            f"✅ ক্যাটাগরি যোগ হয়েছে!\n\n<b>{esc(cat['name'])}</b> — প্রতি ভালো আইডি {tk(cat['price'])}\n"
            f"এখন সেলাররা এই ক্যাটাগরিতে আইডি জমা দিতে পারবে।",
            admin_menu(),
        )
        return True
    if step == "edit_value":
        cats = load_categories()
        cat = next((c for c in cats if c["id"] == data.get("cat_id")), None)
        if not cat:
            clear()
            send_message(chat_id, "এই ক্যাটাগরিটি আর নেই।")
            return True
        field = data.get("field")
        if field == "price":
            price = parse_number(text)
            if price is None or price <= 0:
                send_message(chat_id, "❌ সঠিক দাম লিখুন (সংখ্যায়)।")
                return True
            cat["price"] = price
            save_categories(cats)
            clear()
            send_message(chat_id, f"✅ <b>{esc(cat['name'])}</b>-এর নতুন দাম: {tk(price)}/পিস।", admin_menu())
        elif field == "name":
            cat["name"] = text.strip()
            save_categories(cats)
            clear()
            send_message(chat_id, f"✅ ক্যাটাগরির নতুন নাম: <b>{esc(cat['name'])}</b>।", admin_menu())
        else:
            clear()
            send_message(chat_id, "বুঝতে পারিনি।")
        return True
    if step == "rules_text":
        config = load_config()
        config["rules"] = text.strip()
        save_config(config)
        clear()
        send_message(chat_id, "✅ নিয়ম বসানো হয়েছে। সেলাররা 📜 নিয়ম বাটনে দেখতে পারবে।", admin_menu())
        return True
    if step == "review_good":
        sub_id = data.get("sub_id")
        sub = next((s for s in load_submissions() if s["id"] == sub_id), None)
        if not sub or sub.get("status") != "pending":
            clear()
            send_message(chat_id, "এই জমাটি আর যাচাই বাকি নেই।")
            return True
        good = parse_number(text)
        if good is None or good < 0 or good > int(sub["total"]):
            send_message(chat_id, f"❌ ০ থেকে {sub['total']}-এর মধ্যে সংখ্যা লিখুন।")
            return True
        bad = int(sub["total"]) - good
        paid = good * int(sub["price"])
        flow["step"] = "review_confirm"
        flow["data"]["good"] = good
        save_state(state)
        kb = {"inline_keyboard": [
            [{"text": "✅ হ্যাঁ, নিশ্চিত", "callback_data": "adm:reviewok"},
             {"text": "❌ না, বাতিল", "callback_data": "adm:reviewno"}],
        ]}
        send_message(
            chat_id,
            f"🔍 <b>নিশ্চিত করুন — {sub_id}</b>\n\n"
            f"📦 মোট: {sub['total']}টি\n"
            f"✅ ভালো: <b>{good}টি</b>\n"
            f"❌ খারাপ: <b>{bad}টি</b>\n"
            f"💰 পাওনা: <b>{tk(paid)}</b> (বিক্রেতার ব্যালেন্সে যোগ হবে)\n\n"
            f"⚠️ নিয়ম: শিটের <b>প্রথম {good}টি</b> আইডি ভালো হিসেবে ধরা হবে, "
            f"বাকিগুলো খারাপ।",
            kb,
        )
        return True
    if step == "give_amount":
        amount = parse_number(text)
        if amount is None or amount <= 0:
            send_message(chat_id, "❌ সঠিক টাকার পরিমাণ লিখুন (সংখ্যায়)।")
            return True
        data["amount"] = amount
        flow["step"] = "give_confirm"
        save_state(state)
        kb = {"inline_keyboard": [
            [{"text": "✅ হ্যাঁ, দিন", "callback_data": "adm:giveok"},
             {"text": "❌ না", "callback_data": "adm:giveno"}],
        ]}
        send_message(
            chat_id,
            f"💰 <b>নিশ্চিত করুন</b>\n\n"
            f"ইউজার ID: {data['user_id']}\n"
            f"টাকা: <b>{tk(amount)}</b>\n\n"
            f"এই ব্যালেন্স কি দেবেন?",
            kb,
        )
        return True
    return False


# ---------------------------------------------------------------- message routing

def handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    config = load_config()
    state = load_state()

    # Remember every user (for the admin's give-balance picker).
    sender = msg.get("from", {})
    if sender:
        name = sender.get("first_name", "") or ""
        if sender.get("last_name"):
            name += " " + sender["last_name"]
        state.setdefault("users", {})[str(chat_id)] = {
            "name": name.strip() or str(chat_id),
            "username": sender.get("username", ""),
        }
        save_state(state)

    if text.startswith("/start"):
        send_message(chat_id, home_text(config, chat_id),
                     reply_keyboard_markup(config, chat_id))
        return
    if text.startswith("/cancel"):
        state.get("sell", {}).pop(str(chat_id), None)
        state.get("admin", {}).pop(str(chat_id), None)
        save_state(state)
        send_message(chat_id, "বাতিল হয়েছে। 🏠 হোমে ফিরে গেলাম।",
                     reply_keyboard_markup(config, chat_id))
        return

    # A document (CSV / .xlsx) sent while selling: parse and submit.
    if "document" in msg:
        flow = state.get("sell", {}).get(str(chat_id))
        if not flow:
            send_message(chat_id, "📤 আইডি জমা দিতে প্রথমে 📤 আইডি বিক্রি করুন বাটনে চাপ দিন।")
            return
        doc = msg["document"]
        try:
            data = download_file(doc["file_id"])
            accounts = parse_accounts(data, doc.get("file_name", ""))
        except Exception as exc:
            send_message(chat_id, f"😔 ফাইলটি পড়া যায়নি: {esc(str(exc))}\n\nআবার চেষ্টা করুন।")
            return
        finish_sell(chat_id, msg, accounts)
        return

    # Persistent home-page buttons (reply keyboard): tapping one sends its
    # label as a plain message. Route it like the matching button.
    # This check runs before the sell/admin flows, so tapping a button
    # always navigates, even in the middle of a flow.
    if text == BTN_SELL:
        show_sell_categories(chat_id)
        return
    if text == BTN_BALANCE:
        show_balance(chat_id)
        return
    if text == BTN_MYSUBS:
        show_my_submissions(chat_id)
        return
    if text == BTN_RULES:
        show_rules(chat_id)
        return
    if text == BTN_HELP:
        send_message(chat_id, HELP_TEXT, reply_keyboard_markup(config, chat_id))
        return
    if text == BTN_ADMIN:
        if admin_required(chat_id, config):
            send_message(chat_id, "👑 <b>অ্যাডমিন প্যানেল</b>",
                         admin_keyboard_markup())
        return
    if text == BTN_HOME:
        state.get("sell", {}).pop(str(chat_id), None)
        state.get("admin", {}).pop(str(chat_id), None)
        save_state(state)
        send_message(chat_id, home_text(config, chat_id),
                     reply_keyboard_markup(config, chat_id))
        return

    # Admin action buttons, also locked on the home page (admin only).
    if text in (ABTN_ADD_CAT, ABTN_EDIT_CAT, ABTN_TOGGLE, ABTN_RULES,
                ABTN_PENDING, ABTN_REVIEW, ABTN_GIVEBAL, ABTN_MASTER,
                ABTN_TODAY, ABTN_EXPORT):
        if not admin_required(chat_id, config):
            return
        if text == ABTN_ADD_CAT:
            state.setdefault("admin", {})[str(chat_id)] = {"step": "add_name", "data": {}}
            save_state(state)
            send_message(chat_id, "➕ <b>নতুন ক্যাটাগরি</b>\n\nক্যাটাগরির <b>নাম</b> লিখুন (যেমন: Instagram 2FA ID)")
        elif text == ABTN_EDIT_CAT:
            cats = load_categories()
            if not cats:
                send_message(chat_id, "এখনো কোনো ক্যাটাগরি নেই।", admin_menu())
            else:
                send_message(chat_id, "✏️ কোন ক্যাটাগরি এডিট করবেন?",
                             pick_category_keyboard("adm:editpick", cats))
        elif text == ABTN_TOGGLE:
            cats = load_categories()
            if not cats:
                send_message(chat_id, "এখনো কোনো ক্যাটাগরি নেই।", admin_menu())
            else:
                send_message(chat_id, "⏸️ কোন ক্যাটাগরি বন্ধ/চালু করবেন?",
                             pick_category_keyboard("adm:togglepick", cats, show_status=True))
        elif text == ABTN_RULES:
            state.setdefault("admin", {})[str(chat_id)] = {"step": "rules_text", "data": {}}
            save_state(state)
            send_message(chat_id, "📜 <b>নিয়ম বসান</b>\n\nসেলারদের জন্য নিয়ম লিখে পাঠান (যেমন: ভালো আইডি ছাড়া টাকা দেওয়া হবে না)।")
        elif text == ABTN_PENDING:
            show_pending(chat_id)
        elif text == ABTN_REVIEW:
            show_pending(chat_id)
        elif text == ABTN_GIVEBAL:
            start_give_balance(chat_id)
        elif text == ABTN_MASTER:
            show_master(chat_id)
        elif text == ABTN_TODAY:
            show_today(chat_id)
        elif text == ABTN_EXPORT:
            try:
                send_document(chat_id, "master-sheet.csv", master_csv_bytes(),
                              caption="📊 <b>মাস্টার শিট</b> — সব আইডির হিসাব।")
            except Exception as exc:
                send_message(chat_id, f"😔 ফাইল পাঠাতে পারিনি: {esc(str(exc))}")
        return

    # Pasted text lines while selling: parse and submit.
    if state.get("sell", {}).get(str(chat_id)):
        accounts = parse_accounts_text(text)
        finish_sell(chat_id, msg, accounts)
        return

    if handle_admin_text(chat_id, text, state):
        return

    send_message(
        chat_id,
        "বুঝতে পারিনি। নিচ থেকে বেছে নিন, অথবা /start লিখুন।",
        main_menu(config, chat_id),
    )


def handle_callback(query: dict) -> None:
    chat_id = query["message"]["chat"]["id"]
    message_id = query["message"]["message_id"]
    data = query.get("data", "")
    config = load_config()
    state = load_state()

    def home():
        send_message(chat_id, home_text(config, chat_id),
                     reply_keyboard_markup(config, chat_id))

    if data == "home":
        home()
    elif data == "help":
        edit_message(chat_id, message_id, HELP_TEXT, main_menu(config, chat_id))
    elif data == "sell":
        show_sell_categories(chat_id, message_id)
    elif data.startswith("sellcat:"):
        start_sell(chat_id, message_id, int(data.split(":", 1)[1]))
    elif data == "balance":
        show_balance(chat_id, message_id)
    elif data == "mysubs":
        show_my_submissions(chat_id, message_id)
    elif data == "rules":
        show_rules(chat_id, message_id)
    elif data == "admin":
        if admin_required(chat_id, config):
            show_admin(chat_id, message_id)
    elif data == "adm:addcat":
        if not admin_required(chat_id, config):
            return
        state.setdefault("admin", {})[str(chat_id)] = {"step": "add_name", "data": {}}
        save_state(state)
        edit_message(chat_id, message_id, "➕ <b>নতুন ক্যাটাগরি</b>\n\nক্যাটাগরির <b>নাম</b> লিখুন (যেমন: Instagram 2FA ID)")
    elif data == "adm:editcat":
        if not admin_required(chat_id, config):
            return
        cats = load_categories()
        if not cats:
            edit_message(chat_id, message_id, "এখনো কোনো ক্যাটাগরি নেই।", admin_menu())
            return
        edit_message(chat_id, message_id, "✏️ কোন ক্যাটাগরি এডিট করবেন?",
                     pick_category_keyboard("adm:editpick", cats))
    elif data.startswith("adm:editpick:"):
        if not admin_required(chat_id, config):
            return
        cat_id = int(data.split(":")[2])
        kb = {"inline_keyboard": [
            [{"text": "💰 দাম", "callback_data": f"adm:field:price:{cat_id}"},
             {"text": "✏️ নাম", "callback_data": f"adm:field:name:{cat_id}"}],
            [{"text": "⬅️ অ্যাডমিন প্যানেল", "callback_data": "admin"}],
        ]}
        edit_message(chat_id, message_id, "কোনটা বদলাবেন?", kb)
    elif data.startswith("adm:field:"):
        if not admin_required(chat_id, config):
            return
        _, _, field, cat_id = data.split(":")
        labels = {"price": "নতুন <b>দাম</b> (প্রতি ভালো আইডি)",
                  "name": "নতুন <b>নাম</b>"}
        state.setdefault("admin", {})[str(chat_id)] = {
            "step": "edit_value", "data": {"cat_id": int(cat_id), "field": field},
        }
        save_state(state)
        edit_message(chat_id, message_id, f"{labels[field]} লিখুন:")
    elif data == "adm:toggle":
        if not admin_required(chat_id, config):
            return
        cats = load_categories()
        if not cats:
            edit_message(chat_id, message_id, "এখনো কোনো ক্যাটাগরি নেই।", admin_menu())
            return
        edit_message(chat_id, message_id, "⏸️ কোন ক্যাটাগরি বন্ধ/চালু করবেন?",
                     pick_category_keyboard("adm:togglepick", cats, show_status=True))
    elif data.startswith("adm:togglepick:"):
        if not admin_required(chat_id, config):
            return
        cat_id = int(data.split(":")[2])
        cat = next((c for c in load_categories() if c["id"] == cat_id), None)
        if not cat:
            edit_message(chat_id, message_id, "এই ক্যাটাগরিটি আর নেই।", admin_menu())
            return
        updated = set_category_active(cat_id, not cat.get("active", True))
        status = "চালু হয়েছে ▶️" if updated["active"] else "বন্ধ হয়েছে ⏸️"
        edit_message(
            chat_id, message_id,
            f"<b>{esc(updated['name'])}</b> ক্যাটাগরি {status}।\n"
            + ("সেলাররা আবার এই ক্যাটাগরিতে আইডি জমা দিতে পারবে।" if updated["active"]
               else "সেলাররা এখন এই ক্যাটাগরিতে আইডি জমা দিতে পারবে না।"),
            admin_menu(),
        )
    elif data == "adm:rules":
        if not admin_required(chat_id, config):
            return
        state.setdefault("admin", {})[str(chat_id)] = {"step": "rules_text", "data": {}}
        save_state(state)
        edit_message(chat_id, message_id, "📜 <b>নিয়ম বসান</b>\n\nসেলারদের জন্য নিয়ম লিখে পাঠান।")
    elif data == "adm:pending" or data == "adm:review":
        if admin_required(chat_id, config):
            show_pending(chat_id, message_id)
    elif data.startswith("adm:reviewpick:"):
        if not admin_required(chat_id, config):
            return
        show_review_detail(chat_id, message_id, data.split(":", 2)[2])
    elif data == "adm:reviewok":
        if not admin_required(chat_id, config):
            return
        flow = state.get("admin", {}).get(str(chat_id))
        if not flow or flow.get("step") != "review_confirm":
            edit_message(chat_id, message_id, "এই যাচাইটি আর বাকি নেই।", admin_menu())
            return
        sub_id = flow["data"]["sub_id"]
        good = int(flow["data"]["good"])
        try:
            sub = apply_review(sub_id, good)
        except ValueError as exc:
            edit_message(chat_id, message_id, f"❌ {esc(str(exc))}", admin_menu())
            state.get("admin", {}).pop(str(chat_id), None)
            save_state(state)
            return
        state.get("admin", {}).pop(str(chat_id), None)
        save_state(state)
        try:
            notify_seller_review(sub)
        except Exception:
            pass  # seller notification is best-effort
        edit_message(
            chat_id, message_id,
            f"✅ <b>যাচাই শেষ — {sub_id}</b>\n\n"
            f"🆔 {esc(sub['category_name'])}\n"
            f"✅ ভালো: <b>{sub['good']}টি</b> | ❌ খারাপ: <b>{sub['bad']}টি</b>\n"
            f"💰 পাওনা: <b>{tk(sub['paid'])}</b> — বিক্রেতার ব্যালেন্সে যোগ হয়েছে।\n\n"
            f"⚠️ শিটের প্রথম {sub['good']}টি আইডি ভালো হিসেবে ধরা হয়েছে।",
            admin_menu(),
        )
    elif data == "adm:reviewno":
        if not admin_required(chat_id, config):
            return
        state.get("admin", {}).pop(str(chat_id), None)
        save_state(state)
        edit_message(chat_id, message_id, "❌ যাচাই বাতিল হয়েছে। জমাটি যাচাই বাকি আছে।", admin_menu())
    elif data == "adm:givebalance":
        if admin_required(chat_id, config):
            start_give_balance(chat_id, message_id)
    elif data.startswith("adm:givepick:"):
        if not admin_required(chat_id, config):
            return
        target = data.split(":", 2)[2]
        state.setdefault("admin", {})[str(chat_id)] = {
            "step": "give_amount", "data": {"user_id": int(target)},
        }
        save_state(state)
        edit_message(chat_id, message_id,
                     f"💰 কত টাকা ব্যালেন্স দেবেন? (সংখ্যায় লিখুন, যেমন: ১০০)\nইউজার ID: {target}")
    elif data == "adm:giveok":
        if not admin_required(chat_id, config):
            return
        flow = state.get("admin", {}).get(str(chat_id))
        if not flow or flow.get("step") != "give_confirm":
            edit_message(chat_id, message_id, "এই ব্যালেন্স দেওয়াটি আর বাকি নেই।", admin_menu())
            return
        user_id = int(flow["data"]["user_id"])
        amount = int(flow["data"]["amount"])
        new_bal = give_balance_to(user_id, amount)
        state.get("admin", {}).pop(str(chat_id), None)
        save_state(state)
        try:
            send_message(
                user_id,
                f"💰 <b>ব্যালেন্স পেয়েছেন!</b>\n\n"
                f"অ্যাডমিন আপনাকে <b>{tk(amount)}</b> ব্যালেন্স দিয়েছেন।\n"
                f"💰 আপনার বর্তমান ব্যালেন্স: <b>{tk(new_bal)}</b>",
                reply_keyboard_markup(load_config(), user_id),
            )
        except Exception:
            pass
        edit_message(chat_id, message_id,
                     f"✅ ইউজার {user_id}-কে {tk(amount)} ব্যালেন্স দেওয়া হয়েছে।\n"
                     f"তার বর্তমান ব্যালেন্স: {tk(new_bal)}", admin_menu())
    elif data == "adm:giveno":
        if not admin_required(chat_id, config):
            return
        state.get("admin", {}).pop(str(chat_id), None)
        save_state(state)
        edit_message(chat_id, message_id, "❌ ব্যালেন্স দেওয়া বাতিল হয়েছে।", admin_menu())
    elif data == "adm:master":
        if admin_required(chat_id, config):
            show_master(chat_id, message_id)
    elif data == "adm:today":
        if admin_required(chat_id, config):
            show_today(chat_id, message_id)
    elif data == "adm:export":
        if not admin_required(chat_id, config):
            return
        try:
            send_document(chat_id, "master-sheet.csv", master_csv_bytes(),
                          caption="📊 <b>মাস্টার শিট</b> — সব আইডির হিসাব।")
            answer_callback(query["id"], "মাস্টার শিট পাঠানো হয়েছে ✅")
        except Exception as exc:
            edit_message(chat_id, message_id, f"😔 ফাইল পাঠাতে পারিনি: {esc(str(exc))}")
    answer_callback(query["id"])


# ---------------------------------------------------------------- hosting
# Render's free web services must answer HTTP, otherwise Render treats the
# service as down and sleeps it after 15 idle minutes. A tiny health server
# answers / and /ping so Render (and a free UptimeRobot monitor pinging
# every 5 minutes) can keep the bot awake.
def _start_health_server() -> None:
    """Serve a tiny OK page on $PORT in a background thread (Render)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = int(os.environ.get("PORT", "10000"))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server naming
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):  # silence request logs
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"health server listening on port {port}", flush=True)


def main() -> int:
    print("starting telegram ID buy bot…", flush=True)
    if os.environ.get("PORT"):
        _start_health_server()
    try:
        _token_or_surrogate()
    except RuntimeError as exc:
        print(f"FATAL: {exc}", flush=True)
        print("Bot did NOT start. Create the bot with @BotFather, then set "
              "TELEGRAM_BUY_BOT_TOKEN (or connect custom.telegram_buy) "
              "and start again.", flush=True)
        return 2
    me = None
    last_exc: Exception | None = None
    for attempt in range(1, 8):
        try:
            me = api("getMe", timeout=60.0)
            break
        except Exception as exc:  # transient network / slow egress proxy
            last_exc = exc
            print(f"getMe attempt {attempt} failed: {_redact(str(exc))}", flush=True)
            time.sleep(10)
    if me is None:
        print(f"FATAL: cannot reach Telegram API (check the bot token): {_redact(str(last_exc))}", flush=True)
        return 2
    print(f"bot connected as @{me.get('username')} ({me.get('first_name')})", flush=True)

    offset = 0
    while True:
        try:
            updates = api("getUpdates", {"offset": offset, "timeout": 25}, timeout=35)
        except Exception as exc:
            print(f"getUpdates failed: {_redact(str(exc))}", flush=True)
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    handle_message(update["message"])
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])
            except Exception:
                print(f"error handling update {update.get('update_id')}:", flush=True)
                traceback.print_exc()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
