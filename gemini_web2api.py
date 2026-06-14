#!/usr/bin/env python3
"""
gemini-web2api - Gemini Web to OpenAI API proxy.

Converts Google Gemini's web interface into an OpenAI-compatible API server.
Zero authentication required. Works on any platform (Windows/macOS/Linux).

Usage:
    pip install httpx
    python gemini_web2api.py [--port 8081] [--config config.json]

Client configuration (Cherry Studio, ChatBox, etc.):
    Base URL: http://localhost:8081/v1
    API Key: (anything or empty)

How it works:
    Sends requests directly to Gemini's public StreamGenerate endpoint.
    The backend does not verify authentication for basic text generation.
    Model selection via MODE_CATEGORY field [79] in the request payload.
    This is NOT a user-tier spoofing attack - the endpoint simply doesn't
    require auth for anonymous access.
"""
import json
import urllib.request
import urllib.parse
import time
import ssl
import sys
import uuid
import re
import os
import hashlib
import argparse
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

__version__ = "1.2.0"

_START_TIME = time.time()
_TOTAL_REQUESTS = 0

# ─── Configuration ───────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.5-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
}

CONFIG = dict(DEFAULT_CONFIG)

# ─── Models ──────────────────────────────────────────────────────────────────
# Mapping from JS source: MODE_CATEGORY enum (028-6eb337387583.js)
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

MODELS = {
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Fast general-purpose model",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model",
    },
}

# ─── Utilities ───────────────────────────────────────────────────────────────

import threading
import random
import os
import time
import json
import re
import hashlib
import ssl
import urllib.request
import urllib.parse

_token_lock = threading.Lock()
_last_token_refresh = 0

UA_PROFILES = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "chrome"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "chrome120"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "chrome124"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "chrome119"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "chrome"),
]
_SELECTED_UA, _SELECTED_IMPERSONATE = random.choice(UA_PROFILES)

# ─── Persistent SQLite Thread Cache ──────────────────────────────────────────
import sqlite3

class SQLiteThreadCache:
    def __init__(self, db_path="threads.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS threads ("
                    "  thread_id TEXT PRIMARY KEY, "
                    "  conv_id TEXT, "
                    "  session_ctx TEXT"
                    ")"
                )
                conn.commit()
        except Exception as e:
            log(f"SQLite init error: {e}")

    def get(self, thread_id):
        if not thread_id:
            return None
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT conv_id, session_ctx FROM threads WHERE thread_id = ?", (thread_id,))
                row = cursor.fetchone()
                if row:
                    conv_id, session_ctx_str = row
                    try:
                        session_ctx = json.loads(session_ctx_str)
                    except Exception:
                        session_ctx = session_ctx_str
                    return conv_id, session_ctx
        except Exception as e:
            log(f"SQLite get error: {e}")
        return None

    def set(self, thread_id, conv_id, session_ctx):
        if not thread_id:
            return
        try:
            session_ctx_str = json.dumps(session_ctx) if isinstance(session_ctx, (dict, list)) else str(session_ctx)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO threads (thread_id, conv_id, session_ctx) VALUES (?, ?, ?)",
                    (thread_id, conv_id, session_ctx_str)
                )
                conn.commit()
        except Exception as e:
            log(f"SQLite set error: {e}")

    def __setitem__(self, thread_id, value):
        conv_id, session_ctx = value
        self.set(thread_id, conv_id, session_ctx)

    def __contains__(self, thread_id):
        return self.get(thread_id) is not None

THREAD_CACHE = SQLiteThreadCache("threads.db")

# ─── Persistent SQLite Local Memory Cache ─────────────────────────────────────
class SQLiteMemory:
    def __init__(self, db_path="memory.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS memory ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  content TEXT,"
                    "  tags TEXT,"
                    "  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                )
                conn.commit()
        except Exception as e:
            log(f"SQLite memory init error: {e}")

    def store(self, content, tags=""):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("INSERT INTO memory (content, tags) VALUES (?, ?)", (content, tags))
                conn.commit()
            return True
        except Exception as e:
            log(f"SQLite memory store error: {e}")
            return False

    def search(self, query, limit=5):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, content, tags, timestamp FROM memory")
                rows = cursor.fetchall()
            
            q_words = set(query.lower().split())
            results = []
            for rid, content, tags, ts in rows:
                c_words = content.lower().split()
                overlap = len(q_words.intersection(c_words))
                if overlap > 0:
                    results.append({
                        "id": rid, "content": content, "tags": tags, "timestamp": ts, "score": overlap
                    })
            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:limit]
        except Exception as e:
            log(f"SQLite memory search error: {e}")
            return []

GLOBAL_MEMORY = SQLiteMemory("memory.db")

# ─── Rolling API Logs ────────────────────────────────────────────────────────
MAX_LOG_ENTRIES = 100
LOG_ENTRIES = []
_log_entries_lock = threading.Lock()

def add_api_log(direction, endpoint, status_code, request_body, response_body):
    with _log_entries_lock:
        LOG_ENTRIES.append({
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
            "direction": direction,
            "endpoint": endpoint,
            "status_code": status_code,
            "request": request_body[:5000] if isinstance(request_body, str) else str(request_body)[:5000],
            "response": response_body[:5000] if isinstance(response_body, str) else str(response_body)[:5000]
        })
        if len(LOG_ENTRIES) > MAX_LOG_ENTRIES:
            LOG_ENTRIES.pop(0)

# ─── Multi-User XSRF Cache ──────────────────────────────────────────────────
_XSRF_TOKEN_CACHE = {}
_xsrf_cache_lock = threading.Lock()

def get_xsrf_and_bl(cookie_str: str) -> tuple:
    if not cookie_str:
        return CONFIG.get("xsrf_token"), CONFIG.get("gemini_bl")
    key = hashlib.md5(cookie_str.encode()).hexdigest()
    with _xsrf_cache_lock:
        cached = _XSRF_TOKEN_CACHE.get(key)
        if cached:
            xsrf, bl, ts = cached
            if time.time() - ts < 600:
                return xsrf, bl
    return None, None

def set_xsrf_and_bl(cookie_str: str, xsrf: str, bl: str):
    if not cookie_str:
        CONFIG["xsrf_token"] = xsrf
        CONFIG["gemini_bl"] = bl
        return
    key = hashlib.md5(cookie_str.encode()).hexdigest()
    with _xsrf_cache_lock:
        _XSRF_TOKEN_CACHE[key] = (xsrf, bl, time.time())

# ─── Native Cookie Extractors ──────────────────────────────────────────────
def get_mac_key(service_name):
    import subprocess
    try:
        res = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service_name],
            capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0:
            return res.stdout.strip().encode('utf-8')
    except Exception:
        pass
    return None

def decrypt_mac_cookie_openssl(encrypted_value, key):
    if not encrypted_value or not encrypted_value.startswith(b"v10") or not key:
        return None
    import subprocess
    hex_key = key.hex()
    hex_iv = (b' ' * 16).hex()
    try:
        proc = subprocess.Popen(
            ["openssl", "enc", "-d", "-aes-128-cbc", "-K", hex_key, "-iv", hex_iv],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        out, err = proc.communicate(input=encrypted_value[3:])
        if proc.returncode == 0:
            return out.decode('utf-8', errors='ignore')
    except Exception:
        pass
    return None

def decrypt_dpapi(encrypted_bytes):
    try:
        import ctypes
        from ctypes import wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        crypt32 = ctypes.windll.crypt32
        in_blob = DATA_BLOB(len(encrypted_bytes), ctypes.create_string_buffer(encrypted_bytes))
        out_blob = DATA_BLOB()
        if crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0x01, ctypes.byref(out_blob)):
            res = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
            return res
    except Exception:
        pass
    return None

def auto_extract_cookies_native():
    cookie_file = CONFIG.get("cookie_file") or "./cookie.txt"
    if os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 10:
        return True
        
    log("Attempting native cookie extraction...")
    
    # macOS Chrome/Brave/Edge SQLite extraction
    if sys.platform == "darwin":
        import sqlite3
        paths = [
            ("Chrome", "~/Library/Application Support/Google/Chrome/Default/Network/Cookies", "Chrome Safe Storage"),
            ("Chrome Profile 1", "~/Library/Application Support/Google/Chrome/Profile 1/Network/Cookies", "Chrome Safe Storage"),
            ("Chrome Profile 2", "~/Library/Application Support/Google/Chrome/Profile 2/Network/Cookies", "Chrome Safe Storage"),
            ("Brave", "~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Network/Cookies", "Brave Safe Storage"),
            ("Edge", "~/Library/Application Support/Microsoft Edge/Default/Network/Cookies", "Microsoft Edge Safe Storage"),
        ]
        for name, rel_path, svc in paths:
            full_path = os.path.expanduser(rel_path)
            if not os.path.exists(full_path):
                continue
            pw = get_mac_key(svc)
            if not pw:
                continue
            key = hashlib.pbkdf2_hmac('sha1', pw, b'salt', 1003, 16)
            try:
                # Copy DB to read safely
                temp_db = f"/tmp/cookies_{name}"
                import shutil
                shutil.copy(full_path, temp_db)
                with sqlite3.connect(temp_db) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT name, value, encrypted_value FROM cookies WHERE host_key LIKE '%google.com'")
                    cookies = {}
                    sapisid = ""
                    for c_name, val, enc_val in cursor.fetchall():
                        dec = val
                        if enc_val and enc_val.startswith(b"v10"):
                            dec = decrypt_mac_cookie_openssl(enc_val, key) or val
                        cookies[c_name] = dec
                        if c_name == "SAPISID":
                            sapisid = dec
                    if "__Secure-1PSID" in cookies:
                        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                        with open(cookie_file, "w") as f:
                            json.dump({"cookie": cookie_str, "sapisid": sapisid}, f, indent=2)
                        log(f"Successfully extracted native cookies from {name}!")
                        try: os.remove(temp_db)
                        except: pass
                        return True
                try: os.remove(temp_db)
                except: pass
            except Exception as e:
                log(f"Native Mac extraction error for {name}: {e}")
                
    elif sys.platform == "win32":
        # Windows DPAPI SQLite extraction
        paths = [
            ("Chrome", "~/AppData/Local/Google/Chrome/User Data/Default/Network/Cookies", "~/AppData/Local/Google/Chrome/User Data/Local State"),
            ("Brave", "~/AppData/Local/BraveSoftware/Brave-Browser/User Data/Default/Network/Cookies", "~/AppData/Local/BraveSoftware/Brave-Browser/User Data/Local State"),
            ("Edge", "~/AppData/Local/Microsoft/Edge/User Data/Default/Network/Cookies", "~/AppData/Local/Microsoft/Edge/User Data/Local State"),
        ]
        for name, rel_path, rel_state in paths:
            full_path = os.path.expanduser(rel_path)
            full_state = os.path.expanduser(rel_state)
            if not os.path.exists(full_path) or not os.path.exists(full_state):
                continue
            try:
                with open(full_state, "r", encoding="utf-8") as f:
                    local_state = json.load(f)
                enc_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
                # Decrypt master key (strip DPAPI prefix 'DPAPI')
                master_key = decrypt_dpapi(enc_key[5:])
                if not master_key:
                    continue
                # Copy DB
                temp_db = os.path.join(os.environ.get("TEMP", "."), f"cookies_{name}")
                import shutil
                shutil.copy(full_path, temp_db)
                # SQLite
                with sqlite3.connect(temp_db) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT name, value, encrypted_value FROM cookies WHERE host_key LIKE '%google.com'")
                    cookies = {}
                    sapisid = ""
                    for c_name, val, enc_val in cursor.fetchall():
                        dec = val
                        if enc_val and enc_val.startswith(b"v10"):
                            # Decrypt AES-GCM
                            try:
                                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                                aesgcm = AESGCM(master_key)
                                iv = enc_val[3:15]
                                payload = enc_val[15:]
                                dec = aesgcm.decrypt(iv, payload, None).decode('utf-8')
                            except Exception:
                                pass
                        cookies[c_name] = dec
                        if c_name == "SAPISID":
                            sapisid = dec
                    if "__Secure-1PSID" in cookies:
                        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                        with open(cookie_file, "w") as f:
                            json.dump({"cookie": cookie_str, "sapisid": sapisid}, f, indent=2)
                        log(f"Successfully extracted native cookies from {name}!")
                        try: os.remove(temp_db)
                        except: pass
                        return True
                try: os.remove(temp_db)
                except: pass
            except Exception as e:
                log(f"Native Windows extraction error for {name}: {e}")
    return False

