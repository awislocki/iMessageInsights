#!/usr/bin/env python3
"""
iMessage Export & Analyze — a local web tool to browse Apple Messages threads,
get AI-powered relationship insights, and export them as AI-optimized
Markdown, encrypted Markdown, or PDF.

Stdlib only. Reads ~/Library/Messages/chat.db READ-ONLY.
Requires Full Disk Access for the terminal that launches it (see README.md).

Usage:
    python3 server.py                 # serve on http://127.0.0.1:8765
    python3 server.py --port 9000
    ANTHROPIC_API_KEY=sk-... python3 server.py   # (optional) preload key
"""

import io
import os
import re
import sys
import json
import html
import time
import shutil
import zipfile
import sqlite3
import argparse
import datetime
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

HOME = os.path.expanduser("~")
CHAT_DB = os.path.join(HOME, "Library", "Messages", "chat.db")
APPLE_EPOCH = 978307200  # 2001-01-01 00:00:00 UTC in unix seconds

CONFIG_DIR = os.path.join(HOME, ".imessage-export")
REL_FILE = os.path.join(CONFIG_DIR, "relationships.json")

# Runtime settings — secrets live ONLY in memory, never written to disk.
SETTINGS = {
    "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    "model": "claude-sonnet-4-6",
    "vault_pw": "",   # password that encrypts relationship classifications + notes
}

RELATIONSHIP_TYPES = [
    "Family", "Close Friend", "Friend", "Romantic / Partner", "Ex / Former Partner",
    "Professional / Colleague", "Client / Business", "Acquaintance", "Other",
]

# Demo mode (--demo): serve fictional data so the UI can be shown/screenshotted
# without Full Disk Access, a real database, an API key, or a vault. Set in main().
DEMO = False
_DEMO_REL = {}
_DEMO_QA = {}

# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

# A live chat.db is in WAL mode and Messages.app writes to it constantly, which
# causes intermittent "unable to open database file" and can hide messages still in
# the -wal file. We read from a COPY (db + -wal + -shm) so reads are stable and
# complete. The copy is refreshed when the source changes (rate-limited).
_SNAP = {"path": None, "mtime": 0.0, "ts": 0.0}
_SNAP_LOCK = threading.Lock()
# chat.db is ~0.5GB; recopying it every live-poll would thrash the disk. 60s of
# staleness is invisible in practice (the UI polls every 8s but most polls hit
# the same snapshot).
_SNAP_MIN_INTERVAL = 60.0


def _cleanup_snapshots(min_age=0):
    """Remove imsg-snap-* temp dirs. min_age guards against deleting a snapshot a
    concurrently-running instance (CLI + menu-bar app) is actively using."""
    tmp = tempfile.gettempdir()
    now = time.time()
    try:
        for name in os.listdir(tmp):
            if not name.startswith("imsg-snap-"):
                continue
            full = os.path.join(tmp, name)
            if _SNAP["path"] and os.path.dirname(_SNAP["path"]) == full:
                shutil.rmtree(full, ignore_errors=True)  # our own → always OK
                continue
            try:
                if now - os.path.getmtime(full) > min_age:
                    shutil.rmtree(full, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


import atexit
atexit.register(lambda: _cleanup_snapshots(min_age=3600))
_cleanup_snapshots(min_age=3600)  # sweep hour-old leftovers from prior runs


def _db_snapshot():
    """Path to a fresh, complete, lock-free copy of chat.db. Falls back to live file."""
    try:
        src_mtime = os.path.getmtime(CHAT_DB)
    except OSError:
        return CHAT_DB
    with _SNAP_LOCK:
        now = time.time()
        have = _SNAP["path"] and os.path.exists(_SNAP["path"])
        stale = src_mtime != _SNAP["mtime"] and (now - _SNAP["ts"]) > _SNAP_MIN_INTERVAL
        if not have or stale:
            try:
                d = tempfile.mkdtemp(prefix="imsg-snap-")
                dst = os.path.join(d, "chat.db")
                for suf in ("", "-wal", "-shm"):
                    if os.path.exists(CHAT_DB + suf):
                        shutil.copy2(CHAT_DB + suf, dst + suf)
                old = _SNAP["path"]
                _SNAP.update(path=dst, mtime=src_mtime, ts=now)
                if old and os.path.exists(old):
                    shutil.rmtree(os.path.dirname(old), ignore_errors=True)
            except Exception:
                return CHAT_DB
        return _SNAP["path"]


def open_db():
    path = _db_snapshot()
    if path != CHAT_DB:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)  # our own copy
    else:                                                                    # fallback
        con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro&immutable=1", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def apple_time_to_dt(value):
    if not value:
        return None
    secs = value / 1e9 if value > 1_000_000_000_000 else value
    try:
        return datetime.datetime.fromtimestamp(APPLE_EPOCH + secs)
    except (OverflowError, OSError, ValueError):
        return None


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "unknown"


# ---------------------------------------------------------------------------
# attributedBody decoding (modern macOS stores text here when `text` is NULL)
# ---------------------------------------------------------------------------

def decode_attributed_body(blob):
    if not blob:
        return None
    try:
        marker = blob.index(b"NSString")
    except ValueError:
        return None
    data = blob[marker + len(b"NSString"):]
    plus = data.find(b"+")
    if plus == -1:
        return None
    data = data[plus + 1:]
    if not data:
        return None
    length = data[0]
    offset = 1
    if length == 0x81:
        length = int.from_bytes(data[1:3], "little"); offset = 3
    elif length == 0x82:
        length = int.from_bytes(data[1:5], "little"); offset = 5
    text = data[offset:offset + length]
    try:
        return text.decode("utf-8", errors="replace").strip("\x00") or None
    except Exception:
        return None


def message_text(row):
    txt = row["text"]
    if txt and txt.strip():
        return txt.strip()
    return decode_attributed_body(row["attributedBody"])


# ---------------------------------------------------------------------------
# Contact name resolution (best effort, via AddressBook)
# ---------------------------------------------------------------------------

def normalize_handle(h):
    if not h:
        return h
    if "@" in h:
        return h.lower()
    digits = re.sub(r"\D", "", h)
    return digits[-10:] if len(digits) >= 10 else digits