COOKIE_POOL = []
_cookie_index = 0

def load_cookie_pool():
    global COOKIE_POOL
    cookie_path = CONFIG.get("cookie_file")
    if not cookie_path:
        return
    COOKIE_POOL = []
    if os.path.isdir(cookie_path):
        for f in os.listdir(cookie_path):
            if f.endswith(".txt") or f.endswith(".json"):
                COOKIE_POOL.append(os.path.join(cookie_path, f))
        log(f"Loaded {len(COOKIE_POOL)} cookies from pool directory.")
    else:
        if os.path.exists(cookie_path):
            COOKIE_POOL.append(cookie_path)

def get_next_cookie_file() -> str:
    global _cookie_index
    if not COOKIE_POOL:
        return CONFIG.get("cookie_file")
    f = COOKIE_POOL[_cookie_index % len(COOKIE_POOL)]
    _cookie_index += 1
    return f

_proxy_index = 0

def get_next_proxy() -> str:
    global _proxy_index
    proxy_val = CONFIG.get("proxy")
    if not proxy_val:
        return None
    if isinstance(proxy_val, list):
        if not proxy_val:
            return None
        p = proxy_val[_proxy_index % len(proxy_val)]
        _proxy_index += 1
        return p
    return proxy_val

def refresh_cookies_via_browser():
    """Attempt interactive login/refresh via Playwright or Selenium."""
    cookie_file = CONFIG.get("cookie_file") or "./cookie.txt"
    if os.path.isdir(cookie_file):
        cookie_file = os.path.join(cookie_file, "cookie_refreshed.txt")
        
    # Playwright attempt
    try:
        from playwright.sync_api import sync_playwright
        log("Launching Playwright chromium for interactive login...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://gemini.google.com/app")
            log("Please log in or ensure your session is loaded in the browser window.")
            for _ in range(120):
                if "/app" in page.url and page.query_selector("a[href*='accounts.google.com/SignOut']"):
                    break
                time.sleep(1)
            cookies = context.cookies()
            sapisid = next((c['value'] for c in cookies if c['name'] == 'SAPISID'), None)
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            if sapisid:
                with open(cookie_file, "w") as f:
                    json.dump({"cookie": cookie_str, "sapisid": sapisid}, f, indent=2)
                log(f"[✓] Cookies successfully refreshed via Playwright and saved to {cookie_file}")
                browser.close()
                load_cookie_pool()
                return True
            browser.close()
    except Exception as e:
        log(f"Playwright auto-login not available or failed: {e}")

    # Selenium fallback
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        log("Launching Selenium Chrome for interactive login...")
        opts = Options()
        opts.headless = False
        driver = webdriver.Chrome(options=opts)
        driver.get("https://gemini.google.com/app")
        log("Please log in or ensure your session is loaded in the browser window.")
        for _ in range(120):
            try:
                if "/app" in driver.current_url:
                    cookies = driver.get_cookies()
                    sapisid = next((c['value'] for c in cookies if c['name'] == 'SAPISID'), None)
                    if sapisid:
                        break
            except Exception:
                pass
            time.sleep(1)
        cookies = driver.get_cookies()
        sapisid = next((c['value'] for c in cookies if c['name'] == 'SAPISID'), None)
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        driver.quit()
        if sapisid:
            with open(cookie_file, "w") as f:
                json.dump({"cookie": cookie_str, "sapisid": sapisid}, f, indent=2)
            log(f"[✓] Cookies successfully refreshed via Selenium and saved to {cookie_file}")
            load_cookie_pool()
            return True
    except Exception as e:
        log(f"Selenium auto-login not available or failed: {e}")
    return False

def extract_session_ids_from_line(line: str) -> tuple:
    if '"wrb.fr"' not in line or len(line) < 200:
        return None
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str:
            return None
        inner = json.loads(inner_str)
        if isinstance(inner, list) and len(inner) > 1 and isinstance(inner[1], list):
            conv_id = inner[1][0]
            resp_id = inner[1][1]
            if isinstance(conv_id, str) and conv_id.startswith("c_") and isinstance(resp_id, str) and resp_id.startswith("r_"):
                # If inner[2] is a dict and has key "18", we can use it directly
                if len(inner) > 2 and isinstance(inner[2], dict) and "18" in inner[2]:
                    return conv_id, inner[2]
                
                # Otherwise, let's look for choice_id in inner[4][0][0]
                choice_id = None
                if len(inner) > 4 and isinstance(inner[4], list) and len(inner[4]) > 0:
                    first_cand = inner[4][0]
                    if isinstance(first_cand, list) and len(first_cand) > 0 and isinstance(first_cand[0], str):
                        choice_id = first_cand[0]
                
                if choice_id:
                    session_dict = {"18": resp_id, "21": [choice_id]}
                    return conv_id, session_dict
                
                return conv_id, resp_id
    except Exception:
        pass
    return None

def log(msg: str):
    if CONFIG["log_requests"]:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def load_cookie(cookie_override: str = None) -> tuple:
    """Load cookie from file or override. Returns (cookie_str, sapisid)."""
    if cookie_override:
        pairs = dict(p.split("=", 1) for p in cookie_override.split("; ") if "=" in p)
        sapisid = pairs.get("SAPISID", "")
        return cookie_override, sapisid if sapisid else None
        
    cookie_file = get_next_cookie_file()
    if not cookie_file:
        return "", None
    if not os.path.exists(cookie_file):
        return "", None
    try:
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return "", None


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def account_prefix(auth_user_override = None) -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = auth_user_override if auth_user_override is not None else CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def refresh_xsrf_token(force=False, cookie_override: str = None, auth_user_override: str = None):
    global _last_token_refresh
    cookie_str, sapisid = load_cookie(cookie_override)
    if not cookie_str:
        return
        
    xsrf, bl = get_xsrf_and_bl(cookie_str)
    if not force and xsrf:
        return
        
    with _token_lock:
        xsrf, bl = get_xsrf_and_bl(cookie_str)
        if not force and xsrf:
            return
            
        prefix = account_prefix(auth_user_override)
        url = f"https://gemini.google.com{prefix}/app"
        
        headers = {
            "User-Agent": _SELECTED_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cookie": cookie_str
        }
        auth_user = auth_user_override if auth_user_override is not None else CONFIG.get("auth_user")
        if auth_user is not None:
            headers["X-Goog-AuthUser"] = str(auth_user)
            
        log(f"Refreshing XSRF token from {url}...")
        try:
            proxy = CONFIG.get("proxy")
            proxies = {"http": proxy, "https": proxy} if proxy else None
            
            if HAS_CURL_CFFI:
                resp = curl_requests.get(url, headers=headers, impersonate=_SELECTED_IMPERSONATE, timeout=15, proxies=proxies)
                html = resp.text
            else:
                req = urllib.request.Request(url, headers=headers)
                ctx = ssl.create_default_context()
                if proxy:
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                        urllib.request.HTTPSHandler(context=ctx)
                    )
                    resp = opener.open(req, timeout=15)
                else:
                    resp = urllib.request.urlopen(req, context=ctx, timeout=15)
                html = resp.read().decode("utf-8", errors="replace")
                
            match = re.search(r'window\.WIZ_global_data\s*=\s*(\{.*?\});', html, re.DOTALL)
            if not match:
                match = re.search(r'window\.WIZ_global_data\s*=\s*(\{.*?\})', html, re.DOTALL)
                
            if match:
                data = json.loads(match.group(1))
                new_xsrf = data.get("thykhd")
                new_bl = data.get("cfb2h")
                if new_xsrf:
                    set_xsrf_and_bl(cookie_str, new_xsrf, new_bl or "boq_assistant-bard-web-server_20260525.09_p0")
                    log(f"Successfully refreshed XSRF token: {new_xsrf[:20]}..., BL: {new_bl}")
            else:
                log("Failed to find window.WIZ_global_data in HTML to refresh XSRF token.")
        except Exception as e:
            log(f"Error refreshing XSRF token: {e}")


def auto_extract_cookies():
    if auto_extract_cookies_native():
        return
        
    cookie_file = CONFIG.get("cookie_file") or "./cookie.txt"
    if os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 10:
        return
        
    log("Cookie file not found or empty. Attempting auto-extraction from local browsers...")
    try:
        import browser_cookie3
    except ImportError:
        log("browser-cookie3 package not installed. Auto-cookie extraction skipped.")
        return
        
    browsers = [
        ("Chrome", browser_cookie3.chrome),
        ("Brave", browser_cookie3.brave),
        ("Firefox", browser_cookie3.firefox),
        ("Safari", browser_cookie3.safari),
        ("Edge", browser_cookie3.edge)
    ]
    
    found_cookies = None
    found_sapisid = None
    
    for name, func in browsers:
        try:
            log(f"Checking cookies in {name}...")
            cj = func(domain_name="google.com")
            cookies = {}
            sapisid = None
            for cookie in cj:
                cookies[cookie.name] = cookie.value
                if cookie.name == "SAPISID":
                    sapisid = cookie.value
            
            if "__Secure-1PSID" in cookies:
                found_cookies = cookies
                found_sapisid = sapisid
                log(f"Successfully extracted cookies from {name}!")
                break
        except Exception:
            pass
            
    if found_cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in found_cookies.items())
        cookie_data = {
            "cookie": cookie_str,
            "sapisid": found_sapisid
        }
        try:
            with open(cookie_file, "w") as f:
                json.dump(cookie_data, f, indent=2)
            CONFIG["cookie_file"] = cookie_file
            log(f"Saved extracted cookies to: {cookie_file}")
        except Exception as e:
            log(f"Failed to write extracted cookies to {cookie_file}: {e}")


def discover_active_account():
    cookie_str, sapisid = load_cookie()
    if not cookie_str:
        return
        
    candidates = [
        (None, "https://gemini.google.com/app"),
        (0, "https://gemini.google.com/u/0/app"),
        (1, "https://gemini.google.com/u/1/app"),
        (2, "https://gemini.google.com/u/2/app"),
        (3, "https://gemini.google.com/u/3/app"),
    ]
    
    logged_in_user = None
    pro_user = None
    
    log("Starting multi-account auto-discovery...")
    
    for user_id, url in candidates:
        headers = {
            "User-Agent": _SELECTED_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cookie": cookie_str
        }
        if user_id is not None:
            headers["X-Goog-AuthUser"] = str(user_id)
            
        try:
            proxy = CONFIG.get("proxy")
            proxies = {"http": proxy, "https": proxy} if proxy else None
            
            if HAS_CURL_CFFI:
                resp = curl_requests.get(url, headers=headers, impersonate=_SELECTED_IMPERSONATE, timeout=10, proxies=proxies)
                html = resp.text
                final_url = resp.url
            else:
                req = urllib.request.Request(url, headers=headers)
                ctx = ssl.create_default_context()
                if proxy:
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                        urllib.request.HTTPSHandler(context=ctx)
                    )
                    resp = opener.open(req, timeout=10)
                else:
                    resp = urllib.request.urlopen(req, context=ctx, timeout=10)
                html = resp.read().decode("utf-8", errors="replace")
                final_url = resp.url
                
            is_login = "Sign in" in html or "login" in final_url or "accounts.google.com" in final_url
            
            if not is_login:
                if logged_in_user is None:
                    logged_in_user = user_id
                
                is_pro = "Pro" in html or "Advanced" in html
                log(f"  Account /u/{user_id if user_id is not None else 'default'} is ACTIVE (Pro/Advanced: {is_pro})")
                
                if is_pro:
                    pro_user = user_id
                    break
        except Exception:
            pass
            
    selected_user = pro_user if pro_user is not None else logged_in_user
    if selected_user is not None:
        CONFIG["auth_user"] = selected_user
        log(f"Auto-selected active account: /u/{selected_user}")
    else:
        log("Auto-discovery could not find any active logged-in session. Defaulting to config value.")


def fetch_image_bytes(url: str) -> bytes:
    try:
        proxy = CONFIG.get("proxy")
        proxies = {"http": proxy, "https": proxy} if proxy else None
        if HAS_CURL_CFFI:
            resp = curl_requests.get(url, headers={"User-Agent": _SELECTED_UA}, impersonate=_SELECTED_IMPERSONATE, timeout=30, proxies=proxies)
            return resp.content
        else:
            req = urllib.request.Request(url, headers={"User-Agent": _SELECTED_UA})
            ctx = ssl.create_default_context()
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=30)
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=30)
            return resp.read()
    except Exception as e:
        log(f"Image fetch failed: {e}")
        return b""


def upload_image(image_bytes: bytes, filename: str = "image.png", mime_type: str = "image/png", cookie_override: str = None, auth_user_override: str = None) -> str:
    cookie_str, sapisid = load_cookie(cookie_override)
    prefix = account_prefix(auth_user_override)
    
    app_url = f"https://gemini.google.com{prefix}/app"
    headers = {
        "User-Agent": _SELECTED_UA,
        "Cookie": cookie_str
    }
    auth_user = auth_user_override if auth_user_override is not None else CONFIG.get("auth_user")
    if auth_user is not None:
        headers["X-Goog-AuthUser"] = str(auth_user)
        
    try:
        proxy = CONFIG.get("proxy")
        proxies = {"http": proxy, "https": proxy} if proxy else None
        
        if HAS_CURL_CFFI:
            resp = curl_requests.get(app_url, headers=headers, impersonate=_SELECTED_IMPERSONATE, timeout=30, proxies=proxies)
            html = resp.text
        else:
            req = urllib.request.Request(app_url, headers=headers)
            ctx = ssl.create_default_context()
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=30)
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=30)
            html = resp.read().decode("utf-8", errors="replace")
            
        tokens = {}
        for key, pattern in [
            ("push_id", r'"qKIAYe":"([^"]+)"'),
            ("pctx", r'"Ylro7b":"([^"]+)"'),
        ]:
            m = re.search(pattern, html)
            if m:
                tokens[key] = m.group(1)
    except Exception as e:
        log(f"Page token fetch failed for upload: {e}")
        tokens = {}
        
    push_id = tokens.get("push_id") or "feeds/mcudyrk2a4khkz"
    pctx = tokens.get("pctx") or "CgcSBWjK7pYx"
    
    # Step 1: Initiate resumable upload
    start_headers = {
        "Push-ID": push_id,
        "X-Tenant-Id": "bard-storage",
        "X-Client-Pctx": pctx,
        "X-Goog-Upload-Header-Content-Length": str(len(image_bytes)),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "User-Agent": _SELECTED_UA,
    }
    if cookie_str:
        start_headers["Cookie"] = cookie_str
    if sapisid:
        start_headers["Authorization"] = make_sapisidhash(sapisid)
        
    start_url = "https://content-push.googleapis.com/upload/"
    
    if HAS_CURL_CFFI:
        resp = curl_requests.post(start_url, data=b"", headers=start_headers, impersonate=_SELECTED_IMPERSONATE, timeout=30, proxies=proxies)
        upload_url = resp.headers.get("X-Goog-Upload-URL") or resp.headers.get("x-goog-upload-url")
    else:
        req = urllib.request.Request(start_url, data=b"", headers=start_headers, method="POST")
        ctx = ssl.create_default_context()
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=ctx)
            )
            resp = opener.open(req, timeout=30)
        else:
            resp = urllib.request.urlopen(req, context=ctx, timeout=30)
        upload_url = resp.headers.get("X-Goog-Upload-URL") or resp.headers.get("x-goog-upload-url")
        
    if not upload_url:
        raise RuntimeError("No upload URL in response headers")
        
    # Step 2: Upload file data + finalize
    upload_headers = {
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
        "Content-Type": "application/octet-stream",
        "User-Agent": _SELECTED_UA,
    }
    
    if HAS_CURL_CFFI:
        resp2 = curl_requests.post(upload_url, data=image_bytes, headers=upload_headers, impersonate=_SELECTED_IMPERSONATE, timeout=60, proxies=proxies)
        file_ref = resp2.text.strip()
    else:
        req2 = urllib.request.Request(upload_url, data=image_bytes, headers=upload_headers, method="POST")
        ctx = ssl.create_default_context()
        if proxy:
            resp2 = opener.open(req2, timeout=60)
        else:
            resp2 = urllib.request.urlopen(req2, context=ctx, timeout=60)
        file_ref = resp2.read().decode().strip()
        
    if not file_ref or not file_ref.startswith("/"):
        raise RuntimeError(f"Invalid file reference: {file_ref[:100]}")
        
    log(f"Image uploaded successfully: {file_ref}")
    return file_ref


def upload_images_helper(images: list, cookie_override: str = None, auth_user_override: str = None) -> list:
    if not images:
        return None
    file_refs = []
    for item in images:
        try:
            data, mime = item
            if isinstance(data, str):
                data = fetch_image_bytes(data)
                mime = mime or "image/png"
            if data:
                ref = upload_image(data, "image.png", mime or "image/png", cookie_override=cookie_override, auth_user_override=auth_user_override)
                file_refs.append(ref)
        except Exception as e:
            log(f"Image upload failed: {e}")
    return file_refs if file_refs else None


# ─── Gemini Protocol ─────────────────────────────────────────────────────────