def load_contacts():
    mapping = {}
    base = os.path.join(HOME, "Library", "Application Support", "AddressBook", "Sources")
    if not os.path.isdir(base):
        return mapping
    for src in os.listdir(base):
        db = os.path.join(base, src, "AddressBook-v22.abcddb")
        if not os.path.exists(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=3)
            con.row_factory = sqlite3.Row
        except Exception:
            continue
        try:
            def name_of(r):
                name = " ".join(p for p in [r["ZFIRSTNAME"], r["ZLASTNAME"]] if p)
                return name or r["ZORGANIZATION"] or None
            for tbl, col in (("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                             ("ZABCDEMAILADDRESS", "ZADDRESS")):
                try:
                    for r in con.execute(
                        f"SELECT x.{col} AS v, rec.ZFIRSTNAME, rec.ZLASTNAME, rec.ZORGANIZATION "
                        f"FROM {tbl} x JOIN ZABCDRECORD rec ON x.ZOWNER = rec.Z_PK"):
                        n = name_of(r)
                        if n and r["v"]:
                            mapping.setdefault(normalize_handle(r["v"]), n)
                except sqlite3.Error:
                    pass
        finally:
            con.close()
    return mapping


_CONTACTS = None

def contacts():
    global _CONTACTS
    if _CONTACTS is None:
        try:
            _CONTACTS = load_contacts()
        except Exception:
            _CONTACTS = {}
    return _CONTACTS


def display_for_handle(h):
    return contacts().get(normalize_handle(h), h) if h else "Unknown"


# ---------------------------------------------------------------------------
# Contact photos. Stored in ZABCDRECORD as a blob with a 1-byte prefix:
#   0x01 -> a local JPEG/PNG follows;  0x02 -> an iCloud reference (no local data).
# We serve only the locally-stored ones; everything else falls back to a dot.
# ---------------------------------------------------------------------------

_JPEG = b"\xff\xd8\xff"
_PNG = b"\x89PNG\r\n\x1a\n"


def _extract_image(*blobs):
    for b in blobs:
        if not b:
            continue
        cand = b[1:] if b[0] in (1, 2) else b
        if cand[:3] == _JPEG or cand[:8] == _PNG:
            return cand
    return None


def load_contact_images():
    imgs = {}
    base = os.path.join(HOME, "Library", "Application Support", "AddressBook", "Sources")
    if not os.path.isdir(base):
        return imgs
    for src in os.listdir(base):
        db = os.path.join(base, src, "AddressBook-v22.abcddb")
        if not os.path.exists(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=3)
            con.row_factory = sqlite3.Row
        except Exception:
            continue
        try:
            recimg = {}
            for r in con.execute("SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, "
                                 "ZIMAGEDATA AS f, ZTHUMBNAILIMAGEDATA AS t "
                                 "FROM ZABCDRECORD WHERE ZIMAGEDATA IS NOT NULL "
                                 "OR ZTHUMBNAILIMAGEDATA IS NOT NULL"):
                im = _extract_image(r["t"], r["f"])
                if im:
                    recimg[r["Z_PK"]] = im
                    nm = " ".join(p for p in [r["ZFIRSTNAME"], r["ZLASTNAME"]] if p) \
                        or r["ZORGANIZATION"]
                    if nm:  # let imported convos resolve a photo by contact name
                        imgs.setdefault("name:" + nm.strip().lower(), im)
            if not recimg:
                continue
            for tbl, col in (("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                             ("ZABCDEMAILADDRESS", "ZADDRESS")):
                try:
                    for r in con.execute(f"SELECT x.{col} AS v, x.ZOWNER AS owner FROM {tbl} x"):
                        if r["owner"] in recimg and r["v"]:
                            imgs.setdefault(normalize_handle(r["v"]), recimg[r["owner"]])
                except sqlite3.Error:
                    pass
        finally:
            con.close()
    return imgs


_CONTACT_IMAGES = None


def contact_images():
    global _CONTACT_IMAGES
    if _CONTACT_IMAGES is None:
        try:
            _CONTACT_IMAGES = load_contact_images()
        except Exception:
            _CONTACT_IMAGES = {}
    return _CONTACT_IMAGES


def get_contact_image(handle):
    if not handle:
        return None
    m = contact_images()
    return m.get(normalize_handle(handle)) or m.get("name:" + handle.strip().lower())


def image_url_for(handle):
    return "/api/avatar?handle=" + quote(handle) if get_contact_image(handle) else None


# ---------------------------------------------------------------------------
# Relationship store — ENCRYPTED at rest (AES-256) with the user's vault password.
# Classifications and breakup/context notes never touch disk in plaintext.
# The password is held in memory only; the vault stays locked until unlocked.
# ---------------------------------------------------------------------------

REL_ENC_FILE = REL_FILE + ".enc"
_REL_CACHE = None  # decrypted dict, populated only after a successful unlock
# AI results (insights/reply/critique) cached per chat so re-opening costs nothing.
# Mirrors the "_ai" key inside the encrypted vault; kept in memory for the session.
_AI_CACHE = {}


def _vault_decrypt(blob, pw):
    env = dict(os.environ, IMSG_VPW=pw)
    p = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", "env:IMSG_VPW"],
        input=blob, capture_output=True, env=env)
    if p.returncode != 0:
        raise ValueError("wrong password or corrupt vault")
    return p.stdout.decode("utf-8")


def vault_status():
    """exists: an encrypted vault file is present. unlocked: we can read it."""
    if DEMO:
        return {"exists": False, "unlocked": True, "legacy_plaintext": False}
    legacy = os.path.exists(REL_FILE)
    return {"exists": os.path.exists(REL_ENC_FILE) or legacy,
            "unlocked": _REL_CACHE is not None,
            "legacy_plaintext": legacy and not os.path.exists(REL_ENC_FILE)}


def vault_unlock(pw):
    """Set the password and decrypt. Returns True on success, False on bad password.
    If no vault exists yet, this password becomes the vault's password."""
    global _REL_CACHE, _AI_CACHE
    SETTINGS["vault_pw"] = pw
    _REL_CACHE = None
    if os.path.exists(REL_ENC_FILE):
        try:
            with open(REL_ENC_FILE, "rb") as f:
                _REL_CACHE = json.loads(_vault_decrypt(f.read(), pw))
            _AI_CACHE = dict(_REL_CACHE.get("_ai", {}))  # restore saved AI results
            _restore_settings()                          # restore saved API key/model
            return True
        except Exception:
            SETTINGS["vault_pw"] = ""
            return False
    # No encrypted vault yet — start one (migrating any legacy plaintext file).
    _REL_CACHE = {}
    if os.path.exists(REL_FILE):
        try:
            with open(REL_FILE) as f:
                _REL_CACHE = json.load(f)
        except Exception:
            _REL_CACHE = {}
    return True


def load_relationships():
    """Decrypted relationships, or {} when locked."""
    return _REL_CACHE if _REL_CACHE is not None else {}


def _vault_write():
    """Encrypt the current _REL_CACHE to disk (keeping one .bak). Requires unlock."""
    pw = SETTINGS.get("vault_pw")
    if not pw or _REL_CACHE is None:
        raise RuntimeError("Vault is locked. Set/enter your vault password in Settings (⚙) "
                           "to save encrypted data.")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(REL_ENC_FILE):
        try:
            with open(REL_ENC_FILE, "rb") as src, open(REL_ENC_FILE + ".bak", "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass
    cipher = encrypt_bytes(json.dumps(_REL_CACHE, indent=2), pw)
    with open(REL_ENC_FILE, "wb") as f:
        f.write(cipher)
    if os.path.exists(REL_FILE):  # remove any plaintext legacy file
        try:
            os.remove(REL_FILE)
        except OSError:
            pass


def save_relationship(chat_id, rel_type, notes):
    if DEMO:
        _DEMO_REL[str(chat_id)] = {"type": rel_type, "notes": notes or ""}
        return
    if _REL_CACHE is None:
        raise RuntimeError("Vault is locked. Set/enter your vault password in Settings (⚙) "
                           "to save encrypted relationship notes.")
    _REL_CACHE[str(chat_id)] = {"type": rel_type, "notes": notes or ""}
    _vault_write()


def relationship_for(chat_id):
    return load_relationships().get(str(chat_id), {"type": "", "notes": ""})


# --- AI result cache (insights / reply / critique) --------------------------

def get_ai(chat_id, kind):
    return _AI_CACHE.get(str(chat_id), {}).get(kind)


def set_ai(chat_id, kind, data, rng=None):
    """Cache an AI result in memory, and persist into the vault when unlocked."""
    entry = {"data": data, "at": fmt_dt(datetime.datetime.now()), "range": rng}
    _AI_CACHE.setdefault(str(chat_id), {})[kind] = entry
    if _REL_CACHE is not None and SETTINGS.get("vault_pw"):
        _REL_CACHE.setdefault("_ai", {}).setdefault(str(chat_id), {})[kind] = entry
        try:
            _vault_write()
        except Exception:
            pass
    return entry


def _restore_settings():
    """Pull saved api_key/model out of the (decrypted) vault into memory."""
    s = (_REL_CACHE or {}).get("_settings", {})
    if s.get("api_key"):
        SETTINGS["api_key"] = s["api_key"]
    if s.get("model"):
        SETTINGS["model"] = s["model"]


def persist_settings():
    """Save api_key/model into the encrypted vault. Returns True if persisted."""
    if _REL_CACHE is None or not SETTINGS.get("vault_pw"):
        return False
    _REL_CACHE["_settings"] = {"api_key": SETTINGS["api_key"], "model": SETTINGS["model"]}
    try:
        _vault_write()
        return True
    except Exception:
        return False


def get_qa(chat_id):
    if DEMO:
        return _DEMO_QA.get(str(chat_id), [])
    return _AI_CACHE.get(str(chat_id), {}).get("qa", [])


def add_qa(chat_id, question, answer, rng):
    item = {"q": question, "a": answer, "range": rng,
            "at": fmt_dt(datetime.datetime.now())}
    if DEMO:
        _DEMO_QA.setdefault(str(chat_id), []).append(item)
        return item
    _AI_CACHE.setdefault(str(chat_id), {}).setdefault("qa", []).append(item)
    if _REL_CACHE is not None and SETTINGS.get("vault_pw"):
        _REL_CACHE.setdefault("_ai", {}).setdefault(str(chat_id), {}) \
            .setdefault("qa", []).append(item)
        try:
            _vault_write()
        except Exception:
            pass
    return item


# ---------------------------------------------------------------------------
# Imported conversations. Pasted/uploaded chat logs stored ONLY in the encrypted
# vault — never written to Messages. They appear as threads with id "imp:<n>".
# ---------------------------------------------------------------------------

_TS_RE = re.compile(
    r"^\s*\[?(\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T,]+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?)\]?\s*")
_SENDER_RE = re.compile(r"^([^:/\n]{1,40}?)\s*:\s+(.*)$")

# macOS Messages "transcript export" style: a date line, then a sender line, then
# the body — e.g. "Sep 05, 2022  1:44:40 PM (Read by them...)" / "Me" / "Test".
_BLOCK_TS = re.compile(
    r"^[A-Z][a-z]{2,9}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp][Mm]\b")
_TAPBACK = re.compile(r"^(Loved|Liked|Laughed|Emphasized|Disliked|Questioned|Reacted)\b", re.I)


def _fmt_block_ts(line):
    core = re.sub(r"\s*\(.*?\)\s*$", "", line).strip()      # drop "(Read by ...)"
    norm = re.sub(r"\s+", " ", core)
    for fmt in ("%b %d, %Y %I:%M:%S %p", "%b %d, %Y %I:%M %p"):
        try:
            return datetime.datetime.strptime(norm, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return norm


def _parse_block(text, me_name, contact_name):
    me = (me_name or "Me").strip().lower()
    lines = text.split("\n")
    msgs, cur, in_tapbacks, i = [], None, False, 0
    while i < len(lines):
        s = lines[i].strip(); i += 1
        if not s:
            continue
        if _BLOCK_TS.match(s):
            ts = _fmt_block_ts(s)
            sender = None
            while i < len(lines):                 # next non-empty line is the sender
                cand = lines[i].strip(); i += 1
                if cand:
                    sender = cand; break
            if sender is None:
                break
            is_me = sender.lower() in (me, "me")
            cur = {"timestamp": ts, "is_from_me": is_me,
                   "sender": "Me" if is_me else contact_name, "text": ""}
            msgs.append(cur); in_tapbacks = False
            continue
        if s.startswith("Tapbacks:"):
            in_tapbacks = True; continue
        if in_tapbacks or _TAPBACK.match(s):       # skip reaction lines
            continue
        if cur is not None:                        # body line for current message
            body = s.lstrip("￼").strip()      # strip attachment placeholder ￼
            if body:
                cur["text"] = (cur["text"] + "\n" + body).strip() if cur["text"] else body
    for m in msgs:
        if not m["text"]:
            m["text"] = "[attachment]"
    msgs.sort(key=lambda m: m["timestamp"])        # threaded replies → chronological
    return msgs


def parse_chat_text(text, me_name, contact_name):
    lines = text.splitlines()
    if sum(1 for ln in lines[:400] if _BLOCK_TS.match(ln.strip())) >= 3:
        return _parse_block(text, me_name, contact_name)
    me = (me_name or "Me").strip().lower()
    msgs, cur = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        ts = ""
        m = _TS_RE.match(line)
        if m:
            ts = m.group(1).strip()
            line = line[m.end():]
        sm = _SENDER_RE.match(line)
        if sm:
            sender = sm.group(1).strip()
            is_me = sender.lower() in (me, "me", "you")
            cur = {"timestamp": ts or "imported", "is_from_me": is_me,
                   "sender": "Me" if is_me else (sender or contact_name),
                   "text": sm.group(2).strip()}
            msgs.append(cur)
        elif cur:                      # continuation of the previous message
            cur["text"] += "\n" + line.strip()
        else:                          # leading line with no sender → from contact
            msgs.append({"timestamp": ts or "imported", "is_from_me": False,
                         "sender": contact_name, "text": line.strip()})
            cur = msgs[-1]
    return msgs


def list_imported():
    return (_REL_CACHE or {}).get("_imported", {}) if _REL_CACHE is not None else {}


def add_imported(title, me_name, text):
    if _REL_CACHE is None:
        raise RuntimeError("Vault is locked. Set/enter your vault password (⚙) to import — "
                           "imported chats are stored encrypted.")
    messages = parse_chat_text(text, me_name, title)
    if not messages:
        raise RuntimeError("Couldn't find any messages in that text.")
    impid = "imp:" + str(int(datetime.datetime.now().timestamp() * 1000))
    _REL_CACHE.setdefault("_imported", {})[impid] = {
        "title": title, "me_name": me_name or "Me", "messages": messages,
        "created": fmt_dt(datetime.datetime.now())}
    _vault_write()
    return impid, len(messages)


def get_imported_thread(impid, since_ns=None):
    e = list_imported().get(impid)
    if not e:
        return None
    msgs = e["messages"]
    if since_ns is not None:
        cutoff = datetime.datetime.fromtimestamp(
            APPLE_EPOCH + since_ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")
        msgs = [m for m in msgs if m["timestamp"] >= cutoff]
    img = image_url_for(e["title"])
    out = [{**m, "img": None if m["is_from_me"] else img} for m in msgs]
    return {"id": impid, "title": e["title"], "is_group": False, "imported": True,
            "img": img, "participants": [e["title"]],
            "relationship": relationship_for(impid), "messages": out}


def fetch_thread(idv, since_ns=None):
    """Dispatch to an imported conversation or a real Messages thread."""
    if DEMO:
        return _demo_get(idv)
    s = str(idv)
    return get_imported_thread(s, since_ns) if s.startswith("imp:") \
        else get_thread(int(s), since_ns)


def _person_handle_rowids(con, primary):
    """All handle ROWIDs for one person: same number/email, or same contact name
    (so a person split across phone + iCloud email is unified)."""
    target = normalize_handle(primary)
    name = contacts().get(target)
    ids = []
    for r in con.execute("SELECT ROWID, id FROM handle"):
        nh = normalize_handle(r["id"])
        if nh == target or (name and contacts().get(nh) == name):
            ids.append(r["ROWID"])
    return ids


def person_full_history(chat_id):
    """Every 1:1 message with a person, merged across ALL their chat threads/handles."""
    con = open_db()
    try:
        crow = con.execute("SELECT chat_identifier, display_name, style FROM chat WHERE ROWID=?",
                           (chat_id,)).fetchone()
        if not crow:
            return None
        handles = [r["handle"] for r in con.execute(
            "SELECT h.id AS handle FROM chat_handle_join chj JOIN handle h ON h.ROWID=chj.handle_id "
            "WHERE chj.chat_id=?", (chat_id,))]
        # Group chats: don't aggregate (would mix in other people) — return that chat.
        if crow["style"] == 43 or len(handles) > 1:
            t = get_thread(chat_id)
            return {"title": t["title"], "is_group": True, "participants": t["participants"],
                    "relationship": t["relationship"], "messages": t["messages"],
                    "chat_count": 1} if t else None
        primary = handles[0] if handles else crow["chat_identifier"]
        title = crow["display_name"] or display_for_handle(primary)
        hids = _person_handle_rowids(con, primary)
        if hids:
            qm = ",".join("?" * len(hids))
            chat_ids = [row[0] for row in con.execute(
                f"""SELECT DISTINCT chj.chat_id FROM chat_handle_join chj
                    WHERE chj.handle_id IN ({qm}) AND chj.chat_id IN
                      (SELECT chat_id FROM chat_handle_join GROUP BY chat_id HAVING COUNT(*)=1)""",
                hids).fetchall()]
        else:
            chat_ids = []
        if not chat_ids:
            chat_ids = [chat_id]
        cm = ",".join("?" * len(chat_ids))
        rows = con.execute(f"""
            SELECT m.ROWID AS id, m.date, m.is_from_me, m.text, m.attributedBody,
                   m.cache_has_attachments, m.service
            FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id
            WHERE cmj.chat_id IN ({cm}) ORDER BY m.date ASC""", chat_ids).fetchall()
        seen, messages = set(), []
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            txt = message_text(r)
            if not txt and r["cache_has_attachments"]:
                txt = "[attachment]"
            if not txt:
                continue
            messages.append({"timestamp": fmt_dt(apple_time_to_dt(r["date"])),
                             "is_from_me": bool(r["is_from_me"]),
                             "sender": "Me" if r["is_from_me"] else title,
                             "text": txt, "service": r["service"]})
        return {"title": title, "is_group": False, "participants": [title],
                "relationship": relationship_for(chat_id), "messages": messages,
                "chat_count": len(chat_ids)}
    finally:
        con.close()


def _person_bundle(idv):
    """(title, messages, chat_count, relationship) for a person, any source."""
    s = str(idv)
    if DEMO:
        t = _demo_get(s)
        return (t["title"], t["messages"], 1, t["relationship"]) if t else None
    if s.startswith("imp:"):
        t = get_imported_thread(s)
        return (t["title"], t["messages"], 1, t["relationship"]) if t else None
    ph = person_full_history(int(s))
    return (ph["title"], ph["messages"], ph["chat_count"], ph["relationship"]) if ph else None


# ~120k tokens per part — fits a 200k-token context window with room for the question.
_AI_PART_CHARS = 480_000


def person_ai_zip(idv):
    """Complete history with one person as an AI-optimized, chunked export.

    Format is built for LLM consumption: minimal tokens (one '## date' header per
    day, 'HH:MM Sender: text' lines, first names), each part self-describing and
    sized to fit a large context window. Returns (basename, zip_bytes)."""
    b = _person_bundle(idv)
    if not b:
        return None
    title, msgs, chats, rel = b
    if not msgs:
        return None
    short = (title.split()[0] if title else "Them")
    if short.lower() == "me":
        short = title

    def new_part():
        return {"lines": [], "start": None, "end": None, "chars": 0, "day": None}
    parts, p = [], new_part()
    for m in msgs:
        ts = m["timestamp"]
        day, hm = ts[:10], ts[11:16]
        sender = "Me" if m["is_from_me"] else short
        block = (f"## {day}\n" if day != p["day"] else "") + f"{hm} {sender}: {m['text']}\n"
        if p["chars"] + len(block) > _AI_PART_CHARS and p["lines"]:
            parts.append(p)
            p = new_part()
            block = f"## {day}\n{hm} {sender}: {m['text']}\n"
        if p["start"] is None:
            p["start"] = ts
        p["lines"].append(block)
        p["chars"] += len(block)
        p["end"] = ts
        p["day"] = day
    if p["lines"]:
        parts.append(p)

    n = len(parts)
    base = slugify(title)
    span = f"{msgs[0]['timestamp'][:10]} to {msgs[-1]['timestamp'][:10]}"
    rel_line = f"# Relationship (per the user): {rel['type']}\n" if rel.get("type") else ""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, pt in enumerate(parts, 1):
            header = (f"# Conversation with {title}\n"
                      f"# Part {i} of {n} — {pt['start'][:10]} to {pt['end'][:10]}\n"
                      f"# {len(msgs):,} total messages ({span})."
                      f" \"Me\" = the account owner; the other speaker is {short}.\n"
                      + rel_line +
                      f"# Format: \"## YYYY-MM-DD\" date headers, then \"HH:MM Sender: message\".\n"
                      f"# Real chat history provided for AI analysis.\n\n")
            z.writestr(f"{base}_AI_part{i:02d}_of_{n:02d}.md", header + "".join(pt["lines"]))
        z.writestr("README.md",
            f"# AI-optimized export — conversation with {title}\n\n"
            f"- {len(msgs):,} messages ({span}), split into {n} chronological part(s)"
            + (f", merged from {chats} chat threads" if chats > 1 else "") + ".\n"
            f"- Each part fits a large AI context window (~120k tokens).\n"
            f"- Compact format: one date header per day, 'HH:MM Sender: message' lines.\n\n"
            f"## How to use\n"
            f"- One period: upload a single part and ask your question.\n"
            f"- Whole history: have the AI summarize each part in order, then combine "
            f"the summaries into an overall analysis.\n")
    return base, buf.getvalue()


def person_zip(idv):
    """Build a .zip of a person's COMPLETE history (.md + .txt + .json). Returns (name, bytes)."""
    b = _person_bundle(idv)
    if not b:
        return None
    title, msgs, chats, rel = b
    thread = {"title": title, "is_group": False, "participants": [title],
              "relationship": rel, "messages": msgs}
    base = slugify(title)
    md = thread_to_markdown(thread)
    txt = "\n".join(f"[{m['timestamp']}] {m['sender']}: {m['text']}" for m in msgs)
    js = json.dumps({"title": title, "merged_chat_threads": chats,
                     "message_count": len(msgs), "messages": msgs}, indent=2)
    note = (f"All messages with {title}.\n{len(msgs)} messages"
            + (f", merged from {chats} chat threads.\n" if chats > 1 else ".\n")
            + "Files: .md (AI-ready), .txt (plain), .json (structured).\n"
              "Generated by iMessage Insights.\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{base}.md", md)
        z.writestr(f"{base}.txt", txt)
        z.writestr(f"{base}.json", js)
        z.writestr("README.txt", note)
    return base, buf.getvalue()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def list_threads():
    if DEMO:
        return _demo_list()
    con = open_db()
    try:
        rows = con.execute("""
            SELECT c.ROWID AS id, c.chat_identifier, c.display_name, c.style,
                   MAX(m.date) AS last_date, COUNT(m.ROWID) AS msg_count
            FROM chat c
            JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            JOIN message m ON m.ROWID = cmj.message_id
            GROUP BY c.ROWID ORDER BY last_date DESC
        """).fetchall()
        parts = {}
        for r in con.execute("""
            SELECT chj.chat_id AS cid, h.id AS handle
            FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id"""):
            parts.setdefault(r["cid"], []).append(r["handle"])
        rels = load_relationships()
        out = []
        for r in rows:
            handles = parts.get(r["id"], [])
            is_group = (r["style"] == 43) or len(handles) > 1
            if r["display_name"]:
                title = r["display_name"]
            elif is_group:
                title = ", ".join(display_for_handle(h) for h in handles) or \
                        (r["chat_identifier"] or "Group")
            else:
                title = display_for_handle(handles[0] if handles else r["chat_identifier"])
            primary = handles[0] if handles else r["chat_identifier"]
            out.append({
                "id": r["id"], "title": title, "is_group": is_group,
                "img": None if is_group else image_url_for(primary),
                "participants": [display_for_handle(h) for h in handles] or
                                [display_for_handle(r["chat_identifier"])],
                "last_date": fmt_dt(apple_time_to_dt(r["last_date"])),
                "last_raw": r["last_date"], "msg_count": r["msg_count"],
                "relationship": rels.get(str(r["id"]), {}).get("type", ""),
            })
        # Imported conversations (vault-only) appear at the top, marked.
        imported = []
        for impid, e in list_imported().items():
            imported.append({
                "id": impid, "title": e["title"], "is_group": False, "imported": True,
                "img": image_url_for(e["title"]), "participants": [e["title"]],
                "last_date": e.get("created", "imported"), "last_raw": 0,
                "msg_count": len(e["messages"]),
                "relationship": rels.get(impid, {}).get("type", ""),
            })
        imported.sort(key=lambda t: t["title"].lower())
        return imported + out
    finally:
        con.close()


def range_cutoff_ns(rng):
    """Map a range name to an Apple-time (ns) cutoff, or None for all-time."""
    days = {"day": 1, "week": 7, "month": 30, "year": 365}.get(rng)
    if not days:
        return None
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    return int((cutoff.timestamp() - APPLE_EPOCH) * 1e9)


def get_thread(chat_id, since_ns=None):
    con = open_db()
    try:
        crow = con.execute(
            "SELECT ROWID AS id, chat_identifier, display_name, style FROM chat WHERE ROWID=?",
            (chat_id,)).fetchone()
        if not crow:
            return None
        handles = [r["handle"] for r in con.execute(
            "SELECT h.id AS handle FROM chat_handle_join chj "
            "JOIN handle h ON h.ROWID = chj.handle_id WHERE chj.chat_id=?", (chat_id,))]
        is_group = (crow["style"] == 43) or len(handles) > 1
        if crow["display_name"]:
            title = crow["display_name"]
        elif is_group:
            title = ", ".join(display_for_handle(h) for h in handles) or "Group"
        else:
            title = display_for_handle(handles[0] if handles else crow["chat_identifier"])
        base = ("SELECT m.ROWID AS id, m.date, m.is_from_me, m.text, m.attributedBody, "
                "m.cache_has_attachments, m.service, h.id AS handle "
                "FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id "
                "LEFT JOIN handle h ON h.ROWID = m.handle_id WHERE cmj.chat_id=?")
        if since_ns is not None:
            rows = con.execute(base + " AND m.date >= ? ORDER BY m.date ASC",
                               (chat_id, since_ns)).fetchall()
        else:
            rows = con.execute(base + " ORDER BY m.date ASC", (chat_id,)).fetchall()
        messages = []
        for r in rows:
            txt = message_text(r)
            if not txt and r["cache_has_attachments"]:
                txt = "[attachment]"
            if not txt:
                continue
            messages.append({
                "timestamp": fmt_dt(apple_time_to_dt(r["date"])),
                "is_from_me": bool(r["is_from_me"]),
                "sender": "Me" if r["is_from_me"] else display_for_handle(r["handle"]),
                "img": None if r["is_from_me"] else image_url_for(r["handle"]),
                "text": txt, "service": r["service"],
            })
        rel = relationship_for(chat_id)
        primary = handles[0] if handles else crow["chat_identifier"]
        return {"id": chat_id, "title": title, "is_group": is_group,
                "img": None if is_group else image_url_for(primary),
                "participants": [display_for_handle(h) for h in handles],
                "relationship": rel, "messages": messages}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Export formatting
# ---------------------------------------------------------------------------

def thread_to_markdown(thread):
    msgs = thread["messages"]
    L = [f"# Conversation: {thread['title']}", ""]
    L.append(f"- **Type:** {'Group chat' if thread['is_group'] else 'Direct message'}")
    if thread["participants"]:
        L.append(f"- **Participants:** {', '.join(thread['participants'])}")
    if thread.get("relationship", {}).get("type"):
        L.append(f"- **Relationship:** {thread['relationship']['type']}")
    if msgs:
        L.append(f"- **Date range:** {msgs[0]['timestamp']} → {msgs[-1]['timestamp']}")
    L.append(f"- **Message count:** {len(msgs)}")
    L += ["", "---", ""]
    for m in msgs:
        body = m["text"].replace("\r\n", "\n").replace("\r", "\n")
        body = "\n".join(("  " + ln) if i else ln for i, ln in enumerate(body.split("\n")))
        L.append(f"[{m['timestamp']}] {m['sender']}: {body}")
    L.append("")
    return "\n".join(L)


def all_threads_markdown():
    threads = list_threads()
    parts = ["# Apple Messages Export",
             f"_Generated {fmt_dt(datetime.datetime.now())} · {len(threads)} threads_", ""]
    for t in threads:
        full = fetch_thread(t["id"])
        if full and full["messages"]:
            parts.append(thread_to_markdown(full)); parts.append("\n---\n")
    return "\n".join(parts)


def thread_to_print_html(thread):
    rows = []
    for m in thread["messages"]:
        cls = "me" if m["is_from_me"] else "them"
        rows.append(f'<div class="msg {cls}"><div class="meta">{html.escape(m["sender"])} · '
                    f'{html.escape(m["timestamp"])}</div><div class="body">'
                    f'{html.escape(m["text"]).replace(chr(10), "<br>")}</div></div>')
    meta = [f"Type: {'Group chat' if thread['is_group'] else 'Direct message'}"]
    if thread["participants"]:
        meta.append("Participants: " + html.escape(", ".join(thread["participants"])))
    if thread.get("relationship", {}).get("type"):
        meta.append("Relationship: " + html.escape(thread["relationship"]["type"]))
    msgs = thread["messages"]
    if msgs:
        meta.append(f"Date range: {msgs[0]['timestamp']} → {msgs[-1]['timestamp']}")
    meta.append(f"Messages: {len(msgs)}")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(thread['title'])}</title><style>
  body {{ font:13px -apple-system,system-ui,sans-serif; max-width:760px; margin:32px auto;
          color:#111; padding:0 16px; }}
  h1 {{ font-size:22px; }} .summary {{ color:#555; font-size:12px; margin-bottom:20px;
          line-height:1.6; }}
  .msg {{ margin:10px 0; }} .meta {{ font-size:11px; color:#888; margin-bottom:2px; }}
  .body {{ display:inline-block; padding:8px 12px; border-radius:14px; background:#e9e9eb;
           white-space:pre-wrap; }}
  .me {{ text-align:right; }} .me .body {{ background:#007aff; color:#fff; }}
  .bar {{ position:sticky; top:0; background:#fff; padding:10px 0; border-bottom:1px solid #eee; }}
  button {{ font-size:14px; padding:8px 16px; cursor:pointer; }}
  @media print {{ .bar {{ display:none; }} }}
</style></head><body>
<div class="bar"><button onclick="window.print()">Save as PDF / Print</button></div>
<h1>{html.escape(thread['title'])}</h1>
<div class="summary">{'<br>'.join(meta)}</div>
{''.join(rows)}
<script>setTimeout(()=>window.print(), 400);</script></body></html>"""


# ---------------------------------------------------------------------------
# Encryption (AES-256-CBC + PBKDF2 via openssl; passphrase never hits disk/argv)
# ---------------------------------------------------------------------------

def encrypt_bytes(data, passphrase):
    if isinstance(data, str):
        data = data.encode("utf-8")
    env = dict(os.environ, IMSG_PW=passphrase)
    p = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", "env:IMSG_PW"],
        input=data, capture_output=True, env=env)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace"))
    return p.stdout


def all_threads_zip():
    """Bundle every conversation as its own .md inside one ZIP (bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in list_threads():
            full = fetch_thread(t["id"])
            if full and full["messages"]:
                name = f"{slugify(full['title'])}_{t['id']}.md"
                z.writestr(name, thread_to_markdown(full))
        z.writestr("_index.md", all_threads_markdown())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Claude API (stdlib HTTP — no SDK needed)
# ---------------------------------------------------------------------------

def call_claude(system, user_text, max_tokens=1800):
    key = SETTINGS["api_key"]
    if not key:
        raise RuntimeError("No Anthropic API key set. Open Settings (⚙) and paste your key.")
    body = json.dumps({
        "model": SETTINGS["model"], "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except Exception:
            pass
        raise RuntimeError(f"AI API error ({e.code}): {detail}")
    return "".join(b.get("text", "") for b in resp.get("content", []))


def extract_json(raw):
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise RuntimeError("The AI did not return JSON.")
    return json.loads(m.group(0))


INSIGHTS_SYSTEM = """You are a thoughtful communication and relationship analyst.
You are given a transcript of a real iMessage conversation between "Me" and one or
more other people, plus how the user classifies this relationship. Analyze honestly
and constructively. Tailor advice to the relationship type: be warm and casual for
friends/family, careful and professional for colleagues/clients, emotionally attuned
for romantic relationships. Never be creepy or manipulative; suggest authentic,
respectful communication. Return ONLY a JSON object, no prose, with EXACTLY these keys:
{
  "summary": "2-3 sentence overview of what this conversation is about and its arc",
  "tone": "the overall emotional tone (e.g. warm, tense, transactional, playful)",
  "relationship_health": "1-2 sentences on how the relationship seems to be going",
  "their_style": "how the other person communicates (pace, warmth, directness)",
  "key_topics": ["short", "topic", "phrases"],
  "open_threads": ["anything left unanswered or that needs follow-up"],
  "suggested_replies": [
    {"strategy": "name of the approach", "when": "when to use it", "example": "a ready-to-send example reply"}
  ],
  "watch_outs": ["things to be mindful of given the relationship type"]
}
Provide 3 suggested_replies with distinct strategies suited to the relationship type."""


def build_insights(thread):
    if DEMO:
        return DEMO_INSIGHT
    msgs = thread["messages"][-220:]
    transcript = "\n".join(f"[{m['timestamp']}] {m['sender']}: {m['text']}" for m in msgs)
    transcript = transcript[:42000]
    rel = thread.get("relationship", {})
    user = (f"Relationship type the user assigned: {rel.get('type') or 'unspecified'}\n"
            f"User's private notes about this person: {rel.get('notes') or '(none)'}\n"
            f"Conversation participants: {', '.join(thread['participants']) or thread['title']}\n"
            f"Showing the most recent {len(msgs)} messages.\n\n"
            f"TRANSCRIPT:\n{transcript}")
    return extract_json(call_claude(INSIGHTS_SYSTEM, user))


REPLY_SYSTEM = """You help the user ("Me") write an authentic, well-judged reply to the
most recent messages in this conversation. Honor the relationship type and the user's
private context notes. Match an appropriate register (casual for friends/family,
careful for colleagues/clients, emotionally attuned for romantic/ex situations). Be
genuine and respectful — never manipulative. If the user gave an instruction about what
they want to convey or the vibe, follow it. Return ONLY JSON:
{
  "replies": [
    {"tone": "short label for this option", "text": "the ready-to-send reply"}
  ],
  "tips": ["brief tips on delivery / timing"]
}
Offer 3 distinct reply options."""

CRITIQUE_SYSTEM = """You are a candid but kind communication coach. Look ONLY at the
messages the user ("Me") recently sent in this conversation and critique how the user
communicates — clarity, tone, warmth, responsiveness, anything that might land badly
given the relationship type and context notes. Be specific and constructive. Return
ONLY JSON:
{
  "overall": "1-2 sentence honest summary of how the user comes across",
  "strengths": ["what the user does well"],
  "improvements": ["specific things to improve, with why"],
  "rewrites": [{"original": "a real recent message of theirs", "better": "an improved version"}]
}
Include 2-3 rewrites drawn from their actual recent messages."""


def _assist_context(thread, tail):
    msgs = thread["messages"][-tail:]
    transcript = "\n".join(f"[{m['timestamp']}] {m['sender']}: {m['text']}" for m in msgs)
    rel = thread.get("relationship", {})
    return (f"Relationship type: {rel.get('type') or 'unspecified'}\n"
            f"User's private context notes: {rel.get('notes') or '(none)'}\n"
            f"Participants: {', '.join(thread['participants']) or thread['title']}\n\n"
            f"RECENT TRANSCRIPT (last {len(msgs)} messages):\n{transcript[:38000]}")


def build_reply(thread, instruction):
    if DEMO:
        return DEMO_REPLY
    user = _assist_context(thread, 80)
    if instruction and instruction.strip():
        user += f"\n\nWhat the user wants to convey / the vibe: {instruction.strip()}"
    user += "\n\nDraft replies the user could send next."
    return extract_json(call_claude(REPLY_SYSTEM, user, 1400))


def build_critique(thread):
    if DEMO:
        return DEMO_CRITIQUE
    user = _assist_context(thread, 60) + "\n\nCritique the user's own recent messages."
    return extract_json(call_claude(CRITIQUE_SYSTEM, user, 1400))


ASK_SYSTEM = """You answer the user's specific question about a conversation, using ONLY the
messages provided (a windowed excerpt — possibly just part of the full history). Be concrete:
quote or paraphrase relevant messages and note approximate dates. If the provided messages
don't contain the answer, say so plainly rather than guessing. Take the relationship type and
the user's context notes into account. Keep the answer focused and useful."""


def build_answer(thread, question):
    if DEMO:
        return ("(demo answer) Based on the messages in view, here's what stands out for "
                f"“{question}”: the exchange is friendly and low-pressure, with a "
                "light nostalgic undertone. Nothing here is explicitly romantic, and the "
                "open-ended coffee suggestion keeps things casual. Connect a real API key to "
                "get answers grounded in your actual conversation.")
    msgs = thread["messages"][-300:]
    transcript = "\n".join(f"[{m['timestamp']}] {m['sender']}: {m['text']}" for m in msgs)[:42000]
    rel = thread.get("relationship", {})
    user = (f"Relationship type: {rel.get('type') or 'unspecified'}\n"
            f"User's context notes: {rel.get('notes') or '(none)'}\n"
            f"Messages currently in view ({len(msgs)} shown):\n{transcript}\n\n"
            f"QUESTION: {question}\n\nAnswer using only the messages above.")
    return call_claude(ASK_SYSTEM, user, 1200)


# Approximate Anthropic pricing, USD per 1M tokens: (input, output).
PRICES = {
    "claude-opus-4-8": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


def estimate_cost(thread, kind):
    if DEMO:
        return {"model": SETTINGS["model"], "input_tokens": 1180,
                "output_tokens": 1800, "cost_usd": 0.0121}
    # (messages used, char cap, max output tokens, system-prompt token allowance)
    n, cap, out, sys_tok = {
        "insights": (220, 42000, 1800, 700),
        "reply":    (80, 38000, 1400, 450),
        "critique": (60, 38000, 1400, 450),
        "ask":      (300, 42000, 1200, 400),
    }.get(kind, (80, 38000, 1400, 450))
    msgs = thread["messages"][-n:]
    transcript = "\n".join(f"[{m['timestamp']}] {m['sender']}: {m['text']}" for m in msgs)[:cap]
    in_tok = len(transcript) // 4 + sys_tok           # ~4 chars/token heuristic
    in_p, out_p = PRICES.get(SETTINGS["model"], (3.0, 15.0))
    cost = in_tok / 1e6 * in_p + out / 1e6 * out_p
    return {"model": SETTINGS["model"], "input_tokens": in_tok,
            "output_tokens": out, "cost_usd": round(cost, 4)}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def slugify(s):
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", s or "thread").strip("_")[:60] or "thread"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _access_error(self, exc):
        self._send(403,
            f"<b>Cannot read Messages database.</b><br><br>{html.escape(str(exc))}<br><br>"
            "Grant <b>Full Disk Access</b> to your terminal: System Settings → Privacy &amp; "
            "Security → Full Disk Access → enable <code>Terminal</code> (or iTerm), then fully "
            "quit &amp; reopen it and rerun <code>python3 server.py</code>.")

    # ---- GET --------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send(200, INDEX_HTML)
            if u.path == "/api/threads":
                return self._json(list_threads())
            if u.path == "/api/thread":
                rng = qs.get("range", ["month"])[0]
                t = fetch_thread(qs["id"][0], range_cutoff_ns(rng))
                if t and len(t["messages"]) > 800:   # cap the VIEW; analysis/export use all
                    t["truncated"] = len(t["messages"])
                    t["messages"] = t["messages"][-800:]
                return self._json(t) if t else self._json({"error": "not found"}, 404)
            if u.path == "/api/settings":
                return self._json({"has_key": bool(SETTINGS["api_key"]),
                                   "model": SETTINGS["model"],
                                   "persisted": bool((_REL_CACHE or {}).get(
                                       "_settings", {}).get("api_key")),
                                   "relationship_types": RELATIONSHIP_TYPES,
                                   "vault": vault_status()})
            if u.path == "/api/vault":
                return self._json(vault_status())
            if u.path == "/api/status":
                ok, err = db_readable()
                return self._json({"demo": DEMO, "db_ok": ok, "db_error": err,
                                   "has_key": bool(SETTINGS["api_key"]),
                                   "vault": vault_status()})
            if u.path == "/api/estimate":
                t = fetch_thread(qs["id"][0], range_cutoff_ns(qs.get("range", ["all"])[0]))
                if not t or not t["messages"]:
                    return self._json({"error": "no messages"}, 400)
                if not SETTINGS["api_key"]:
                    return self._json({"error": "no_key"}, 200)
                return self._json(estimate_cost(t, qs.get("kind", ["insights"])[0]))
            if u.path == "/api/ai-cache":
                cid = qs["id"][0]
                def pack(kind):
                    c = get_ai(cid, kind)
                    return {**c["data"], "at": c["at"], "range": c.get("range"),
                            "cached": True} if c else None
                return self._json({"insights": pack("insights"), "qa": get_qa(cid)})
            if u.path == "/api/avatar":
                img = get_contact_image(qs.get("handle", [""])[0])
                if not img:
                    return self._send(404, b"", "text/plain")
                ctype = "image/png" if img[:8] == _PNG else "image/jpeg"
                return self._send(200, img, ctype, {"Cache-Control": "max-age=3600"})
            if u.path == "/export/md":
                t = fetch_thread(qs["id"][0])
                if not t: return self._json({"error": "not found"}, 404)
                fn = slugify(t["title"]) + ".md"
                return self._send(200, thread_to_markdown(t), "text/markdown; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{fn}"'})
            if u.path == "/export/all":
                return self._send(200, all_threads_markdown(), "text/markdown; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="imessages_all.md"'})
            if u.path == "/export/print":
                t = fetch_thread(qs["id"][0])
                if not t: return self._json({"error": "not found"}, 404)
                return self._send(200, thread_to_print_html(t))
            if u.path == "/export/person-zip":
                res = person_zip(qs["id"][0])
                if not res: return self._json({"error": "not found"}, 404)
                base, data = res
                return self._send(200, data, "application/zip",
                    {"Content-Disposition": f'attachment; filename="{base}_all_messages.zip"'})
            if u.path == "/export/person-ai":
                res = person_ai_zip(qs["id"][0])
                if not res: return self._json({"error": "not found"}, 404)
                base, data = res
                return self._send(200, data, "application/zip",
                    {"Content-Disposition": f'attachment; filename="{base}_for_AI.zip"'})
            return self._send(404, "Not found")
        except sqlite3.OperationalError as e:
            return self._access_error(e)
        except Exception as e:
            if any(w in str(e).lower() for w in ("denied", "unable to open database", "authoriz")):
                return self._access_error(e)
            return self._send(500, f"Server error: {html.escape(str(e))}")

    # ---- POST -------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        try:
            data = self._body()
            if u.path == "/api/settings":
                if "api_key" in data and data["api_key"].strip():
                    SETTINGS["api_key"] = data["api_key"].strip()
                if data.get("model"):
                    SETTINGS["model"] = data["model"].strip()
                saved = persist_settings()
                return self._json({"has_key": bool(SETTINGS["api_key"]),
                                   "model": SETTINGS["model"], "persisted": saved})
            if u.path == "/api/vault":
                ok = vault_unlock(data.get("password", ""))
                st = vault_status(); st["ok"] = ok
                return self._json(st)
            if u.path == "/api/open-settings":
                try:
                    subprocess.Popen(["open",
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
                    return self._json({"ok": True})
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
            if u.path == "/api/demo":
                enable_demo()
                return self._json({"ok": True})
            if u.path == "/api/relationship":
                save_relationship(data["id"], data.get("type", ""), data.get("notes", ""))
                return self._json({"ok": True, **relationship_for(data["id"])})
            if u.path == "/api/import":
                impid, n = add_imported(data.get("title", "Imported").strip() or "Imported",
                                        data.get("me_name", "Me"), data.get("text", ""))
                return self._json({"ok": True, "id": impid, "count": n})
            if u.path == "/api/insights":
                cid = data["id"]
                rng = data.get("range", "all")
                if not data.get("force"):
                    c = get_ai(cid, "insights")
                    if c: return self._json({**c["data"], "at": c["at"],
                                             "range": c.get("range"), "cached": True})
                t = fetch_thread(cid, range_cutoff_ns(rng))   # scope to the visible window
                if not t: return self._json({"error": "not found"}, 404)
                if not t["messages"]:
                    return self._json({"error": "No messages in this window to analyze."}, 400)
                d = build_insights(t); e = set_ai(cid, "insights", d, rng)
                return self._json({**d, "at": e["at"], "range": rng, "cached": False})
            if u.path == "/api/assist":
                # Replies/critiques are always generated FRESH (not cached) — the
                # latest messages matter, so re-running re-bills by design.
                cid = data["id"]
                kind = "critique" if data.get("mode") == "critique" else "reply"
                t = fetch_thread(cid, range_cutoff_ns(data.get("range", "all")))
                if not t: return self._json({"error": "not found"}, 404)
                if not t["messages"]:
                    return self._json({"error": "No messages in this window."}, 400)
                d = build_critique(t) if kind == "critique" else build_reply(t, data.get("instruction", ""))
                return self._json(d)
            if u.path == "/api/ask":
                cid = data["id"]
                question = (data.get("question") or "").strip()
                if not question:
                    return self._json({"error": "Type a question first."}, 400)
                rng = data.get("range", "all")
                t = fetch_thread(cid, range_cutoff_ns(rng))
                if not t or not t["messages"]:
                    return self._json({"error": "No messages in this window."}, 400)
                answer = build_answer(t, question)
                item = add_qa(cid, question, answer, rng)
                return self._json(item)
            if u.path == "/export/encrypted-archive":
                if not SETTINGS.get("vault_pw"):
                    return self._json({"error": "Vault is locked. Unlock it (🔒) first — the "
                                       "archive is encrypted with your vault password."}, 400)
                cipher = encrypt_bytes(all_threads_zip(), SETTINGS["vault_pw"])
                return self._send(200, cipher, "application/octet-stream",
                    {"Content-Disposition": 'attachment; filename="imessages_all.zip.enc"'})
            return self._send(404, "Not found")
        except sqlite3.OperationalError as e:
            return self._access_error(e)
        except Exception as e:
            if any(w in str(e).lower() for w in ("denied", "unable to open database", "authoriz")):
                return self._access_error(e)
            return self._json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# Front-end
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iMessage Export & Analyze</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --line:#e4e7eb; --ink:#15181d;
          --muted:#6b7280; --accent:#2f6df6; --accent-weak:#eef3fe; --dark:#171a20; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.45 -apple-system,system-ui,"Segoe UI",sans-serif;
         color:var(--ink); background:var(--bg); display:flex; height:100vh; overflow:hidden;
         -webkit-font-smoothing:antialiased; }
  /* sidebar */
  #side { width:300px; background:var(--card); border-right:1px solid var(--line);
          display:flex; flex-direction:column; }
  .brand { padding:15px 16px; background:var(--card); border-bottom:1px solid var(--line); }
  .brand h2 { margin:0; font-size:15px; font-weight:650; letter-spacing:-.01em; color:var(--ink); }
  .brand .sub { font-size:11px; color:var(--muted); margin-top:2px; }
  .toolrow { display:flex; gap:6px; padding:10px 12px; border-bottom:1px solid var(--line);
             flex-wrap:wrap; }
  #search { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:10px;
            background:var(--bg); }
  #list { overflow-y:auto; flex:1; }
  .thread { padding:11px 14px; border-bottom:1px solid #f1f2f7; cursor:pointer;
            transition:background .12s; }
  .thread:hover { background:#f7f8fc; }
  .thread.active { background:var(--accent-weak); box-shadow:inset 2px 0 0 var(--accent); }
  .thread .t { font-weight:600; display:flex; align-items:center; gap:6px; }
  .thread .s { color:var(--muted); font-size:11.5px; margin-top:3px;
               display:flex; justify-content:space-between; gap:8px; align-items:center; }
  .badge { background:#eef1f4; color:var(--muted); border-radius:20px; padding:1px 8px;
           font-size:10.5px; font-weight:600; }
  .reltag { font-size:10px; background:var(--accent-weak); color:var(--accent); border-radius:20px;
            padding:1px 7px; font-weight:600; }
  /* main */
  #main { flex:1; display:flex; flex-direction:column; overflow:hidden; }
  #toolbar { padding:12px 18px; background:var(--card); border-bottom:1px solid var(--line);
             display:flex; align-items:center; gap:10px; }
  #toolbar h1 { font-size:16px; margin:0; flex:1; display:flex; align-items:center; gap:8px; }
  #relSel { padding:6px 8px; border:1px solid var(--line); border-radius:9px; font-size:12px; }
  #relFilter { padding:7px 9px; border:1px solid var(--line); border-radius:10px;
               background:var(--bg); font-size:12px; max-width:140px; }
  .seg { display:inline-flex; border:1px solid var(--line); border-radius:9px; overflow:hidden; }
  .seg button { border:none; background:var(--card); padding:5px 10px; font-size:11.5px;
                border-radius:0; color:var(--muted); font-weight:600; }
  .seg button:not(:last-child) { border-right:1px solid var(--line); }
  .seg button.on { background:var(--accent); color:#fff; }
  .loadinfo { font-size:11.5px; color:var(--muted); padding:7px 18px; background:#fafbfc;
              border-bottom:1px solid var(--line); display:flex; gap:8px; align-items:center; }
  /* avatar dots */
  .av { width:18px; height:18px; border-radius:50%; display:inline-block; vertical-align:middle;
        margin-right:7px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.07); flex:none; }
  img.av { object-fit:cover; }
  .av.sm { width:11px; height:11px; margin-right:5px; }
  .relicon { font-size:13px; line-height:1; cursor:default; }
  /* privacy: shoulder-surf blur (hover any item to reveal it) */
  body.privacy #msgs .body,
  body.privacy .thread .nm,
  body.privacy #title span,
  body.privacy .ctxlist,
  body.privacy #ibody,
  body.privacy .av { filter:blur(6px); transition:filter .12s; }
  body.privacy #msgs .body:hover,
  body.privacy .thread:hover .nm,
  body.privacy #title:hover span,
  body.privacy .ctxlist:hover,
  body.privacy #ibody:hover { filter:none; }
  /* auto-hide overlay the instant the window loses focus */
  #screen { position:fixed; inset:0; z-index:200; display:none; flex-direction:column;
            align-items:center; justify-content:center; cursor:pointer; color:var(--ink);
            font-size:14px; font-weight:600; background:rgba(244,245,247,.55);
            backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px); }
  #screen.on { display:flex; }
  #screen .lk { font-size:32px; margin-bottom:10px; }
  /* first-run onboarding (no Full Disk Access yet) */
  #onboard { position:fixed; inset:0; z-index:250; display:none; align-items:center;
             justify-content:center; background:var(--bg); }
  #onboard.on { display:flex; }
  .ob-card { background:var(--card); border:1px solid var(--line); border-radius:18px;
             padding:28px 32px; max-width:480px; box-shadow:0 20px 60px rgba(0,0,0,.12); }
  .ob-card .ob-icon { font-size:38px; }
  .ob-card h2 { margin:10px 0 6px; font-size:20px; letter-spacing:-.01em; }
  .ob-card p { color:var(--muted); margin:0 0 14px; line-height:1.5; }
  .ob-card ol { color:var(--ink); font-size:13.5px; line-height:1.8; padding-left:20px; margin:0; }
  .ob-actions { display:flex; gap:8px; margin-top:18px; flex-wrap:wrap; }
  .ob-card details { margin-top:14px; }
  .ob-card summary { cursor:pointer; font-size:11px; color:var(--muted); }
  .thread .t { display:flex; align-items:center; }
  .thread .t .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  /* insights controls */
  #icontrols { padding:12px 16px; border-bottom:1px solid var(--line); background:#fafbfc; }
  .ctxbox { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:9px 11px; }
  .ctxhdr { display:flex; align-items:center; justify-content:space-between; font-size:10.5px;
            text-transform:uppercase; letter-spacing:.5px; color:var(--muted); }
  .ctxlist { margin:7px 0 0; padding-left:17px; font-size:12.5px; }
  .ctxlist li { margin:3px 0; }
  .ctxlist li.muted { list-style:none; margin-left:-17px; color:var(--muted); font-style:italic; }
  button.link { background:none; border:none; color:var(--accent); cursor:pointer;
                font-size:12px; font-weight:600; padding:0; }
  button.link:hover { text-decoration:underline; }
  #ctxEdit { width:100%; padding:9px 10px; border:1px solid var(--line); border-radius:9px;
             font:13px/1.5 -apple-system,system-ui,sans-serif; resize:vertical; }
  .hint { font-size:11px; color:var(--muted); }
  .iact { display:flex; gap:6px; margin-top:8px; }
  .iact input { flex:1; padding:6px 9px; border:1px solid var(--line); border-radius:9px; min-width:0; }
  .iact button { white-space:nowrap; }
  .rewrite { font-size:12.5px; margin:7px 0; }
  .rewrite .o { color:#b00020; text-decoration:line-through; }
  .rewrite .b { color:#0a7d33; margin-top:2px; }
  .cachebar { font-size:11px; color:var(--muted); background:var(--accent-weak);
              border-radius:8px; padding:6px 9px; margin-bottom:10px; line-height:1.5; }
  .scopebar { font-size:11px; color:var(--muted); margin-top:9px; line-height:1.6; }
  .qa h4 { font-weight:700; }
  .qa .ans { white-space:pre-wrap; }
  #toast { position:fixed; bottom:22px; left:50%; transform:translateX(-50%);
           background:var(--ink); color:#fff; padding:9px 16px; border-radius:10px;
           font-size:13px; opacity:0; pointer-events:none; transition:opacity .18s; z-index:300; }
  #toast.show { opacity:.95; }
  #msgs { overflow-y:auto; flex:1; padding:18px 22px; }
  .msg { margin:9px 0; max-width:74%; }
  .msg .meta { font-size:10.5px; color:var(--muted); margin-bottom:3px; }
  .msg .body { padding:9px 13px; border-radius:16px; background:#e9eaf0; white-space:pre-wrap;
               display:inline-block; box-shadow:0 1px 1px rgba(0,0,0,.04); }
  .msg.me { margin-left:auto; text-align:right; }
  .msg.me .body { background:var(--accent); color:#fff; }
  /* insights panel */
  #insights { width:340px; background:var(--card); border-left:1px solid var(--line);
              display:flex; flex-direction:column; overflow:hidden; }
  .ipanel-head { padding:14px 16px; border-bottom:1px solid var(--line);
                 display:flex; align-items:center; gap:8px; }
  .ipanel-head h3 { margin:0; font-size:14px; flex:1; }
  #ibody { overflow-y:auto; flex:1; padding:14px 16px; }
  .icard { background:var(--bg); border:1px solid var(--line); border-radius:12px;
           padding:11px 13px; margin-bottom:11px; }
  .icard h4 { margin:0 0 6px; font-size:11px; text-transform:uppercase; letter-spacing:.5px;
              color:var(--muted); }
  .chip { display:inline-block; background:var(--accent-weak); color:var(--accent); border-radius:20px;
          padding:2px 9px; font-size:11px; margin:2px 4px 2px 0; }
  .reply { border-left:3px solid var(--accent); padding:6px 10px; margin:8px 0;
           background:#f7f9fc; border-radius:0 8px 8px 0; }
  .reply .strat { font-weight:700; font-size:12px; }
  .reply .when { color:var(--muted); font-size:11px; margin:2px 0 5px; }
  .reply .ex { background:#fff; border:1px solid var(--line); border-radius:8px; padding:7px 9px;
               font-size:12.5px; }
  .copy { font-size:10px; cursor:pointer; color:var(--accent); float:right; }
  /* buttons */
  button { padding:7px 13px; border:1px solid var(--line); background:var(--card);
           border-radius:10px; cursor:pointer; font-size:12.5px; font-weight:600; color:var(--ink); }
  button:hover { background:#f3f4fa; }
  button.primary { background:var(--accent); color:#fff; border:none; }
  button.primary:hover { background:#255fd6; }
  button.ghost { background:transparent; border:none; color:var(--muted); font-size:17px;
                 padding:2px 6px; }
  button.ghost:hover { color:var(--ink); }
  .iconbtn { border:none; background:transparent; font-size:16px; cursor:pointer; padding:3px; }
  #empty,#iempty { color:var(--muted); text-align:center; padding:40px 20px; font-size:13px; }
  .err { color:#b00020; padding:22px; line-height:1.6; }
  .err code { background:#f4f4f4; padding:2px 5px; border-radius:4px; }
  .spin { width:22px;height:22px;border:3px solid #e7e8ef;border-top-color:var(--accent);
          border-radius:50%; animation:s 1s linear infinite; margin:30px auto; }
  @keyframes s { to { transform:rotate(360deg);} }
  /* modal */
  .modal { position:fixed; inset:0; background:rgba(20,22,31,.45); display:none;
           align-items:center; justify-content:center; z-index:50; }
  .modal.open { display:flex; }
  .sheet { background:#fff; border-radius:16px; padding:22px; width:380px;
           box-shadow:0 20px 60px rgba(0,0,0,.3); }
  .sheet h3 { margin:0 0 12px; }
  .sheet label { font-size:12px; color:var(--muted); display:block; margin:10px 0 4px; }
  .sheet input,.sheet select { width:100%; padding:9px 10px; border:1px solid var(--line);
           border-radius:9px; }
  .sheet textarea { width:100%; padding:9px 10px; border:1px solid var(--line); border-radius:9px;
           font:13px/1.5 -apple-system,system-ui,sans-serif; resize:vertical; margin-top:6px; }
  .sheet input[type=file] { width:auto; border:none; padding:0; }
  .sheet .row { display:flex; gap:8px; justify-content:flex-end; margin-top:16px; }
  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
  .live { display:flex; align-items:center; gap:5px; font-size:11px; color:var(--muted); }
</style></head><body>

<div id="side">
  <div class="brand">
    <div style="display:flex;align-items:center">
      <div style="flex:1"><h2>iMessage · Analyze</h2>
      <div class="sub">local · read-only · private</div></div>
      <button class="ghost" id="lockBtn" title="Notes vault (locked)" onclick="vaultPrompt()">🔒</button>
      <button class="ghost" title="Settings" onclick="openSettings()">⚙</button>
    </div>
  </div>
  <div class="toolrow">
    <input id="search" placeholder="Filter conversations…" oninput="render()">
    <select id="relFilter" onchange="render()"><option value="">All relationships</option></select>
    <button onclick="openImport()">📥 Import</button>
    <button onclick="exportAll()">⬇︎ All .md</button>
    <button onclick="encryptArchive()">🔒 Encrypt all</button>
    <label class="live"><input type="checkbox" id="initChk" onchange="render()"> initials</label>
    <label class="live"><input type="checkbox" id="privChk" onchange="togglePrivacy()"> 🙈 privacy</label>
    <label class="live"><input type="checkbox" id="liveChk" checked> live</label>
  </div>
  <div id="list"><div class="spin"></div></div>
</div>

<div id="main">
  <div id="toolbar">
    <h1 id="title">Select a conversation</h1>
    <select id="relSel" style="display:none" onchange="saveRel()"></select>
    <button id="btnMd" style="display:none" onclick="exportMd()">⬇︎ .md</button>
    <button id="btnZip" style="display:none" onclick="exportZip()" title="Every message with this person, merged across all their chat threads">⬇︎ All (ZIP)</button>
    <button id="btnAI" style="display:none" onclick="exportAI()" title="Complete history in an AI-optimized format: compact transcript split into context-window-sized parts">🤖 AI export</button>
    <button id="btnPdf" style="display:none" onclick="exportPdf()">🖨 PDF</button>
  </div>
  <div id="loadbar" class="loadinfo" style="display:none">
    <span>Load range:</span>
    <span class="seg" id="rangeSeg">
      <button onclick="setRange('day')">Day</button>
      <button onclick="setRange('week')">Week</button>
      <button onclick="setRange('month')">Month</button>
      <button onclick="setRange('year')">Year</button>
      <button onclick="setRange('all')">All time</button>
    </span>
    <span id="loadcount"></span>
  </div>
  <div id="msgs"><div id="empty">Pick a thread on the left to read it.</div></div>
</div>

<div id="insights">
  <div class="ipanel-head"><h3>✨ AI Insights</h3></div>
  <div id="icontrols" style="display:none">
    <div class="ctxbox">
      <div class="ctxhdr"><span>🔒 Context</span>
        <button class="link" onclick="openCtx()">＋ Add / Edit</button></div>
      <ul id="ctxList" class="ctxlist"><li class="muted">No context added yet.</li></ul>
    </div>
    <div class="iact">
      <button class="primary" onclick="analyze()">✨ Analyze</button>
      <button onclick="critique()">🔎 Critique mine</button>
    </div>
    <div class="iact">
      <input id="replyInstruction" placeholder="(optional) what you want to say / the vibe">
      <button onclick="draftReply()">💬 Draft reply</button>
    </div>
    <div class="iact">
      <input id="askBox" placeholder="Ask a question about the messages in view…"
             onkeydown="if(event.key==='Enter')ask()">
      <button onclick="ask()">❓ Ask</button>
    </div>
    <div class="scopebar">Analyses use the <b id="scopeLbl">current</b> view to save tokens ·
      <button class="link" id="qaLink" onclick="showQA()" style="display:none"></button></div>
  </div>
  <div id="ibody"><div id="iempty">Select a conversation, classify the relationship and add
    any context, then use <b>Analyze</b>, <b>Draft reply</b>, or <b>Critique mine</b>.</div></div>
</div>

<div class="modal" id="importModal">
  <div class="sheet">
    <h3>Import a conversation</h3>
    <div class="hint">Paste or upload a chat log. Stored <b>encrypted in your vault</b> — never
      written to Messages. One message per line, e.g. <code>Alex: hey</code> or
      <code>[2024-03-01 14:02] Me: hi</code>.</div>
    <label>Contact name</label>
    <input id="impTitle" placeholder="e.g. Alex Rivera">
    <label>Your name in the log (lines from this name are marked “Me”)</label>
    <input id="impMe" value="Me">
    <label>Conversation text &nbsp;
      <input type="file" id="impFile" accept=".txt,.md,.csv,text/plain" style="font-size:11px"></label>
    <textarea id="impText" rows="9"
      placeholder="[2024-03-01 14:02] Alex: hey!&#10;[2024-03-01 14:03] Me: hi :)&#10;Alex: how have you been?"></textarea>
    <div class="row"><button onclick="closeImport()">Cancel</button>
      <button class="primary" onclick="doImport()">Import</button></div>
  </div>
</div>

<div id="onboard">
  <div class="ob-card">
    <div class="ob-icon">💬</div>
    <h2>Welcome to iMessage Insights</h2>
    <p>To read your conversations, the app needs <b>Full Disk Access</b>. Everything stays
      on your Mac — nothing is uploaded.</p>
    <ol>
      <li>Click <b>Open Full Disk Access settings</b> below.</li>
      <li>Turn on <b>iMessage Insights</b> (or your terminal, if you launched from the command line).</li>
      <li>Quit &amp; reopen, then click <b>Re-check</b>.</li>
    </ol>
    <div class="ob-actions">
      <button class="primary" onclick="openFDA()">Open Full Disk Access settings</button>
      <button onclick="recheck()">Re-check</button>
      <button onclick="previewDemo()">Preview with sample data</button>
    </div>
    <div id="obStatus" class="hint" style="margin-top:10px"></div>
    <details><summary>Technical detail</summary>
      <div id="onboardErr" class="hint" style="margin-top:6px;word-break:break-word"></div></details>
  </div>
</div>

<div id="screen"><span class="lk">🙈</span>Hidden for privacy — click or refocus to resume</div>

<div class="modal" id="settings">
  <div class="sheet">
    <h3>Settings</h3>
    <label>Anthropic API key (kept in memory only, never written to disk)</label>
    <input id="apiKey" type="password" placeholder="sk-ant-...">
    <label>Model</label>
    <select id="model">
      <option value="claude-sonnet-4-6">claude-sonnet-4-6 (fast, cheaper)</option>
      <option value="claude-opus-4-8">claude-opus-4-8 (deepest)</option>
      <option value="claude-haiku-4-5-20251001">claude-haiku-4-5 (cheapest)</option>
    </select>
    <label>Notes vault password — encrypts relationship types &amp; context notes
      (AES-256, memory-only). Set once, then enter it each session to unlock.</label>
    <input id="vaultPw" type="password" placeholder="set or enter to unlock">
    <div id="vaultMsg" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
    <div class="row"><button onclick="closeSettings()">Cancel</button>
      <button class="primary" onclick="saveSettings()">Save</button></div>
  </div>
</div>

<div class="modal" id="ctxModal">
  <div class="sheet">
    <h3>Relationship context</h3>
    <div class="hint">Background the AI can't see in the messages — history, breakup details,
      what you want, the tone. <b>One point per line.</b> Encrypted with your vault password.</div>
    <textarea id="ctxEdit" rows="9" style="margin-top:10px"
      placeholder="e.g.&#10;Dated 2021–2023, ended over distance&#10;Stay friendly but don't reopen the relationship&#10;Avoid bringing up the move"></textarea>
    <div class="row"><button onclick="closeCtx()">Cancel</button>
      <button class="primary" onclick="saveCtx()">Save context</button></div>
  </div>
</div>

<script>
let THREADS=[], current=null, REL_TYPES=[], lastSig="";

let curNotes="", curRange="month";

async function boot(){
  const s = await (await fetch('/api/settings')).json();
  REL_TYPES = s.relationship_types || [];
  document.getElementById('model').value = s.model;
  document.getElementById('relFilter').innerHTML =
    '<option value="">All relationships</option>' +
    REL_TYPES.map(r=>`<option value="${esc(r)}">${relIcon(r)} ${esc(r)}</option>`).join('') +
    '<option value="__none">🔖 Unclassified</option>';
  document.getElementById('screen').onclick = screenOff;
  // Access check first — if we can't read Messages, show onboarding (no scary error).
  const st = await (await fetch('/api/status')).json();
  if(!st.demo && !st.db_ok){ showOnboard(st.db_error); return; }
  updateLock(s.vault);
  await loadThreads();
  // If a vault exists but is locked, prompt to unlock so saved relationships
  // appear and new ones can be saved (otherwise it looks like nothing saved).
  if(s.vault && s.vault.exists && !s.vault.unlocked){
    if(await ensureVault()) await loadThreads();
  }
  setInterval(tick, 8000);
}

function showOnboard(err){
  document.getElementById('onboard').classList.add('on');
  if(err) document.getElementById('onboardErr').textContent = err;
}
async function openFDA(){
  await fetch('/api/open-settings',{method:'POST'});
  document.getElementById('obStatus').textContent =
    'Opened System Settings → enable “iMessage Insights”, then return here and click Re-check.';
}
async function recheck(){
  document.getElementById('obStatus').textContent = 'Checking…';
  const st = await (await fetch('/api/status')).json();
  if(st.db_ok || st.demo){ location.reload(); }
  else { document.getElementById('obStatus').textContent =
    'Still no access. Make sure it’s enabled, then fully quit and reopen the app.'; }
}
async function previewDemo(){ await fetch('/api/demo',{method:'POST'}); location.reload(); }

function updateLock(st){
  const b=document.getElementById('lockBtn');
  if(st && st.unlocked){ b.textContent='🔓'; b.title='Notes vault (unlocked)'; }
  else { b.textContent='🔒'; b.title='Notes vault (locked — click to unlock)'; }
}

async function ensureVault(){
  const pw=prompt('Notes vault password (encrypts your relationship types & context). '+
    'Set it the first time, then enter the same password each session to unlock:');
  if(!pw) return false;
  const st=await (await fetch('/api/vault',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({password:pw})})).json();
  updateLock(st);
  if(!st.ok){ alert('Wrong password — vault stayed locked.'); return false; }
  return true;
}

async function vaultPrompt(){
  if(await ensureVault()){ await loadThreads(); if(current) openThread(current); }
}

function flash(id,txt){ const e=document.getElementById(id); const o=e.textContent;
  e.textContent=txt; setTimeout(()=>e.textContent=o,1200); }

async function loadThreads(){
  const r = await fetch('/api/threads');
  if(!r.ok){ document.getElementById('list').innerHTML =
    '<div class="err">'+await r.text()+'</div>'; return; }
  THREADS = await r.json();
  render();
}

function sig(){ return THREADS.map(t=>t.id+':'+t.last_raw).join('|'); }

async function tick(){
  if(!document.getElementById('liveChk').checked) return;
  const r = await fetch('/api/threads'); if(!r.ok) return;
  const fresh = await r.json();
  const newSig = fresh.map(t=>t.id+':'+t.last_raw).join('|');
  if(newSig !== sig()){ THREADS = fresh; render(); }
  if(current) refreshOpen();
}

function avatarColor(name){
  let h=0; for(const c of (name||'?')) h=(h*31 + c.charCodeAt(0))>>>0;
  return 'hsl('+(h%360)+',60%,55%)';
}
function initials(name){
  if(!name) return '?';
  const words=name.trim().split(/[\s,]+/).filter(Boolean);
  if(words.length>=2 && /[a-zA-Z]/.test(words[0]+words[1]))
    return (words[0][0]+words[1][0]).toUpperCase();
  const letters=(words[0]||name).replace(/[^a-zA-Z0-9]/g,'');
  return (letters.slice(0,2)||(words[0]||name).slice(0,2)).toUpperCase();
}
function label(name){
  return document.getElementById('initChk').checked ? initials(name) : (name||'');
}
function dot(name,sm){
  return `<span class="av${sm?' sm':''}" style="background:${avatarColor(name)}" `+
         `title="${esc(name)}"></span>`;
}
function avatarEl(name,url,sm){
  return url ? `<img class="av${sm?' sm':''}" src="${url}" title="${esc(name)}" loading="lazy">`
             : dot(name,sm);
}

const REL_ICON = {
  "Family":"👪","Close Friend":"💛","Friend":"🙂","Romantic / Partner":"❤️",
  "Ex / Former Partner":"💔","Professional / Colleague":"💼","Client / Business":"🤝",
  "Acquaintance":"👋","Other":"🔖"
};
function relIcon(t){ return REL_ICON[t] || "🔖"; }

function togglePrivacy(){
  document.body.classList.toggle('privacy', document.getElementById('privChk').checked);
}
let autoHide=true;
function screenOn(){ document.getElementById('screen').classList.add('on'); }
function screenOff(){ document.getElementById('screen').classList.remove('on'); }
window.addEventListener('blur', ()=>{ if(autoHide) screenOn(); });
window.addEventListener('focus', screenOff);
document.addEventListener('visibilitychange', ()=>{ if(document.hidden && autoHide) screenOn(); });

function render(){
  const q = document.getElementById('search').value.toLowerCase();
  const rf = document.getElementById('relFilter').value;
  const list = document.getElementById('list'); list.innerHTML='';
  THREADS.filter(t=>{
      if(q && !(t.title.toLowerCase().includes(q) ||
        (t.participants||[]).join(' ').toLowerCase().includes(q))) return false;
      if(rf==='__none') return !t.relationship;
      if(rf) return t.relationship===rf;
      return true;
    })
    .forEach(t=>{
      const d=document.createElement('div');
      d.className='thread'+(current===t.id?' active':'');
      d.onclick=()=>openThread(t.id);
      d.innerHTML=`<div class="t">${avatarEl(t.title, t.img)}
        <span class="nm">${esc(label(t.title))}${t.is_group?' 👥':''}${t.imported?' 📎':''}</span></div>
        <div class="s"><span>${esc(t.last_date)}</span>
        <span>${t.relationship?`<span class="relicon" title="${esc(t.relationship)}">${relIcon(t.relationship)}</span> `:''}
        <span class="badge">${t.msg_count}</span></span></div>`;
      list.appendChild(d);
    });
}

async function openThread(id){
  current=id; render(); lastSig="";
  curRange = String(id).startsWith('imp:') ? 'all' : 'month';   // imported = historical
  ['btnMd','btnZip','btnAI','btnPdf'].forEach(b=>document.getElementById(b).style.display='');
  document.getElementById('icontrols').style.display='block';
  document.getElementById('loadbar').style.display='flex';
  document.getElementById('ibody').innerHTML='<div id="iempty">Add context, then '+
    'use <b>Analyze</b>, <b>Draft reply</b>, or <b>Critique mine</b>.</div>';
  updateRangeSeg();
  await loadThread();
}

async function loadThread(){
  const msgs=document.getElementById('msgs'); msgs.innerHTML='<div class="spin"></div>';
  const t=await (await fetch('/api/thread?id='+current+'&range='+curRange)).json();
  document.getElementById('title').innerHTML=avatarEl(t.title,t.img)+'<span>'+esc(label(t.title))+'</span>';
  const sel=document.getElementById('relSel'); sel.style.display='';
  sel.innerHTML='<option value="">— relationship —</option>'+
    REL_TYPES.map(r=>`<option ${t.relationship.type===r?'selected':''}>${esc(r)}</option>`).join('');
  curNotes=t.relationship.notes||''; renderCtx(curNotes);
  const shown=t.messages.length;
  document.getElementById('loadcount').textContent = t.truncated
    ? `showing last ${shown} of ${t.truncated.toLocaleString()} messages`
    : shown + (curRange==='all' ? ' messages (all time)' : ' messages in range');
  paintMessages(t.messages);
  refreshAiPanel();
}

function setRange(r){ curRange=r; updateRangeSeg(); if(current) loadThread(); }
function updateRangeSeg(){
  document.querySelectorAll('#rangeSeg button').forEach(b=>
    b.classList.toggle('on', b.getAttribute('onclick').indexOf("'"+curRange+"'")>=0));
  updateScope();
}

function msgSig(ms){ return ms.length+':'+(ms.length?ms[ms.length-1].timestamp:''); }

function paintMessages(ms){
  lastSig=msgSig(ms);
  const msgs=document.getElementById('msgs'); msgs.innerHTML='';
  ms.forEach(m=>{
    const d=document.createElement('div');
    d.className='msg '+(m.is_from_me?'me':'them');
    d.innerHTML=`<div class="meta">${avatarEl(m.sender,m.img,true)}${esc(label(m.sender))} · ${esc(m.timestamp)}</div>
      <div class="body">${esc(m.text)}</div>`;
    msgs.appendChild(d);
  });
  if(!ms.length) msgs.innerHTML='<div id="empty">No messages in this range.<br>'+
    'Try a wider range above (Year / All time).</div>';
  msgs.scrollTop=msgs.scrollHeight;
}

async function refreshOpen(){
  const t=await (await fetch('/api/thread?id='+current+'&range='+curRange)).json();
  if(msgSig(t.messages)!==lastSig){
    paintMessages(t.messages);
    document.getElementById('loadcount').textContent = t.truncated
      ? `showing last ${t.messages.length} of ${t.truncated.toLocaleString()} messages`
      : t.messages.length + (curRange==='all' ? ' messages (all time)' : ' messages in range');
  }
}

async function postRel(type, notes){
  const r=await fetch('/api/relationship',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({id:current,type,notes})});
  const d=await r.json().catch(()=>({error:'failed'}));
  if(!r.ok || d.error){
    if(await ensureVault()) return postRel(type, notes);   // unlocked → retry
    return null;
  }
  return d;
}

async function saveRel(){            // relationship type changed in the dropdown
  const type=document.getElementById('relSel').value;
  const d=await postRel(type, curNotes);
  if(!d){ toast('Not saved — unlock the vault (🔒)'); return; }
  const t=THREADS.find(x=>x.id===current); if(t){ t.relationship=type; render(); }
  toast('Relationship saved');
}

function renderCtx(notes){
  const ul=document.getElementById('ctxList');
  const pts=(notes||'').split(/\r?\n/).map(s=>s.replace(/^[-•*]\s*/,'').trim()).filter(Boolean);
  ul.innerHTML = pts.length ? pts.map(p=>`<li>${esc(p)}</li>`).join('')
    : '<li class="muted">No context added yet.</li>';
}
function openCtx(){ document.getElementById('ctxEdit').value=curNotes;
  document.getElementById('ctxModal').classList.add('open'); }
function closeCtx(){ document.getElementById('ctxModal').classList.remove('open'); }
async function saveCtx(){
  const notes=document.getElementById('ctxEdit').value;
  const type=document.getElementById('relSel').value;
  const d=await postRel(type, notes);
  if(!d){ toast('Not saved — unlock the vault (🔒)'); return; }
  curNotes = (d.notes!==undefined ? d.notes : notes);
  renderCtx(curNotes); closeCtx(); toast('Context saved');
}

function toast(msg){
  let t=document.getElementById('toast');
  if(!t){ t=document.createElement('div'); t.id='toast'; document.body.appendChild(t); }
  t.textContent=msg; t.className='show';
  clearTimeout(t._h); t._h=setTimeout(()=>{ t.className=''; }, 1700);
}

let importedFileText=null;
function openImport(){ importedFileText=null;
  document.getElementById('importModal').classList.add('open'); }
function closeImport(){ document.getElementById('importModal').classList.remove('open'); }
async function doImport(){
  const title=document.getElementById('impTitle').value.trim();
  const me=document.getElementById('impMe').value.trim()||'Me';
  const text=importedFileText || document.getElementById('impText').value;
  if(!text.trim()){ alert('Paste or upload the conversation text first.'); return; }
  toast('Importing…');
  const r=await fetch('/api/import',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({title:title||'Imported', me_name:me, text})});
  const d=await r.json().catch(()=>({error:'failed'}));
  if(!r.ok || d.error){
    if(await ensureVault()) return doImport();         // vault locked → unlock & retry
    alert('Import failed: '+(d.error||'')); return;
  }
  closeImport(); toast('Imported '+d.count.toLocaleString()+' messages');
  await loadThreads(); openThread(d.id);
}
document.addEventListener('change', (e)=>{
  if(e.target && e.target.id==='impFile' && e.target.files[0]){
    const f=e.target.files[0];
    const fr=new FileReader();
    fr.onload=()=>{ importedFileText=fr.result;     // keep big files out of the textarea
      document.getElementById('impText').value =
        `✓ Loaded ${f.name} (${(f.size/1048576).toFixed(1)} MB). Click Import.`; };
    fr.readAsText(f);
  }
});

let COPY=[];
function copyBtn(text){ const i=COPY.push(text)-1;
  return `<span class="copy" onclick="copyTxt(this,${i})">copy</span>`; }
function spinner(txt){ return '<div class="spin"></div><div style="text-align:center;'+
  'color:var(--muted);font-size:12px">'+esc(txt)+'</div>'; }

function cacheBar(d, fn){
  if(!d || !d.at) return '';
  const scope = d.range ? ' · scope: '+esc(d.range) : '';
  const note = d.cached ? ' · saved copy' : ' · saved';
  return `<div class="cachebar">💾 Insight from ${esc(d.at)}${scope}${note}`+
         ` · <button class="link" onclick="${fn}(true)">↻ Regenerate (new charge)</button></div>`;
}
function freshBar(){
  return `<div class="cachebar">⚡ Fresh result — replies & critiques always regenerate `+
         `(not saved), so they reflect the latest messages.</div>`;
}
function updateScope(){
  const m={day:'day',week:'week',month:'month',year:'year',all:'all-time'};
  const el=document.getElementById('scopeLbl'); if(el) el.textContent=m[curRange]||curRange;
}

async function confirmCost(kind){
  let e;
  try { e = await (await fetch('/api/estimate?id='+current+'&kind='+kind+'&range='+curRange)).json(); }
  catch(_) { return confirm('Could not estimate cost. Send this '+kind+' request anyway?'); }
  if(e.error==='no_key'){ alert('Add your Anthropic API key in ⚙ Settings first.'); return false; }
  if(e.error){ return confirm('Could not estimate cost. Send this '+kind+' request anyway?'); }
  return confirm(
    `Estimated cost — ${kind} (scope: ${curRange})\n\n`+
    `≈ $${e.cost_usd.toFixed(4)}   (${e.model})\n`+
    `~${e.input_tokens.toLocaleString()} input tokens · up to ${e.output_tokens.toLocaleString()} output\n\n`+
    `Only the messages in your current view are sent. Proceed with this charge?`);
}

let QA=[];
async function refreshAiPanel(qaOnly){
  const c=await (await fetch('/api/ai-cache?id='+current)).json();
  QA = c.qa || [];
  const link=document.getElementById('qaLink');
  if(QA.length){ link.style.display=''; link.textContent='💬 Saved Q&A ('+QA.length+')'; }
  else { link.style.display='none'; }
  if(qaOnly) return;
  if(c.insights){ document.getElementById('ibody').innerHTML =
    cacheBar(c.insights,'analyze')+renderInsights(c.insights); }
}

function qaCard(item){
  return `<div class="icard qa"><h4>❓ ${esc(item.q)}</h4>`+
         `<div class="ans">${esc(item.a)}</div>`+
         `<div class="hint" style="margin-top:6px">${esc(item.at)} · scope: ${esc(item.range||'all')}</div></div>`;
}
function showQA(){
  const ib=document.getElementById('ibody');
  ib.innerHTML = QA.length ? QA.slice().reverse().map(qaCard).join('')
                           : '<div id="iempty">No saved questions yet.</div>';
}
async function ask(){
  if(!current) return;
  const q=document.getElementById('askBox').value.trim();
  if(!q){ return; }
  if(!await confirmCost('ask')) return;
  const ib=document.getElementById('ibody'); ib.innerHTML=spinner('Answering from the messages in view…');
  const r=await fetch('/api/ask',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({id:current, range:curRange, question:q})});
  const d=await r.json();
  if(d.error){ ib.innerHTML='<div class="err">'+esc(d.error)+'</div>'; return; }
  document.getElementById('askBox').value='';
  await refreshAiPanel(true);     // reload saved Q&A list + count
  showQA();
}

async function analyze(force){     // historical insight: cached; this button generates fresh
  if(!current) return;
  if(!await confirmCost('insights')) return;
  const ib=document.getElementById('ibody'); ib.innerHTML=spinner('Reading the conversation…');
  const r=await fetch('/api/insights',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({id:current, range:curRange, force:true})});
  const d=await r.json();
  if(d.error){ ib.innerHTML='<div class="err">'+esc(d.error)+'</div>'; return; }
  ib.innerHTML=cacheBar(d,'analyze')+renderInsights(d);
}

async function assist(mode){
  return (await fetch('/api/assist',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({id:current, mode, range:curRange,
      instruction:document.getElementById('replyInstruction').value})})).json();
}
async function draftReply(){       // always fresh
  if(!current) return;
  if(!await confirmCost('reply')) return;
  const ib=document.getElementById('ibody'); ib.innerHTML=spinner('Drafting a fresh reply…');
  const d=await assist('reply');
  ib.innerHTML = d.error ? '<div class="err">'+esc(d.error)+'</div>' : freshBar()+renderReplies(d);
}
async function critique(){         // always fresh
  if(!current) return;
  if(!await confirmCost('critique')) return;
  const ib=document.getElementById('ibody'); ib.innerHTML=spinner('Reviewing your recent messages…');
  const d=await assist('critique');
  ib.innerHTML = d.error ? '<div class="err">'+esc(d.error)+'</div>' : freshBar()+renderCritique(d);
}

function renderReplies(d){
  COPY=[]; const card=(t,h)=>`<div class="icard"><h4>${t}</h4>${h}</div>`;
  let h=(d.replies||[]).map(rp=>`<div class="reply">${copyBtn(rp.text||'')}
    <div class="strat">${esc(rp.tone||'Option')}</div>
    <div class="ex">${esc(rp.text||'')}</div></div>`).join('');
  let out=card('💬 Suggested replies', h||'—');
  if(d.tips&&d.tips.length) out+=card('Delivery tips','• '+d.tips.map(esc).join('<br>• '));
  return out;
}

function renderCritique(d){
  const card=(t,h)=>`<div class="icard"><h4>${t}</h4>${h}</div>`;
  let h='';
  if(d.overall) h+=card('Overall', esc(d.overall));
  if(d.strengths&&d.strengths.length) h+=card('Strengths','• '+d.strengths.map(esc).join('<br>• '));
  if(d.improvements&&d.improvements.length)
    h+=card('Improvements','• '+d.improvements.map(esc).join('<br>• '));
  if(d.rewrites&&d.rewrites.length) h+=card('Rewrites', d.rewrites.map(rw=>
    `<div class="rewrite"><div class="o">${esc(rw.original||'')}</div>
     <div class="b">→ ${esc(rw.better||'')}</div></div>`).join(''));
  return h||'<div id="iempty">No critique returned.</div>';
}

function renderInsights(d){
  COPY=[];
  const card=(t,h)=>`<div class="icard"><h4>${t}</h4>${h}</div>`;
  let h='';
  if(d.summary) h+=card('Summary', esc(d.summary));
  let meta='';
  if(d.tone) meta+=`<b>Tone:</b> ${esc(d.tone)}<br>`;
  if(d.relationship_health) meta+=`<b>Health:</b> ${esc(d.relationship_health)}<br>`;
  if(d.their_style) meta+=`<b>Their style:</b> ${esc(d.their_style)}`;
  if(meta) h+=card('Read', meta);
  if(d.key_topics&&d.key_topics.length)
    h+=card('Topics', d.key_topics.map(t=>`<span class="chip">${esc(t)}</span>`).join(''));
  if(d.open_threads&&d.open_threads.length)
    h+=card('Open threads','• '+d.open_threads.map(esc).join('<br>• '));
  if(d.suggested_replies&&d.suggested_replies.length){
    let r=d.suggested_replies.map(s=>`<div class="reply">${copyBtn(s.example||'')}
      <div class="strat">${esc(s.strategy||'')}</div>
      <div class="when">${esc(s.when||'')}</div>
      <div class="ex">${esc(s.example||'')}</div></div>`).join('');
    h+=card('Suggested replies', r);
  }
  if(d.watch_outs&&d.watch_outs.length)
    h+=card('Watch-outs','• '+d.watch_outs.map(esc).join('<br>• '));
  return h||'<div id="iempty">No insights returned.</div>';
}

function copyTxt(el,i){ navigator.clipboard.writeText(COPY[i]||''); el.textContent='copied'; }

function exportMd(){ if(current) location.href='/export/md?id='+current; }
function exportZip(){ if(current){ toast('Building ZIP — merging all their chats…');
  location.href='/export/person-zip?id='+current; } }
function exportAI(){ if(current){ toast('Building AI export — full history, chunked…');
  location.href='/export/person-ai?id='+current; } }
function exportPdf(){ if(current) window.open('/export/print?id='+current,'_blank'); }
function exportAll(){ location.href='/export/all'; }

async function encryptArchive(){
  let st=await (await fetch('/api/vault')).json();
  if(!st.unlocked){ if(!await ensureVault()) return; }
  const r=await fetch('/export/encrypted-archive',{method:'POST',
    headers:{'content-type':'application/json'},body:'{}'});
  if(!r.ok){ const e=await r.json().catch(()=>({error:'failed'}));
    alert('Encrypt failed: '+(e.error||'')); return; }
  const blob=await r.blob(); const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download='imessages_all.zip.enc'; a.click();
  alert('All conversations encrypted into one archive (imessages_all.zip.enc).\n\n'+
    'Decrypt later:\n  openssl enc -d -aes-256-cbc -pbkdf2 -in imessages_all.zip.enc -out all.zip\n'+
    '  unzip all.zip');
}

function openSettings(){
  document.getElementById('settings').classList.add('open');
  fetch('/api/settings').then(r=>r.json()).then(s=>{
    document.getElementById('apiKey').placeholder = s.has_key
      ? (s.persisted ? '✓ A key is saved (encrypted) — leave blank to keep it'
                     : 'A key is set for this session — leave blank to keep it')
      : 'sk-ant-api03-...';
  });
}
function closeSettings(){ document.getElementById('settings').classList.remove('open'); }
async function postSettings(apiKey, model){
  return (await fetch('/api/settings',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({api_key:apiKey, model})})).json();
}
async function saveSettings(){
  const apiKey=document.getElementById('apiKey').value;
  const model=document.getElementById('model').value;
  // 1) unlock the vault FIRST (if a password was given) so the key can be saved into it
  const pw=document.getElementById('vaultPw').value;
  if(pw){
    const st=await (await fetch('/api/vault',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify({password:pw})})).json();
    updateLock(st);
    document.getElementById('vaultMsg').textContent = st.ok ?
      'Vault unlocked.' : 'Wrong password — vault stayed locked.';
    if(!st.ok) return;
    document.getElementById('vaultPw').value='';
  }
  // 2) save settings (persists into the vault when it's unlocked)
  let res=await postSettings(apiKey, model);
  // 3) if a key was entered but couldn't persist (vault locked), unlock and retry
  if(apiKey && !res.persisted){
    if(await ensureVault()) res=await postSettings(apiKey, model);
  }
  if(apiKey) toast(res.persisted ? '🔑 API key saved (encrypted in vault)'
                                  : 'API key set for this session only');
  if(pw){ await loadThreads(); if(current) openThread(current); }
  closeSettings();
}

function esc(s){ return (s||'').replace(/[&<>"]/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
boot();
</script></body></html>"""


# ---------------------------------------------------------------------------
# Demo data (used only when --demo is passed). Entirely fictional.
# ---------------------------------------------------------------------------

def _demo_threads():
    def th(i, title, rel, notes, msgs):
        return {"id": i, "title": title, "rel": rel, "notes": notes,
                "messages": [{"timestamp": t, "is_from_me": m, "text": x} for (t, m, x) in msgs]}
    return [
        th("d1", "Alex Rivera", "Ex / Former Partner",
           "Dated 2021-2023, ended over distance. Keep it friendly - no mixed signals.", [
            ("2026-06-04 18:22", False, "Hey - saw the reunion photos, looked fun!"),
            ("2026-06-04 18:40", True,  "It was! Wish you'd been there."),
            ("2026-06-04 18:41", False, "Next time for sure. How have you been?"),
            ("2026-06-05 09:10", True,  "Good - busy with the new role. You?"),
            ("2026-06-05 09:16", False, "Same. We should grab coffee sometime, no pressure :)")]),
        th("d2", "Mom", "Family", "", [
            ("2026-06-05 07:30", False, "Don't forget Dad's birthday Sunday!"),
            ("2026-06-05 07:45", True,  "Already wrapped the gift"),
            ("2026-06-05 07:46", False, "You're the best. Love you")]),
        th("d3", "Jordan (Design)", "Professional / Colleague", "", [
            ("2026-06-05 11:02", False, "Can you review the mockups before standup?"),
            ("2026-06-05 11:20", True,  "On it - comments in by noon.")]),
        th("d4", "Sam Chen", "Close Friend", "", [
            ("2026-06-03 20:10", False, "Trivia night Thursday?"),
            ("2026-06-03 20:12", True,  "Always. Our streak is on the line")]),
        th("d5", "Taylor", "Romantic / Partner", "Anniversary is the 20th.", [
            ("2026-06-05 12:00", False, "Thinking about you"),
            ("2026-06-05 12:05", True,  "Dinner tonight? I'll cook."),
            ("2026-06-05 12:06", False, "Yes please.")]),
    ]


def _demo_list():
    out = []
    for t in _demo_threads():
        m = t["messages"]
        out.append({"id": t["id"], "title": t["title"], "is_group": False, "img": None,
                    "participants": [t["title"]], "last_date": m[-1]["timestamp"],
                    "last_raw": m[-1]["timestamp"], "msg_count": len(m),
                    "relationship": _DEMO_REL.get(t["id"], {}).get("type", t["rel"])})
    out.sort(key=lambda x: x["last_date"], reverse=True)
    return out


def _demo_get(idv):
    for t in _demo_threads():
        if t["id"] == str(idv):
            rel = _DEMO_REL.get(t["id"], {"type": t["rel"], "notes": t["notes"]})
            msgs = [{**mm, "sender": "Me" if mm["is_from_me"] else t["title"], "img": None}
                    for mm in t["messages"]]
            return {"id": t["id"], "title": t["title"], "is_group": False, "imported": False,
                    "img": None, "participants": [t["title"]], "relationship": rel,
                    "messages": msgs}
    return None


DEMO_INSIGHT = {
    "summary": "A warm, lightly nostalgic check-in. Alex is re-initiating friendly contact "
               "after the reunion; you're receptive but measured.",
    "tone": "warm, light, a little nostalgic",
    "relationship_health": "Amicable post-breakup, on friendly footing with a hint of lingering closeness.",
    "their_style": "Casual, initiates easily, low-pressure, uses light emoji.",
    "key_topics": ["reunion", "catching up", "new job", "coffee"],
    "open_threads": ["Coffee suggested but not yet scheduled"],
    "suggested_replies": [
        {"strategy": "Warm but boundaried", "when": "Stay friendly without mixed signals",
         "example": "Coffee sounds nice - maybe next week? Keeping it easy."},
        {"strategy": "Light deflect", "when": "If you'd rather not meet up yet",
         "example": "Things are hectic right now, but glad you're doing well!"},
        {"strategy": "Direct + kind", "when": "To be clear about boundaries",
         "example": "I'd be up for coffee as friends - just want to keep it in that lane."}],
    "watch_outs": ["Avoid over-promising plans", "Mind the nostalgic tone given the relationship type"]}
DEMO_REPLY = {"replies": [
    {"tone": "Warm & brief", "text": "Coffee sounds good - maybe next week? :)"},
    {"tone": "Friendly + clear", "text": "Would be nice to catch up as friends. Let's find a time."},
    {"tone": "Low-key", "text": "Glad you're doing well! Things are busy but let's see."}],
    "tips": ["Keep it short to match the easy tone", "No need to reply instantly"]}
DEMO_CRITIQUE = {
    "overall": "You're warm and concise, which lands well here.",
    "strengths": ["Friendly without overcommitting", "Good emoji balance"],
    "improvements": ["Could be a touch clearer about intent to avoid mixed signals"],
    "rewrites": [{"original": "Same. We should grab coffee sometime, no pressure :)",
                  "better": "Good to hear from you! Coffee as friends sometime would be nice - no rush."}]}


def enable_demo():
    """Switch to demo data (fictional). Safe to call at runtime via /api/demo."""
    global DEMO
    DEMO = True
    if not SETTINGS["api_key"]:
        SETTINGS["api_key"] = "demo-mode"
    if not _DEMO_REL:
        for t in _demo_threads():
            _DEMO_REL[t["id"]] = {"type": t["rel"], "notes": t["notes"]}
        _DEMO_QA["d1"] = [{"q": "Is Alex trying to rekindle things?",
            "a": "The tone is friendly and a little nostalgic, but nothing here is explicitly "
                 "romantic - it reads as a genuine, low-pressure reconnection. The 'no pressure' "
                 "coffee suggestion keeps it open-ended. Given you've classified this as an ex, "
                 "watch for mixed signals, but there's no clear push to rekindle.",
            "range": "month", "at": "2026-06-05 09:25:00"}]


def db_readable():
    """Can we actually read the Messages DB? Returns (ok, error_message)."""
    if DEMO:
        return True, None
    try:
        con = open_db()
        con.execute("SELECT 1 FROM message LIMIT 1").fetchone()
        con.close()
        return True, None
    except Exception as e:
        return False, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--demo", action="store_true",
                    help="serve fictional demo data (no DB / key / vault needed)")
    args = ap.parse_args()
    if args.demo:
        enable_demo()
    elif not os.path.exists(CHAT_DB):
        print(f"chat.db not found at {CHAT_DB} — is Messages set up on this Mac?")
        # don't exit — let the UI show onboarding instead of dying
    url = f"http://127.0.0.1:{args.port}"
    print(f"iMessage Export & Analyze → {url}" + ("   [DEMO MODE]" if DEMO else ""))
    if not DEMO:
        print("If threads don't load, grant Full Disk Access to your terminal (see README).")
    if not args.no_browser:
        try: webbrowser.open(url)
        except Exception: pass
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