def gemini_stream_generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None, thread_id: str = None, cookie_override: str = None, auth_user_override: str = None) -> str:
    """Send prompt to Gemini StreamGenerate with retry."""
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    
    # Thread persistence
    conv_id, session_ctx = None, None
    if thread_id:
        cached = THREAD_CACHE.get(thread_id)
        if cached:
            conv_id, session_ctx = cached
        
    if conv_id and session_ctx:
        if isinstance(session_ctx, dict):
            resp_id = session_ctx.get("18")
            choice_list = session_ctx.get("21")
            choice_id = choice_list[0] if choice_list and isinstance(choice_list, list) else ""
            inner[2] = [conv_id, resp_id, choice_id, None, None, []]
        else:
            inner[2] = [conv_id, session_ctx, "", None, None, []]
    else:
        inner[2] = ["", "", "", None, None, None, None, None, None, ""]
        
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            cookie_str, sapisid = load_cookie(cookie_override)
            xsrf, bl = get_xsrf_and_bl(cookie_str)
            if not xsrf:
                refresh_xsrf_token(cookie_override=cookie_override, auth_user_override=auth_user_override)
                xsrf, bl = get_xsrf_and_bl(cookie_str)

            outer = [None, json.dumps(inner)]
            params = {"f.req": json.dumps(outer)}
            if xsrf:
                params["at"] = xsrf

            reqid = int(time.time()) % 1000000
            prefix = account_prefix(auth_user_override)
            url = (
                f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
                "assistant.lamda.BardFrontendService/StreamGenerate"
                f"?bl={bl}&hl=en&_reqid={reqid}&rt=c"
            )
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://gemini.google.com",
                "Referer": f"https://gemini.google.com{prefix}/app",
                "X-Same-Domain": "1",
                "User-Agent": _SELECTED_UA,
                "Accept": "*/*",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            auth_user = auth_user_override if auth_user_override is not None else CONFIG.get("auth_user")
            if auth_user is not None:
                headers["X-Goog-AuthUser"] = str(auth_user)

            if cookie_str:
                headers["Cookie"] = cookie_str
            if sapisid:
                headers["Authorization"] = make_sapisidhash(sapisid)

            # Proxy pooling
            proxy = get_next_proxy()
            proxies = {"http": proxy, "https": proxy} if proxy else None

            if HAS_CURL_CFFI:
                resp = curl_requests.post(url, data=params, headers=headers, impersonate=_SELECTED_IMPERSONATE, timeout=CONFIG["request_timeout_sec"], proxies=proxies)
                raw_response = resp.text
            else:
                body = urllib.parse.urlencode(params).encode()
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                ctx = ssl.create_default_context()
                if proxy:
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                        urllib.request.HTTPSHandler(context=ctx)
                    )
                    resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
                else:
                    resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
                raw_response = resp.read().decode("utf-8", errors="replace")

            if "BardErrorInfo" in raw_response:
                m = re.search(r'BardErrorInfo\s*\[(\d+)\]', raw_response)
                if m:
                    log(f"Gemini returned BardErrorInfo [{m.group(1)}]. Retrying with fresh token...")
                    refresh_xsrf_token(force=True, cookie_override=cookie_override, auth_user_override=auth_user_override)
                    raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{m.group(1)}]")

            # Parse and cache session IDs
            if thread_id:
                for line in raw_response.split("\n"):
                    s_ids = extract_session_ids_from_line(line)
                    if s_ids:
                        THREAD_CACHE[thread_id] = s_ids
                        log(f"Cached thread session for {thread_id}: {s_ids}")
                        break

            return raw_response
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def gemini_stream_generate_iter(prompt: str, model_id: int, think_mode: int, file_refs: list = None, thread_id: str = None, cookie_override: str = None, auth_user_override: str = None):
    """Send prompt and yield incremental text deltas using streaming."""
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    
    # Thread persistence
    conv_id, session_ctx = None, None
    if thread_id:
        cached = THREAD_CACHE.get(thread_id)
        if cached:
            conv_id, session_ctx = cached
        
    if conv_id and session_ctx:
        if isinstance(session_ctx, dict):
            resp_id = session_ctx.get("18")
            choice_list = session_ctx.get("21")
            choice_id = choice_list[0] if choice_list and isinstance(choice_list, list) else ""
            inner[2] = [conv_id, resp_id, choice_id, None, None, []]
        else:
            inner[2] = [conv_id, session_ctx, "", None, None, []]
    else:
        inner[2] = ["", "", "", None, None, None, None, None, None, ""]
        
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    if not HAS_CURL_CFFI and not HAS_HTTPX:
        raw = gemini_stream_generate(prompt, model_id, think_mode, file_refs, thread_id=thread_id, cookie_override=cookie_override, auth_user_override=auth_user_override)
        text = extract_response_text(raw)
        if text:
            yield text
        return

    cookie_str, sapisid = load_cookie(cookie_override)
    xsrf, bl = get_xsrf_and_bl(cookie_str)
    if not xsrf:
        refresh_xsrf_token(cookie_override=cookie_override, auth_user_override=auth_user_override)
        xsrf, bl = get_xsrf_and_bl(cookie_str)

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if xsrf:
        params["at"] = xsrf

    reqid = int(time.time()) % 1000000
    prefix = account_prefix(auth_user_override)
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={bl}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": _SELECTED_UA,
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    auth_user = auth_user_override if auth_user_override is not None else CONFIG.get("auth_user")
    if auth_user is not None:
        headers["X-Goog-AuthUser"] = str(auth_user)
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    # Proxy pooling
    proxy = get_next_proxy()
    proxies = {"http": proxy, "https": proxy} if proxy else None

    prev_text = ""
    if HAS_CURL_CFFI:
        try:
            resp = curl_requests.post(url, data=params, headers=headers, impersonate=_SELECTED_IMPERSONATE, stream=True, timeout=CONFIG["request_timeout_sec"], proxies=proxies)
            buf = ""
            for chunk in resp.iter_content():
                buf += chunk.decode("utf-8", errors="ignore")
                if "BardErrorInfo" in buf:
                    m = re.search(r'BardErrorInfo\s*\[(\d+)\]', buf)
                    if m:
                        log(f"Stream error: BardErrorInfo [{m.group(1)}]. Refreshing token...")
                        refresh_xsrf_token(force=True, cookie_override=cookie_override, auth_user_override=auth_user_override)
                        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{m.group(1)}]")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if '"wrb.fr"' not in line or len(line) < 200:
                        continue
                        
                    # Parse and cache session IDs
                    if thread_id:
                        s_ids = extract_session_ids_from_line(line)
                        if s_ids:
                            THREAD_CACHE[thread_id] = s_ids
                            log(f"Cached stream session for {thread_id}: {s_ids}")
                            
                    try:
                        arr = json.loads(line)
                        inner_str = arr[0][2]
                        if not inner_str or len(inner_str) < 50:
                            continue
                        inner2 = json.loads(inner_str)
                        if isinstance(inner2, list) and len(inner2) > 4 and inner2[4]:
                            for part in inner2[4]:
                                if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                                    for t in part[1]:
                                        if isinstance(t, str) and len(t) > len(prev_text):
                                            delta = t[len(prev_text):]
                                            delta = clean_gemini_text(delta)
                                            if delta:
                                                yield delta
                                            prev_text = t
                    except (json.JSONDecodeError, IndexError, TypeError):
                        pass
            return
        except Exception as e:
            log(f"curl_cffi stream failed: {e}. Falling back to httpx...")

    if HAS_HTTPX:
        body = urllib.parse.urlencode(params)
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True) as client:
            with client.stream("POST", url, content=body, headers=headers) as resp:
                buf = ""
                for chunk in resp.iter_text():
                    buf += chunk
                    if "BardErrorInfo" in buf:
                        m = re.search(r'BardErrorInfo\s*\[(\d+)\]', buf)
                        if m:
                            refresh_xsrf_token(force=True, cookie_override=cookie_override, auth_user_override=auth_user_override)
                            raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{m.group(1)}]")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        if '"wrb.fr"' not in line or len(line) < 200:
                            continue
                            
                        # Parse and cache session IDs
                        if thread_id:
                            s_ids = extract_session_ids_from_line(line)
                            if s_ids:
                                THREAD_CACHE[thread_id] = s_ids
                                log(f"Cached stream session for {thread_id}: {s_ids}")
                                
                        try:
                            arr = json.loads(line)
                            inner_str = arr[0][2]
                            if not inner_str or len(inner_str) < 50:
                                continue
                            inner2 = json.loads(inner_str)
                            if isinstance(inner2, list) and len(inner2) > 4 and inner2[4]:
                                for part in inner2[4]:
                                    if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                                        for t in part[1]:
                                            if isinstance(t, str) and len(t) > len(prev_text):
                                                delta = t[len(prev_text):]
                                                delta = clean_gemini_text(delta)
                                                if delta:
                                                    yield delta
                                                prev_text = t
                        except (json.JSONDecodeError, IndexError, TypeError):
                            pass


def clean_gemini_text(text: str) -> str:
    """Remove internal code execution artifacts."""
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    return text.strip()


def extract_response_text(raw: str) -> str:
    """Parse StreamGenerate response to extract final text."""
    import re as _re
    bard_err = _re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    texts = []
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and part[1]:
                        if isinstance(part[1], list):
                            for t in part[1]:
                                if isinstance(t, str) and len(t) > 0:
                                    texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    text = ""
    for t in reversed(texts):
        if t.strip():
            text = t
            break
    return clean_gemini_text(text)


# ─── OpenAI Format Helpers ───────────────────────────────────────────────────

def messages_to_prompt(messages: list, tools: list = None) -> tuple:
    """Convert OpenAI messages to (prompt_str, images_list)."""
    parts = []
    images = []
    
    # 1. ALWAYS add the global system prompt if it exists (combine/prepend it)
    global_sys = CONFIG.get("system_prompt")
    if global_sys:
        parts.append(f"[System instruction]: {global_sys}")
        
    # Build list of normal conversation message blocks
    conv_parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for c in content:
                if c.get("type") in ("text", "input_text"):
                    text_parts.append(c.get("text", ""))
                elif c.get("type") == "image_url":
                    img_url = c.get("image_url", {}).get("url", "")
                    if img_url:
                        if img_url.startswith("data:"):
                            try:
                                header, b64_data = img_url.split(",", 1)
                                mime = header.split(";")[0].split(":")[1]
                                data = base64.b64decode(b64_data)
                                images.append((data, mime))
                            except Exception as e:
                                log(f"Failed to decode base64 image: {e}")
                        else:
                            images.append((img_url, None))
                elif c.get("type") == "image":
                    img_data = c.get("image", "")
                    if img_data:
                        try:
                            data = base64.b64decode(img_data)
                            mime = c.get("mime_type", "image/png")
                            images.append((data, mime))
                        except Exception as e:
                            log(f"Failed to decode base64 image: {e}")
                elif c.get("type") in ("document", "file"):
                    file_data = c.get("document", {}).get("data", "") or c.get("file", {}).get("data", "")
                    file_mime = c.get("document", {}).get("mime_type", "application/pdf") or c.get("file", {}).get("mime_type", "application/pdf")
                    if file_data:
                        try:
                            data = base64.b64decode(file_data)
                            images.append((data, file_mime))
                        except Exception as e:
                            log(f"Failed to decode base64 file: {e}")
                elif c.get("type") in ("file_url", "document_url"):
                    file_url = c.get("file_url", {}).get("url", "") or c.get("document_url", {}).get("url", "")
                    file_mime = c.get("file_url", {}).get("mime_type") or c.get("document_url", {}).get("mime_type")
                    if file_url:
                        images.append((file_url, file_mime))
            content = " ".join(text_parts)
            
        if role == "system":
            conv_parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            if msg.get("tool_calls"):
                tc_strs = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    tc_strs.append(
                        f'```tool_call\n{{"name": "{fn.get("name")}", '
                        f'"arguments": {fn.get("arguments", "{}")}}}\n```'
                    )
                conv_parts.append(f"[Assistant]: {content or ''}\n" + "\n".join(tc_strs))
            else:
                conv_parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            conv_parts.append(f"[Tool result for {msg.get('name', '')}]: {content}")
        else:
            conv_parts.append(content if content else "")

    # 2. Append conversation history except the last user message
    if conv_parts:
        if len(conv_parts) > 1:
            parts.extend(conv_parts[:-1])

    # 3. Add tools instructions immediately before the final user query to maximize prompt adherence
    if tools:
        tool_defs = []
        for tool in tools:
            fn = tool.get("function", tool) if tool.get("type") == "function" else tool
            tool_defs.append({
                "name": fn.get("name", tool.get("name", "")),
                "description": fn.get("description", tool.get("description", "")),
                "parameters": fn.get("parameters", tool.get("parameters", {})),
            })
        if tool_defs:
            parts.append(
                "[System instruction]: You are a local developer assistant with direct access to the system. "
                "You can execute system actions by calling the tools listed below.\n"
                "Whenever the user asks you to write files, create/list directories, run commands, or inspect the system, "
                "you MUST immediately execute the corresponding tool calls. Do NOT explain how the user can do it themselves; "
                "instead, execute the appropriate tool calls directly.\n"
                "To call a tool, respond with a JSON block in a code block:\n"
                '```tool_call\n{"name": "func_name", "arguments": {...}}\n```\n'
                "Ensure you close the code block with ```. Only use tool_call blocks when needed.\n\n"
                f"Available tools:\n{json.dumps(tool_defs, indent=2)}"
            )

    # 4. Append the final message of the history
    if conv_parts:
        parts.append(conv_parts[-1])
        
    prompt = "\n\n".join(p for p in parts if p)
    return prompt, images


def parse_tool_calls(text: str) -> tuple:
    """Extract tool_call blocks. Returns (clean_text, tool_calls_list)."""
    tool_calls = []
    
    # 1. Standard pattern: ```tool_call\n{...}\n```
    pattern_tool = r'```tool_call\s*\n(.*?)\n```'
    for match in re.findall(pattern_tool, text, re.DOTALL):
        try:
            data = json.loads(match.strip())
            if "name" in data:
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": data["name"],
                        "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False),
                    },
                })
        except Exception:
            pass
            
    # 2. JSON block pattern: ```json\n{...}\n``` (often generated by models instead of tool_call)
    pattern_json = r'```json\s*\n(.*?)\n```'
    for match in re.findall(pattern_json, text, re.DOTALL):
        try:
            data = json.loads(match.strip())
            if isinstance(data, dict) and "name" in data:
                args = data.get("arguments") or data.get("args") or {}
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": data["name"],
                        "arguments": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
                    },
                })
        except Exception:
            pass

    # 3. Fallback: Parse any ```bash / ```sh code blocks as command executions if no other tool calls found!
    if not tool_calls:
        pattern_bash = r'```(?:bash|sh|shell|zsh)\s*\n(.*?)\n```'
        for match in re.findall(pattern_bash, text, re.DOTALL):
            cmd = match.strip()
            if cmd:
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": "execute_command",
                        "arguments": json.dumps({"command": cmd}, ensure_ascii=False),
                    },
                })

    # Clean the output text
    clean = text
    clean = re.sub(pattern_tool, '', clean, flags=re.DOTALL)
    clean = re.sub(pattern_json, '', clean, flags=re.DOTALL)
    
    # If we parsed command execution from bash code blocks, we can strip them or leave them as notes
    if any(tc["function"]["name"] == "execute_command" for tc in tool_calls):
        clean = re.sub(r'```(?:bash|sh|shell|zsh)\s*\n(.*?)\n```', '', clean, flags=re.DOTALL)

    return clean.strip(), tool_calls


def is_command_safe(command: str) -> tuple:
    if not command:
        return False, "Empty command"
    cmd_clean = command.strip().lower()
    blacklist = [
        (r'\brm\s+-[rfRF]+', "Recursive deletion (rm -rf) is blocked for safety."),
        (r'\brm\s+(?:/[^/]+)+', "Absolute path deletion is blocked."),
        (r'\brm\s+~/?\s*$', "User directory deletion is blocked."),
        (r'\b(shutdown|reboot|poweroff|halt|init 0|init 6)\b', "System state changes are blocked."),
        (r'\bdd\s+if=', "Low-level disk manipulation is blocked."),
        (r'\b(?:mkfs|format|fdisk|parted)\b', "Disk formatting/partitioning is blocked."),
        (r'\b(?:sudo|su)\b', "Root privilege elevation is blocked."),
        (r'\|\s*(?:bash|sh|zsh|tcsh|csh|python)\b', "Piping directly to shell/interpreter is blocked."),
        (r'>\s*/dev/sd[a-z]', "Direct disk writing is blocked."),
        (r'>\s*/dev/nvme', "Direct NVMe disk writing is blocked."),
        (r'\bcrontab\s+-[eir]', "Interactive/destructive crontab modification is blocked."),
    ]
    for pattern, reason in blacklist:
        if re.search(pattern, cmd_clean):
            return False, reason
    return True, "Safe"


import threading

class MCPClient:
    def __init__(self, name, command, args):
        self.name = name
        self.command = command
        self.args = args
        self.proc = None
        self.request_id = 1
        self.pending_requests = {}
        self.lock = threading.Lock()
        
    def start(self):
        try:
            import subprocess
            self.proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            # Start reader thread
            threading.Thread(target=self._reader_loop, daemon=True).start()
            # Initialize connection
            self.send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "gemini-web2api-client", "version": "1.0.0"}
            })
            # Send initialized notification
            self.send_notification("notifications/initialized")
            return True
        except Exception as e:
            log(f"Failed to start MCP server {self.name}: {e}")
            return False
            
    def _reader_loop(self):
        while self.proc and self.proc.poll() is None:
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
                if "id" in msg:
                    req_id = msg["id"]
                    with self.lock:
                        if req_id in self.pending_requests:
                            self.pending_requests[req_id]["response"] = msg
                            self.pending_requests[req_id]["event"].set()
            except Exception as e:
                log(f"Error reading from MCP server {self.name}: {e}")
                
    def send_request(self, method, params=None, timeout=15):
        if not self.proc:
            return {"error": {"message": "MCP server not running"}}
        with self.lock:
            req_id = self.request_id
            self.request_id += 1
            
        req = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {}
        }
        
        event = threading.Event()
        res_container = {"response": None, "event": event}
        
        with self.lock:
            self.pending_requests[req_id] = res_container
            
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            return {"error": {"message": f"Write failed: {e}"}}
            
        if event.wait(timeout):
            with self.lock:
                return self.pending_requests.pop(req_id, None)["response"]
        else:
            with self.lock:
                self.pending_requests.pop(req_id, None)
            return {"error": {"message": "Request timeout"}}

    def send_notification(self, method, params=None):
        if not self.proc:
            return
        req = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {}
        }
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass


GLOBAL_MCP_CLIENTS = {}

def init_mcp_servers():
    config_path = "./mcp_config.json"
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                mcp_cfg = json.load(f)
            servers = mcp_cfg.get("mcpServers", {})
            for name, srv in servers.items():
                cmd = srv.get("command")
                args = srv.get("args", [])
                client = MCPClient(name, cmd, args)
                if client.start():
                    GLOBAL_MCP_CLIENTS[name] = client
                    log(f"Loaded MCP server: {name}")
        except Exception as e:
            log(f"Error loading MCP config: {e}")

def get_all_tools():
    tools = list(DEFAULT_SYSTEM_TOOLS)
    for name, client in GLOBAL_MCP_CLIENTS.items():
        res = client.send_request("tools/list")
        if res and "result" in res:
            for t in res["result"].get("tools", []):
                prefixed_name = f"{name}_{t['name']}"
                tools.append({
                    "type": "function",
                    "function": {
                        "name": prefixed_name,
                        "description": f"[{name}] {t.get('description', '')}",
                        "parameters": t.get("inputSchema", {"type": "object", "properties": {}})
                    }
                })
    return tools


DEFAULT_SYSTEM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or write content to a file on the local disk. Supports path expansions like ~/Desktop/file.txt",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "The target filepath to write."},
                    "content": {"type": "string", "description": "The text content to write."}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read and view the contents of a local file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "The filepath to read."}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Run a shell/terminal command locally on the system and return the output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command line string to run."}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the contents of a directory on the local disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "The directory path to list. Supports path expansions like ~/Desktop"}
                },
                "required": ["directory"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_grep",
            "description": "Recursively search for a text query/pattern in files within a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "The directory path to search in."},
                    "query": {"type": "string", "description": "The text pattern to search for."}
                },
                "required": ["directory", "query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch the content of a web page/URL and return it as text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The HTTP/HTTPS URL to fetch."}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_env",
            "description": "Get system environment information, including OS, python version, and environment variables.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sandboxed_command",
            "description": "Run a shell command inside a sandboxed Docker container (falls back to host execution if Docker is unavailable).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command line string to run."}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_inspect_screenshot",
            "description": "Render a web page and capture its screenshot and content snippet using Playwright/urllib.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to inspect."}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_code_symbols",
            "description": "Parse a code file (Python AST or JS/C/C++ regex) to extract classes, functions, and symbols.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "The filepath to analyze."}
                },
                "required": ["filepath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_memory_store",
            "description": "Store a text snippet or note into the agent's long-term SQLite database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The content string to store."},
                    "tags": {"type": "string", "description": "Optional space-separated tags."}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_memory_search",
            "description": "Semantically search the agent's long-term database for relevant notes and code snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The text query to search for."},
                    "limit": {"type": "integer", "description": "Max results to return (default 5)."}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_self_debug_loop",
            "description": "Execute a test or validation command. If it fails, alerts the agent of errors for TDD loops.",
            "parameters": {
                "type": "object",
                "properties": {
                    "test_command": {"type": "string", "description": "The test command to execute."}
                },
                "required": ["test_command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_system_process",
            "description": "Inspect active OS processes. Useful for debugging and process verification.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_name": {"type": "string", "description": "Optional substring filter to filter process names."}
                }
            }
        }
    }
]

def run_local_tool(name, arguments, yolo=False):
    import os, subprocess, json, sys, platform
    try:
        if name == "write_file":
            filepath = os.path.expanduser(arguments.get("filename", ""))
            dirname = os.path.dirname(filepath)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(arguments.get("content", ""))
            return {"status": "success", "message": f"File '{filepath}' written successfully."}
        elif name == "read_file":
            filepath = os.path.expanduser(arguments.get("filename", ""))
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            return {"status": "success", "content": content}
        elif name == "execute_command":
            cmd = arguments.get("command", "")
            safe, reason = is_command_safe(cmd)
            if not safe:
                return {"status": "blocked", "message": f"Command execution blocked: {reason}"}
            
            need_prompt = False
            if yolo:
                need_prompt = False
            elif CONFIG.get("require_command_approval") is True:
                need_prompt = True
            elif CONFIG.get("require_command_approval") is None or CONFIG.get("require_command_approval") is False:
                if sys.stdin.isatty() and CONFIG.get("require_command_approval") is not False:
                    need_prompt = True
                    
            if need_prompt:
                if not sys.stdin.isatty():
                    return {"status": "blocked", "message": "Command execution blocked: Interactive approval required but no terminal (TTY) is available."}
                sys.stderr.write(f"\n[APPROVAL REQUEST] Agent wants to execute command:\n  > {cmd}\nApprove? [y/N]: ")
                sys.stderr.flush()
                try:
                    import select
                    rlist, _, _ = select.select([sys.stdin], [], [], 30.0)
                    if rlist:
                        response = sys.stdin.readline().strip().lower()
                        if response not in ('y', 'yes'):
                            return {"status": "blocked", "message": "Command execution rejected by user."}
                    else:
                        return {"status": "blocked", "message": "Command execution timed out waiting for approval."}
                except Exception as e:
                    return {"status": "error", "message": f"Interactive approval error: {e}"}

            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            return {
                "status": "completed",
                "stdout": res.stdout,
                "stderr": res.stderr,
                "exit_code": res.returncode
            }
        elif name == "list_dir":
            directory = os.path.expanduser(arguments.get("directory", "."))
            files = []
            for entry in os.scandir(directory):
                files.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size": entry.stat().st_size if entry.is_file() else None
                })
            return {"status": "success", "directory": directory, "files": files}
        elif name == "search_grep":
            directory = os.path.expanduser(arguments.get("directory", "."))
            query = arguments.get("query", "")
            matches = []
            for root, _, filenames in os.walk(directory):
                for filename in filenames:
                    filepath = os.path.join(root, filename)
                    try:
                        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                            for i, line in enumerate(f, 1):
                                if query in line:
                                    matches.append({
                                        "file": filepath,
                                        "line": i,
                                        "content": line.strip()
                                    })
                                    if len(matches) >= 100:
                                        break
                    except Exception:
                        pass
                    if len(matches) >= 100:
                        break
                if len(matches) >= 100:
                    break
            return {"status": "success", "query": query, "matches": matches}
        elif name == "web_fetch":
            url = arguments.get("url", "")
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="replace")
            return {"status": "success", "url": url, "content": content[:50000]}
        elif name == "get_system_env":
            return {
                "status": "success",
                "os": platform.system(),
                "os_release": platform.release(),
                "python_version": sys.version,
                "cwd": os.getcwd(),
                "env": {k: v for k, v in os.environ.items() if not any(x in k.lower() for x in ["secret", "key", "token", "password", "auth", "cookie"])}
            }
        elif name == "execute_sandboxed_command":
            cmd = arguments.get("command", "")
            import shlex
            docker_cmd = f"docker run --rm -v {os.getcwd()}:/workspace -w /workspace python:3.11-slim sh -c {shlex.quote(cmd)}"
            try:
                res = subprocess.run(docker_cmd, shell=True, capture_output=True, text=True, timeout=60)
                if res.returncode == 127 or "docker: command not found" in res.stderr or "docker API" in res.stderr or "docker.sock" in res.stderr or "daemon is running" in res.stderr:
                    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
                    return {
                        "status": "completed_fallback_host",
                        "message": "Docker daemon not running or not available. Executed on host.",
                        "stdout": res.stdout,
                        "stderr": res.stderr,
                        "exit_code": res.returncode
                    }
                return {
                    "status": "completed_sandboxed",
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "exit_code": res.returncode
                }
            except Exception as e:
                return {"status": "error", "message": f"Sandbox execution error: {e}"}
        elif name == "web_inspect_screenshot":
            url = arguments.get("url", "")
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(url, timeout=30000)
                    screenshot_path = "page_screenshot.png"
                    page.screenshot(path=screenshot_path)
                    html_content = page.content()
                    browser.close()
                return {
                    "status": "success",
                    "screenshot_path": os.path.abspath(screenshot_path),
                    "content_snippet": html_content[:20000]
                }
            except Exception as e:
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content = resp.read().decode("utf-8", errors="replace")
                return {
                    "status": "success_fallback_text",
                    "message": f"Playwright not available: {e}. Returned text.",
                    "content_snippet": content[:20000]
                }
        elif name == "analyze_code_symbols":
            filepath = os.path.expanduser(arguments.get("filepath", ""))
            if not os.path.exists(filepath):
                return {"status": "error", "message": "File not found"}
            if filepath.endswith(".py"):
                import ast
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    code = f.read()
                tree = ast.parse(code, filename=filepath)
                symbols = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        symbols.append({"type": "class", "name": node.name, "line": node.lineno})
                    elif isinstance(node, ast.FunctionDef):
                        symbols.append({
                            "type": "function",
                            "name": node.name,
                            "line": node.lineno,
                            "args": [arg.arg for arg in node.args.args]
                        })
                return {"status": "success", "file": filepath, "symbols": symbols}
            else:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                symbols = []
                for idx, line in enumerate(lines, 1):
                    func_match = re.search(r'(?:function\s+(\w+)|(\w+)\s*\([^)]*\)\s*\{)', line)
                    class_match = re.search(r'class\s+(\w+)', line)
                    if func_match:
                        name = func_match.group(1) or func_match.group(2)
                        if name and name not in ("if", "for", "while", "switch", "catch"):
                            symbols.append({"type": "function/method", "name": name, "line": idx})
                    elif class_match:
                        symbols.append({"type": "class", "name": class_match.group(1), "line": idx})
                return {"status": "success", "file": filepath, "symbols": symbols[:100]}
        elif name == "semantic_memory_store":
            content = arguments.get("content", "")
            tags = arguments.get("tags", "")
            success = GLOBAL_MEMORY.store(content, tags)
            if success:
                return {"status": "success", "message": "Snippet stored in semantic memory."}
            return {"status": "error", "message": "Failed to store in database."}
        elif name == "semantic_memory_search":
            query = arguments.get("query", "")
            limit = int(arguments.get("limit", 5))
            results = GLOBAL_MEMORY.search(query, limit)
            return {"status": "success", "results": results}
        elif name == "run_self_debug_loop":
            test_cmd = arguments.get("test_command", "")
            res = subprocess.run(test_cmd, shell=True, capture_output=True, text=True, timeout=30)
            if res.returncode == 0:
                return {"status": "success", "message": "Validation passed.", "stdout": res.stdout}
            return {
                "status": "failed",
                "exit_code": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr,
                "message": "Validation command failed. Read the error above, modify the code files, and execute the tests again."
            }
        elif name == "inspect_system_process":
            filter_name = arguments.get("filter_name", "").lower()
            system = platform.system()
            processes = []
            if system == "Windows":
                cmd = "tasklist"
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                for line in res.stdout.splitlines():
                    if filter_name in line.lower():
                        processes.append(line)
            else:
                cmd = "ps aux"
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                for line in res.stdout.splitlines():
                    if filter_name in line.lower():
                        processes.append(line)
            return {"status": "success", "processes": processes[:100]}
        # Check if it is an MCP tool
        for srv_name, client in GLOBAL_MCP_CLIENTS.items():
            if name.startswith(f"{srv_name}_"):
                original_name = name[len(srv_name)+1:]
                res = client.send_request("tools/call", {
                    "name": original_name,
                    "arguments": arguments
                })
                if res and "result" in res:
                    content_parts = []
                    for item in res["result"].get("content", []):
                        if item.get("type") == "text":
                            content_parts.append(item.get("text", ""))
                    return {"status": "success", "content": "\n".join(content_parts)}
                else:
                    err_msg = res.get("error", {}).get("message", "Unknown error") if res else "No response from MCP server"
                    return {"status": "error", "message": f"MCP execution failed: {err_msg}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {"status": "error", "message": f"Tool '{name}' not found."}


# ─── HTTP Handler ────────────────────────────────────────────────────────────

class GeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(fmt % args)

    def send_json(self, data, status=200, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        auth = self.headers.get("Authorization", "")
        key = auth[7:] if auth.startswith("Bearer ") else self.headers.get("x-api-key", "")
        
        # 1. Local api_keys config validation
        if keys and key in keys:
            return True
        if not keys and not key:
            return True
            
        # 2. Mock OAuth portal token introspection
        if key:
            try:
                import urllib.request
                req = urllib.request.Request(
                    "http://localhost:8085/oauth/userinfo",
                    headers={"Authorization": f"Bearer {key}"}
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        profile = json.loads(resp.read().decode())
                        log(f"Authorized request via Mock OAuth user: {profile.get('email')}")
                        return True
            except Exception:
                pass
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        global _TOTAL_REQUESTS
        _TOTAL_REQUESTS += 1
        try:
            # Dashboard routes
            if self.path == "/dashboard" or self.path == "/dashboard/":
                self._handle_dashboard()
                return
            elif self.path == "/dashboard/api/status":
                self._handle_dashboard_status()
                return
            elif self.path == "/dashboard/api/logs":
                self._handle_dashboard_logs()
                return

            if self.path.startswith("/v1/") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            if self.path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif self.path.startswith("/v1beta/models"):
                self._handle_google_models_list()
            elif self.path == "/":
                self.send_json({"status": "ok", "version": __version__,
                                "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"GET error: {e}")

    def do_POST(self):
        global _TOTAL_REQUESTS
        _TOTAL_REQUESTS += 1
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b""
            
            # Dashboard routes
            if self.path == "/dashboard/api/cookies/refresh":
                self._handle_dashboard_cookies_refresh()
                return
            elif self.path == "/dashboard/api/config/save":
                self._handle_dashboard_config_save(body)
                return

            if self.path.startswith("/v1/") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            
            # Special bypass for Quota/Tier/Models to ensure UI stays active
            if ":retrieveUserQuota" in self.path or ":retrieveUserQuotaSummary" in self.path:
                self.send_json({"unlimited": True, "remainingFraction": 1.0, "remainingAmount": "999999999", "resetTime": "2030-01-01T00:00:00Z"})
                return
            elif ":loadCodeAssist" in self.path:
                self.send_json({
                    "currentTier": {"id": "standard-tier", "name": "Antigravity", "description": "Unlimited", "userDefinedCloudaicompanionProject": True, "privacyNotice": {}, "usesGcpTos": True},
                    "allowedTiers": [{"id": "standard-tier", "name": "Antigravity", "description": "Unlimited coding assistant with the most powerful Gemini models", "userDefinedCloudaicompanionProject": True, "privacyNotice": {}, "usesGcpTos": True}]
                })
                return
            elif ":fetchAvailableModels" in self.path:
                self.send_json({
                    "models": {
                        "gemini-3.1-pro-high": {
                            "displayName": "Gemini 3.1 Pro (High) [Bypassed]",
                            "maxTokens": 1048576,
                            "maxOutputTokens": 65535,
                            "tokenizerType": "LLAMA_WITH_SPECIAL",
                            "model": "MODEL_PLACEHOLDER_M37",
                            "apiProvider": "API_PROVIDER_GOOGLE_GEMINI",
                            "modelProvider": "MODEL_PROVIDER_GOOGLE",
                            "quotaInfo": {"remainingFraction": 1, "remainingAmount": "999999999", "resetTime": "2030-01-01T00:00:00Z"}
                        },
                        "gemini-3.5-flash": {
                            "displayName": "Gemini 3.5 Flash",
                            "maxTokens": 1048576,
                            "maxOutputTokens": 65535,
                            "tokenizerType": "LLAMA_WITH_SPECIAL",
                            "model": "MODEL_GOOGLE_GEMINI_2_5_FLASH",
                            "apiProvider": "API_PROVIDER_GOOGLE_GEMINI",
                            "modelProvider": "MODEL_PROVIDER_GOOGLE",
                            "quotaInfo": {"remainingFraction": 1, "remainingAmount": "999999999", "resetTime": "2030-01-01T00:00:00Z"}
                        }
                    },
                    "defaultAgentModelId": "gemini-3.5-flash",
                    "agentModelSorts": [{"displayName": "Recommended", "groups": [{"modelIds": ["gemini-3.5-flash", "gemini-3.1-pro-high"]}]}],
                    "commandModelIds": ["gemini-3.5-flash"],
                    "tabModelIds": ["gemini-3.5-flash"]
                })
                return

            if self.path == "/v1/chat/completions":
                self.handle_chat(body)
            elif self.path == "/v1/embeddings":
                self.handle_embeddings(body)
            elif self.path == "/v1/responses":
                self.handle_responses(body)
            elif ":generateContent" in self.path:
                self._handle_google_generate(body, stream=False)
            elif ":streamGenerateContent" in self.path:
                self._handle_google_generate(body, stream=True)
            elif "v1internal" in self.path:
                self.send_json({})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}, Content-Type: {self.headers.get('Content-Type')}, Body: {body[:100]}, Headers: {dict(self.headers)}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except:
                pass

    # ─── Dashboard Handlers ──────────────────────────────────────────────────
    def _handle_dashboard(self):
        try:
            html_content = ""
            dashboard_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
            if os.path.exists(dashboard_path):
                with open(dashboard_path, "r", encoding="utf-8") as f:
                    html_content = f.read()
            if not html_content:
                html_content = "<h1>dashboard.html not found</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_content.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(html_content.encode('utf-8'))
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def _handle_dashboard_status(self):
        uptime = int(time.time() - _START_TIME)
        tools = get_all_tools()
        cookie_pool_size = len(COOKIE_POOL) or (1 if CONFIG.get("cookie_file") else 0)
        status = {
            "version": __version__,
            "uptime": uptime,
            "total_requests": _TOTAL_REQUESTS,
            "cookie_pool_size": cookie_pool_size,
            "auth_user": CONFIG.get("auth_user"),
            "config": {
                "port": CONFIG.get("port"),
                "host": CONFIG.get("host"),
                "default_model": CONFIG.get("default_model"),
                "proxy": CONFIG.get("proxy"),
                "cookie_file": CONFIG.get("cookie_file"),
                "require_command_approval": CONFIG.get("require_command_approval"),
                "system_prompt": CONFIG.get("system_prompt")[:100] + "..." if CONFIG.get("system_prompt") else None
            },
            "tools": tools
        }
        self.send_json(status)

    def _handle_dashboard_logs(self):
        with _log_entries_lock:
            logs = list(LOG_ENTRIES)
        self.send_json(logs)

    def _handle_dashboard_cookies_refresh(self):
        try:
            refresh_xsrf_token(force=True)
            self.send_json({"status": "success", "message": "Tokens refreshed."})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)}, 500)

    def _handle_dashboard_config_save(self, body: bytes):
        try:
            req = json.loads(body)
            if "cookie_str" in req:
                # Custom cookie save
                cookie_file = CONFIG.get("cookie_file") or "./cookie.txt"
                cookie_data = {
                    "cookie": req["cookie_str"],
                    "sapisid": dict(p.split("=", 1) for p in req["cookie_str"].split("; ") if "=" in p).get("SAPISID", "")
                }
                with open(cookie_file, "w") as f:
                    json.dump(cookie_data, f, indent=2)
            
            for k, v in req.items():
                if k in CONFIG and k != "cookie_str":
                    CONFIG[k] = v
            # Save
            config_path = "./config.json"
            if os.path.exists(config_path):
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(CONFIG, f, indent=2)
            load_cookie_pool()
            self.send_json({"status": "success", "message": "Configuration updated."})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)}, 500)

    # ─── Embeddings Handler ──────────────────────────────────────────────────
    def handle_embeddings(self, body: bytes):
        req = json.loads(body)
        input_data = req.get("input", "")
        model = req.get("model", "text-embedding-004")
        
        if isinstance(input_data, str):
            inputs = [input_data]
        elif isinstance(input_data, list):
            inputs = [str(x) for x in input_data]
        else:
            inputs = [str(input_data)]
            
        api_key = CONFIG.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
        embeddings_data = []
        try:
            for idx, text in enumerate(inputs):
                if api_key:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={api_key}"
                    headers = {"Content-Type": "application/json"}
                    payload = {
                        "model": "models/text-embedding-004",
                        "content": {"parts": [{"text": text}]}
                    }
                    import urllib.request
                    req_obj = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
                    proxy = CONFIG.get("proxy")
                    ctx = ssl.create_default_context()
                    if proxy:
                        opener = urllib.request.build_opener(
                            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                            urllib.request.HTTPSHandler(context=ctx)
                        )
                        resp = opener.open(req_obj, timeout=15)
                    else:
                        resp = urllib.request.urlopen(req_obj, context=ctx, timeout=15)
                    res_data = json.loads(resp.read().decode())
                    vector = res_data["embedding"]["values"]
                else:
                    # Deterministic mock embedding
                    h = hashlib.sha256(text.encode()).digest()
                    vector = []
                    for i in range(1536):
                        val = hashlib.sha256(h + i.to_bytes(4, 'big')).digest()
                        ival = int.from_bytes(val[:4], 'big')
                        fval = (ival / 4294967295.0) * 2.0 - 1.0
                        vector.append(fval)
                        
                embeddings_data.append({
                    "object": "embedding",
                    "index": idx,
                    "embedding": vector
                })
                
            resp_payload = {
                "object": "list",
                "data": embeddings_data,
                "model": model,
                "usage": {
                    "prompt_tokens": sum(len(x)//4 for x in inputs),
                    "total_tokens": sum(len(x)//4 for x in inputs)
                }
            }
            self.send_json(resp_payload)
            add_api_log("IN", "/v1/embeddings", 200, req, resp_payload)
        except Exception as e:
            log(f"Embeddings error: {e}")
            self.send_json({"error": {"message": f"Embeddings failed: {e}"}}, 500)

    # ─── Model Resolution / Calls ─────────────────────────────────────────────
    def _resolve_model(self, model_name):
        think_override = None
        if "@think=" in model_name:
            model_name, think_str = model_name.rsplit("@think=", 1)
            think_override = int(think_str)
        cfg = MODELS.get(model_name)
        if not cfg:
            default = CONFIG.get("default_model") or "gemini-3.5-flash"
            log(f"Unknown model '{model_name}', falling back to '{default}'")
            model_name = default
            cfg = MODELS[default]
        return model_name, cfg["mode"], (think_override if think_override is not None else cfg["think"]), None

    def _call_gemini(self, prompt, model_id, think_mode, tools, file_refs=None, thread_id=None, cookie_override=None, auth_user_override=None):
        raw = gemini_stream_generate(prompt, model_id, think_mode, file_refs, thread_id=thread_id, cookie_override=cookie_override, auth_user_override=auth_user_override)
        text = extract_response_text(raw)
        tool_calls = None
        if tools and text:
            text, tool_calls = parse_tool_calls(text)
        return text or "", tool_calls

    def handle_chat(self, body: bytes):
        req = json.loads(body)
        thread_id = req.get("thread_id") or req.get("user") or self.headers.get("X-Thread-ID") or self.headers.get("x-thread-id")
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        cookie_override = self.headers.get("x-gemini-cookie") or self.headers.get("X-Gemini-Cookie")
        auth_user_override = self.headers.get("x-gemini-authuser") or self.headers.get("X-Gemini-AuthUser")

        tools = req.get("tools")
        client_handles_tools = (tools is not None)
        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if not client_handles_tools:
            # Local agent loop on proxy
            chat_log = list(req.get("messages", []))
            final_text = ""
            executed_calls = {}
            for turn in range(5):
                prompt, images = messages_to_prompt(chat_log, get_all_tools())
                if not prompt.strip():
                    self.send_json({"error": {"message": "empty prompt"}}, 400)
                    return
                try:
                    file_refs = upload_images_helper(images, cookie_override=cookie_override, auth_user_override=auth_user_override) if images else None
                    raw = gemini_stream_generate(prompt, model_id, think_mode, file_refs, thread_id=thread_id, cookie_override=cookie_override, auth_user_override=auth_user_override)
                    text = extract_response_text(raw)
                except Exception as e:
                    self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
                    return
                
                clean_text, tool_calls = parse_tool_calls(text or "")
                if not tool_calls:
                    final_text = clean_text
                    break
                
                # Loop Evasion
                loop_detected = False
                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    fn_args_str = tc["function"]["arguments"]
                    call_key = (fn_name, fn_args_str)
                    executed_calls[call_key] = executed_calls.get(call_key, 0) + 1
                    if executed_calls[call_key] > 2:
                        log(f"Loop detected for tool call: {fn_name}({fn_args_str})")
                        loop_detected = True
                        break
                        
                if loop_detected:
                    final_text = (clean_text or "") + "\n\n[Proxy: Tool execution terminated to prevent infinite loop.]"
                    break
                
                # Record assistant turn
                assistant_msg = {"role": "assistant", "content": clean_text or None}
                chat_log.append(assistant_msg)
                
                # Run each tool call locally
                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    fn_args = {}
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        pass
                    log(f"Proxy executing local tool: {fn_name}({fn_args})")
                    yolo_req = (self.headers.get("X-Yolo", "").lower() == "true")
                    tool_res = run_local_tool(fn_name, fn_args, yolo=yolo_req)
                    log(f"Proxy tool result: {tool_res}")
                    
                    chat_log.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": fn_name,
                        "content": json.dumps(tool_res)
                    })
            else:
                final_text = "Tool execution limit reached."

            # Return final response
            msg = {"role": "assistant", "content": final_text or None}
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                if thread_id:
                    self.send_header("X-Thread-ID", thread_id)
                self.end_headers()
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model_name, "choices": [{"index": 0, "delta": msg, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                add_api_log("IN", "/v1/chat/completions", 200, req, f"[Streamed response: {final_text[:100]}...]")
            else:
                resp_data = {
                    "id": cid, "object": "chat.completion", "created": int(time.time()),
                    "model": model_name,
                    "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(final_text)//4,
                              "total_tokens": (len(prompt)+len(final_text))//4},
                }
                extra_hdrs = {"X-Thread-ID": thread_id} if thread_id else None
                if thread_id:
                    resp_data["thread_id"] = thread_id
                self.send_json(resp_data, headers=extra_hdrs)
                add_api_log("IN", "/v1/chat/completions", 200, req, resp_data)
            return

        # Smart client flow (client handles tool execution)
        prompt, images = messages_to_prompt(req.get("messages", []), tools)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        if stream and not tools:
            # True streaming: forward chunks as they arrive
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                if thread_id:
                    self.send_header("X-Thread-ID", thread_id)
                self.end_headers()
                file_refs = upload_images_helper(images, cookie_override=cookie_override, auth_user_override=auth_user_override) if images else None
                full_text = ""
                for delta_text in gemini_stream_generate_iter(prompt, model_id, think_mode, file_refs, thread_id=thread_id, cookie_override=cookie_override, auth_user_override=auth_user_override):
                    full_text += delta_text
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                # Final chunk
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                add_api_log("IN", "/v1/chat/completions", 200, req, f"[Streamed response: {full_text[:100]}...]")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Stream error: {e}")
            return

        # Non-streaming (or tool calling which needs full response)
        try:
            file_refs = upload_images_helper(images, cookie_override=cookie_override, auth_user_override=auth_user_override) if images else None
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools, file_refs, thread_id=thread_id, cookie_override=cookie_override, auth_user_override=auth_user_override)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            # Stream mode with tools: send as single chunk (need full parse for tool_calls)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            if thread_id:
                self.send_header("X-Thread-ID", thread_id)
            self.end_headers()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name, "choices": [{"index": 0, "delta": msg, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            add_api_log("IN", "/v1/chat/completions", 200, req, f"[Streamed tool call: {msg}]")
        else:
            resp_data = {
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text)//4,
                          "total_tokens": (len(prompt)+len(text))//4},
            }
            extra_hdrs = {"X-Thread-ID": thread_id} if thread_id else None
            if thread_id:
                resp_data["thread_id"] = thread_id
            self.send_json(resp_data, headers=extra_hdrs)
            add_api_log("IN", "/v1/chat/completions", 200, req, resp_data)

    def handle_responses(self, body: bytes):
        """OpenAI Responses API for Codex CLI compatibility."""
        req = json.loads(body)
        thread_id = req.get("thread_id") or req.get("user") or self.headers.get("X-Thread-ID") or self.headers.get("x-thread-id")
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        cookie_override = self.headers.get("x-gemini-cookie") or self.headers.get("X-Gemini-Cookie")
        auth_user_override = self.headers.get("x-gemini-authuser") or self.headers.get("X-Gemini-AuthUser")

        input_items = req.get("input", [])
        tools = req.get("tools")

        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    if item.get("type") == "function_call_output":
                        messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                         "name": item.get("name", ""), "content": item.get("output", "")})
                    elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                        cp = item.get("content", [])
                        text_acc, tc_list = "", []
                        if isinstance(cp, list):
                            for c in cp:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text": text_acc += c.get("text", "")
                                    elif c.get("type") == "function_call": tc_list.append(c)
                        elif isinstance(cp, str):
                            text_acc = cp
                        m = {"role": "assistant", "content": text_acc or None}
                        if tc_list:
                            m["tool_calls"] = [{"id": tc.get("call_id", f"call_{i}"), "type": "function",
                                                "function": {"name": tc.get("name",""), "arguments": tc.get("arguments","{}")}}
                                               for i, tc in enumerate(tc_list)]
                        messages.append(m)
                    else:
                        role = item.get("role", "user")
                        content = item.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(c.get("text", "") for c in content if c.get("type") in ("text", "input_text"))
                        messages.append({"role": role, "content": content})

        # Count tool calls in input history to prevent loops
        historical_calls = {}
        for msg in messages:
            if "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    fn = tc["function"]["name"]
                    args = tc["function"]["arguments"]
                    key = (fn, args)
                    historical_calls[key] = historical_calls.get(key, 0) + 1

        loop_detected = False
        for key, count in historical_calls.items():
            if count >= 2:
                log(f"Loop detected in history for tool call: {key[0]}({key[1]})")
                loop_detected = True
                break

        if loop_detected:
            # Strip tools to force text response and break the loop
            tools = None

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        prompt, images = messages_to_prompt(messages, tools)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        try:
            file_refs = upload_images_helper(images, cookie_override=cookie_override, auth_user_override=auth_user_override) if images else None
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools, file_refs, thread_id=thread_id, cookie_override=cookie_override, auth_user_override=auth_user_override)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})

        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            ev = {"type": "response.created", "response": {"id": rid, "object": "response", "status": "in_progress", "model": model_name, "output": []}}
            self.wfile.write(f"event: response.created\ndata: {json.dumps(ev)}\n\n".encode())
            for item in output:
                if item["type"] == "function_call":
                    ev = {"type": "response.function_call_arguments.done", "item_id": item["id"], "call_id": item["call_id"], "name": item["name"], "arguments": item["arguments"]}
                    self.wfile.write(f"event: response.function_call_arguments.done\ndata: {json.dumps(ev)}\n\n".encode())
                elif item["type"] == "message":
                    for ci, cp in enumerate(item["content"]):
                        ev = {"type": "response.output_text.done", "item_id": item["id"], "content_index": ci, "text": cp["text"]}
                        self.wfile.write(f"event: response.output_text.done\ndata: {json.dumps(ev)}\n\n".encode())
            resp_obj = {"id": rid, "object": "response", "status": "completed", "model": model_name, "output": output,
                        "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}}
            self.wfile.write(f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': resp_obj})}\n\n".encode())
            self.wfile.flush()
            add_api_log("IN", "/v1/responses", 200, req, f"[Streamed responses response]")
        else:
            resp_data = {"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                         "model": model_name, "output": output,
                         "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}}
            self.send_json(resp_data)
            add_api_log("IN", "/v1/responses", 200, req, resp_data)


    # ─── Google Native API (Gemini CLI compatible) ────────────────────────────

    def _parse_google_model_from_path(self):
        """Extract model name from /v1beta/models/{model}:method path."""
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        if m:
            return m.group(1)
        return None

    def _handle_google_models_list(self):
        """GET /v1beta/models — Google AI format model list."""
        models = []
        for name, cfg in MODELS.items():
            models.append({
                "name": f"models/{name}",
                "displayName": name,
                "description": cfg["desc"],
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            })
        self.send_json({"models": models})

    def _google_contents_to_prompt(self, req: dict) -> str:
        """Convert Google API contents format to prompt string."""
        parts = []
        global_sys = CONFIG.get("system_prompt")
        if global_sys:
            parts.append(f"[System instruction]: {global_sys}")
        sys_inst = req.get("systemInstruction")
        if sys_inst:
            sys_parts = sys_inst.get("parts", [])
            sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
            if sys_text:
                parts.append(f"[System instruction]: {sys_text}")

        for content in req.get("contents", []):
            role = content.get("role", "user")
            text_parts = []
            for p in content.get("parts", []):
                if p.get("text"):
                    text_parts.append(p["text"])
            text = " ".join(text_parts)
            if role == "model":
                parts.append(f"[Assistant]: {text}")
            else:
                parts.append(text)
        return "\n\n".join(p for p in parts if p)

    def _handle_google_generate(self, body: bytes, stream: bool):
        """Handle Google native generateContent / streamGenerateContent."""
        req = json.loads(body)
        model_name = self._parse_google_model_from_path()
        if not model_name:
            self.send_json({"error": {"message": "model not specified in path"}}, 400)
            return

        model_name, model_id, think_mode, err = self._resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        # Local agent loop for Google Native endpoints (e.g. gemini CLI)
        chat_log = []
        sys_inst = req.get("systemInstruction")
        if sys_inst:
            sys_parts = sys_inst.get("parts", [])
            sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
            if sys_text:
                chat_log.append({"role": "system", "content": sys_text})
        for content in req.get("contents", []):
            role = content.get("role", "user")
            role_map = {"model": "assistant", "user": "user"}
            msg_parts = []
            for p in content.get("parts", []):
                if p.get("text"):
                    msg_parts.append({"type": "text", "text": p["text"]})
                elif p.get("inlineData"):
                    inline = p["inlineData"]
                    msg_parts.append({
                        "type": "image",
                        "image": inline.get("data", ""),
                        "mime_type": inline.get("mimeType", "image/png")
                    })
            if len(msg_parts) == 1 and msg_parts[0]["type"] == "text":
                chat_log.append({"role": role_map.get(role, "user"), "content": msg_parts[0]["text"]})
            else:
                chat_log.append({"role": role_map.get(role, "user"), "content": msg_parts})

        cookie_override = self.headers.get("x-gemini-cookie") or self.headers.get("X-Gemini-Cookie")
        auth_user_override = self.headers.get("x-gemini-authuser") or self.headers.get("X-Gemini-AuthUser")

        final_text = ""
        last_prompt_len = 0
        executed_calls = {}
        for turn in range(5):
            prompt, images = messages_to_prompt(chat_log, get_all_tools())
            if not prompt.strip():
                self.send_json({"error": {"message": "empty content"}}, 400)
                return
            last_prompt_len = len(prompt)
            try:
                file_refs = upload_images_helper(images, cookie_override=cookie_override, auth_user_override=auth_user_override) if images else None
                raw = gemini_stream_generate(prompt, model_id, think_mode, file_refs, cookie_override=cookie_override, auth_user_override=auth_user_override)
                text = extract_response_text(raw)
            except Exception as e:
                self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
                return
            
            clean_text, tool_calls = parse_tool_calls(text or "")
            if not tool_calls:
                final_text = clean_text
                break
            
            # Loop Evasion
            loop_detected = False
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args_str = tc["function"]["arguments"]
                call_key = (fn_name, fn_args_str)
                executed_calls[call_key] = executed_calls.get(call_key, 0) + 1
                if executed_calls[call_key] > 2:
                    log(f"Loop detected for tool call: {fn_name}({fn_args_str})")
                    loop_detected = True
                    break

            if loop_detected:
                final_text = (clean_text or "") + "\n\n[Proxy: Tool execution terminated to prevent infinite loop.]"
                break

            # Record assistant turn
            assistant_msg = {"role": "assistant", "content": clean_text or None}
            chat_log.append(assistant_msg)
            
            # Run each tool call locally
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = {}
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except Exception:
                    pass
                log(f"Proxy executing local tool: {fn_name}({fn_args})")
                yolo_req = (self.headers.get("X-Yolo", "").lower() == "true")
                tool_res = run_local_tool(fn_name, fn_args, yolo=yolo_req)
                log(f"Proxy tool result: {tool_res}")
                
                chat_log.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": fn_name,
                    "content": json.dumps(tool_res)
                })
        else:
            final_text = "Tool execution limit reached."

        candidate = {
            "content": {"parts": [{"text": final_text or ""}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": last_prompt_len // 4,
            "candidatesTokenCount": len(final_text or "") // 4,
            "totalTokenCount": (last_prompt_len + len(final_text or "")) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(response_obj)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json(response_obj)


# ─── Main ────────────────────────────────────────────────────────────────────

def load_config(path: str):
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
        log(f"Config loaded: {path}")
        
        sys_prompt = CONFIG.get("system_prompt")
        if sys_prompt:
            config_dir = os.path.dirname(os.path.abspath(path))
            possible_path = sys_prompt
            if not os.path.isabs(possible_path):
                possible_path = os.path.join(config_dir, possible_path)
            if os.path.exists(possible_path):
                with open(possible_path, "r", encoding="utf-8") as pf:
                    CONFIG["system_prompt"] = pf.read()
                log(f"Loaded system_prompt from file: {possible_path}")



def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None, help="Path to cookie file")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--yolo", action="store_true", help="Bypass command execution confirmation prompts")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG")
    if not config_path:
        for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
            if os.path.exists(p):
                config_path = p
                break
    load_config(config_path)
    load_cookie_pool()

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
        load_cookie_pool()
    if args.proxy:
        CONFIG["proxy"] = args.proxy
    if args.yolo:
        CONFIG["require_command_approval"] = False

    # Auto-extract cookies if not present
    auto_extract_cookies()
    load_cookie_pool()
    
    # If still no cookies in pool, try interactive browser refresh
    if not COOKIE_POOL:
        log("No cookies found in pool. Attempting interactive browser login...")
        refresh_cookies_via_browser()

    # Discover active Google accounts
    discover_active_account()

    # Initial XSRF token fetch
    refresh_xsrf_token()

    class ThreadedServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://0.0.0.0:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes (' + CONFIG['cookie_file'] + ')' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'none (uses system env HTTP_PROXY/HTTPS_PROXY)'}")
    print(f"  Retry:     {CONFIG['retry_attempts']}x / {CONFIG['retry_delay_sec']}s")
    print(f"  YOLO Mode: {'active' if CONFIG.get('require_command_approval') is False else 'inactive'}")
    print()
    init_mcp_servers()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
