"""Gemini StreamGenerate protocol implementation with httpx streaming."""
import json
import time
import uuid
import re
import urllib.request
import urllib.parse
import ssl
import os
import hashlib
import base64

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

from .config import CONFIG

import random

_ssl_ctx = None
_cookie_cache = {"str": "", "sapisid": None, "mtime": 0}
_httpx_client = None

UA_PROFILES = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "chrome"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "chrome120"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "chrome124"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "chrome119"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "chrome"),
]
_SELECTED_UA, _SELECTED_IMPERSONATE = random.choice(UA_PROFILES)
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
        import sys
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _get_httpx_client():
    global _httpx_client
    if _httpx_client is None and HAS_HTTPX:
        proxy = CONFIG.get("proxy")
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        _httpx_client = httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True)
    return _httpx_client


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

def load_cookie(cookie_override: str = None) -> tuple:
    """Load cookie from file or override header with mtime-based caching."""
    if cookie_override:
        pairs = dict(p.split("=", 1) for p in cookie_override.split("; ") if "=" in p)
        sapisid = pairs.get("SAPISID", "")
        return cookie_override, sapisid if sapisid else None

    cookie_file = get_next_cookie_file()
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        mtime = os.path.getmtime(cookie_file)
        if mtime == _cookie_cache["mtime"] and _cookie_cache["str"]:
            return _cookie_cache["str"], _cookie_cache["sapisid"]
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
        _cookie_cache.update({"str": cookie_str, "sapisid": sapisid or None, "mtime": mtime})
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return _cookie_cache["str"], _cookie_cache["sapisid"]


import threading
_token_lock = threading.Lock()
_last_token_refresh = 0

def refresh_xsrf_token(force=False, cookie_override: str = None, auth_user_override: str = None):
    global _last_token_refresh
    now = time.time()

    cookie_str, sapisid = load_cookie(cookie_override)
    cached_xsrf, cached_bl = get_xsrf_and_bl(cookie_str)
    if not force and cached_xsrf:
        CONFIG["xsrf_token"] = cached_xsrf
        CONFIG["gemini_bl"] = cached_bl
        return

    if not force and (now - _last_token_refresh < 600) and CONFIG.get("xsrf_token") and not cookie_override:
        return
        
    with _token_lock:
        if not force and (now - _last_token_refresh < 600) and CONFIG.get("xsrf_token") and not cookie_override:
            return
            
        cookie_str, sapisid = load_cookie(cookie_override)
        prefix = _account_prefix()
        # Handle auth_user override
        auth_user = auth_user_override if auth_user_override is not None else CONFIG.get("auth_user")
        if auth_user is not None and str(auth_user) != "0":
            prefix = f"/u/{auth_user}"
            
        url = f"https://gemini.google.com{prefix}/app"
        
        headers = {
            "User-Agent": _SELECTED_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cookie": cookie_str
        }
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
                ctx = _get_ssl_ctx()
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
                xsrf_token = data.get("thykhd")
                gemini_bl = data.get("cfb2h")
                if xsrf_token:
                    set_xsrf_and_bl(cookie_str, xsrf_token, gemini_bl)
                    CONFIG["xsrf_token"] = xsrf_token
                if gemini_bl:
                    CONFIG["gemini_bl"] = gemini_bl
                _last_token_refresh = time.time()
                log(f"Successfully refreshed XSRF token: {CONFIG['xsrf_token'][:20]}..., BL: {CONFIG['gemini_bl']}")
            else:
                log("Failed to find window.WIZ_global_data in HTML to refresh XSRF token.")
        except Exception as e:
            log(f"Error refreshing XSRF token: {e}")


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
    if os.name != "nt" and os.path.exists("/System/Library/CoreServices/SystemVersion.plist"):
        paths = [
            ("Chrome", "~/Library/Application Support/Google/Chrome/Default/Network/Cookies", "Chrome Safe Storage"),
            ("Chrome Profile 1", "~/Library/Application Support/Google/Chrome/Profile 1/Network/Cookies", "Chrome Safe Storage"),
            ("Chrome Profile 2", "~/Library/Application Support/Google/Chrome/Profile 2/Network/Cookies", "Chrome Safe Storage"),
            ("Brave", "~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Network/Cookies", "Brave Safe Storage"),
            ("Edge", "~/Library/Application Support/Microsoft Edge/Default/Network/Cookies", "Microsoft Edge Safe Storage"),
        ]
        for name, path_raw, service in paths:
            path = os.path.expanduser(path_raw)
            if not os.path.exists(path):
                continue
            key = get_mac_key(service)
            if not key:
                continue
            try:
                import sqlite3
                conn = sqlite3.connect(path)
                cursor = conn.cursor()
                cursor.execute("SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%google.com'")
                cookies = []
                sapisid = None
                for cname, cval in cursor.fetchall():
                    decrypted = decrypt_mac_cookie_openssl(cval, key)
                    if decrypted:
                        cookies.append(f"{cname}={decrypted}")
                        if cname == "SAPISID":
                            sapisid = decrypted
                conn.close()
                if sapisid:
                    cookie_str = "; ".join(cookies)
                    with open(cookie_file, "w") as f:
                        json.dump({"cookie": cookie_str, "sapisid": sapisid}, f, indent=2)
                    log(f"[✓] Cookies successfully extracted natively from macOS {name} and saved to {cookie_file}")
                    return True
            except Exception as e:
                log(f"Failed native extraction from {name}: {e}")
                
    # Windows Chrome/Brave/Edge SQLite extraction
    elif os.name == "nt":
        paths = [
            ("Chrome", "~/AppData/Local/Google/Chrome/User Data/Default/Network/Cookies"),
            ("Chrome Profile 1", "~/AppData/Local/Google/Chrome/User Data/Profile 1/Network/Cookies"),
            ("Chrome Profile 2", "~/AppData/Local/Google/Chrome/User Data/Profile 2/Network/Cookies"),
            ("Brave", "~/AppData/Local/BraveSoftware/Brave-Browser/User Data/Default/Network/Cookies"),
            ("Edge", "~/AppData/Local/Microsoft/Edge/User Data/Default/Network/Cookies"),
        ]
        for name, path_raw in paths:
            path = os.path.expanduser(path_raw)
            if not os.path.exists(path):
                continue
            local_state_path = os.path.join(os.path.dirname(os.path.dirname(path)), "Local State")
            if not os.path.exists(local_state_path):
                continue
            try:
                with open(local_state_path, "r", encoding="utf-8") as f:
                    local_state = json.load(f)
                encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
                master_key = decrypt_dpapi(encrypted_key[5:]) # Remove 'DPAPI' prefix
                if not master_key:
                    continue
                import sqlite3
                conn = sqlite3.connect(path)
                cursor = conn.cursor()
                cursor.execute("SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%google.com'")
                cookies = []
                sapisid = None
                for cname, cval in cursor.fetchall():
                    if cval.startswith(b"v10") or cval.startswith(b"v11"):
                        # AES-256-GCM
                        from Crypto.Cipher import AES
                        nonce = cval[3:15]
                        ciphertext = cval[15:-16]
                        tag = cval[-16:]
                        cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
                        decrypted = cipher.decrypt_and_verify(ciphertext, tag).decode('utf-8')
                    else:
                        decrypted = decrypt_dpapi(cval).decode('utf-8')
                    if decrypted:
                        cookies.append(f"{cname}={decrypted}")
                        if cname == "SAPISID":
                            sapisid = decrypted
                conn.close()
                if sapisid:
                    cookie_str = "; ".join(cookies)
                    with open(cookie_file, "w") as f:
                        json.dump({"cookie": cookie_str, "sapisid": sapisid}, f, indent=2)
                    log(f"[✓] Cookies successfully extracted natively from Windows {name} and saved to {cookie_file}")
                    return True
            except Exception as e:
                log(f"Failed native extraction from {name}: {e}")
    return False

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
        ("Opera", browser_cookie3.opera),
        ("Firefox", browser_cookie3.firefox),
        ("Safari", browser_cookie3.safari),
        ("Edge", browser_cookie3.edge)
    ]
    
    for name, func in browsers:
        try:
            log(f"Trying to extract cookies from {name}...")
            cj = func(domain_name="gemini.google.com")
            cookie_str = "; ".join(f"{c.name}={c.value}" for c in cj)
            sapisid = next((c.value for c in cj if c.name == "SAPISID"), None)
            if sapisid:
                with open(cookie_file, "w") as f:
                    json.dump({"cookie": cookie_str, "sapisid": sapisid}, f, indent=2)
                log(f"[✓] Cookies successfully extracted from {name} and saved to {cookie_file}")
                load_cookie_pool()
                return
        except Exception as e:
            log(f"Extraction from {name} failed: {e}")
            
    log("Auto-extraction failed. Please place your cookie in cookie.txt or use interactive browser refresh.")


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
                ctx = _get_ssl_ctx()
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


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _build_headers(cookie_override: str = None, auth_user_override: str = None) -> dict:
    auth_user = auth_user_override if auth_user_override is not None else CONFIG.get("auth_user")
    account_prefix = ""
    if auth_user is not None and str(auth_user) != "0":
        account_prefix = f"/u/{auth_user}"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": _SELECTED_UA,
    }
    if auth_user is not None:
        headers["X-Goog-AuthUser"] = str(auth_user)
    cookie_str, sapisid = load_cookie(cookie_override)
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, conversation_id: str = None, session_ctx = None) -> str:
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    if conversation_id and session_ctx:
        if isinstance(session_ctx, dict):
            resp_id = session_ctx.get("18")
            choice_list = session_ctx.get("21")
            choice_id = choice_list[0] if choice_list and isinstance(choice_list, list) else ""
            inner[2] = [conversation_id, resp_id, choice_id, None, None, []]
        else:
            inner[2] = [conversation_id, session_ctx, "", None, None, []]
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
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _get_url(auth_user_override: str = None) -> str:
    reqid = int(time.time()) % 1000000
    auth_user = auth_user_override if auth_user_override is not None else CONFIG.get("auth_user")
    account_prefix = ""
    if auth_user is not None and str(auth_user) != "0":
        account_prefix = f"/u/{auth_user}"
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def clean_text(text: str) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip()


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    last_text = ""
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            if len(t) > len(last_text):
                last_text = t
    return clean_text(last_text)


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, thread_id: str = None, cookie_override: str = None, auth_user_override: str = None) -> str:
    """Non-streaming generation with retry."""
    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            refresh_xsrf_token(cookie_override=cookie_override, auth_user_override=auth_user_override)

            conv_id, session_ctx = None, None
            if thread_id:
                cached = THREAD_CACHE.get(thread_id)
                if cached:
                    conv_id, session_ctx = cached

            body_str = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields, conversation_id=conv_id, session_ctx=session_ctx)
            
            url = _get_url(auth_user_override)
            headers = _build_headers(cookie_override, auth_user_override)
            proxy = get_next_proxy()
            proxies = {"http": proxy, "https": proxy} if proxy else None

            if HAS_CURL_CFFI:
                params = dict(urllib.parse.parse_qsl(body_str))
                resp = curl_requests.post(url, data=params, headers=headers, impersonate=_SELECTED_IMPERSONATE, timeout=CONFIG["request_timeout_sec"], proxies=proxies)
                raw = resp.text
            else:
                body_bytes = body_str.encode()
                req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
                ctx = _get_ssl_ctx()
                if proxy:
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                        urllib.request.HTTPSHandler(context=ctx)
                    )
                    resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
                else:
                    resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
                raw = resp.read().decode("utf-8", errors="replace")

            if "BardErrorInfo" in raw:
                m = re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
                if m:
                    log(f"Gemini returned BardErrorInfo [{m.group(1)}]. Retrying with fresh token...")
                    refresh_xsrf_token(force=True, cookie_override=cookie_override, auth_user_override=auth_user_override)
                    raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{m.group(1)}]")

            # Parse and cache session IDs
            if thread_id:
                for line in raw.split("\n"):
                    s_ids = extract_session_ids_from_line(line)
                    if s_ids:
                        THREAD_CACHE[thread_id] = s_ids
                        log(f"Cached thread session for {thread_id}: {s_ids}")
                        break

            return extract_response_text(raw)
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, thread_id: str = None, cookie_override: str = None, auth_user_override: str = None):
    """Streaming generation via curl_cffi/httpx with retry on connection failure."""
    if not HAS_CURL_CFFI and not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields, thread_id=thread_id, cookie_override=cookie_override, auth_user_override=auth_user_override)
        if text:
            yield text
        return

    refresh_xsrf_token(cookie_override=cookie_override, auth_user_override=auth_user_override)

    conv_id, session_ctx = None, None
    if thread_id:
        cached = THREAD_CACHE.get(thread_id)
        if cached:
            conv_id, session_ctx = cached

    body_str = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields, conversation_id=conv_id, session_ctx=session_ctx)
    url = _get_url(auth_user_override)
    headers = _build_headers(cookie_override, auth_user_override)
    proxy = get_next_proxy()
    proxies = {"http": proxy, "https": proxy} if proxy else None

    prev_text = ""
    if HAS_CURL_CFFI:
        try:
            params = dict(urllib.parse.parse_qsl(body_str))
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
                    
                    # Parse and cache session IDs
                    if thread_id:
                        s_ids = extract_session_ids_from_line(line)
                        if s_ids:
                            THREAD_CACHE[thread_id] = s_ids
                            log(f"Cached stream session for {thread_id}: {s_ids}")
                            
                    for t in _extract_texts_from_line(line):
                        if len(t) > len(prev_text):
                            delta = clean_text(t[len(prev_text):])
                            if delta:
                                yield delta
                            prev_text = t
            return
        except Exception as e:
            log(f"curl_cffi stream failed: {e}. Falling back to httpx...")

    if HAS_HTTPX:
        body_bytes = body_str.encode()
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True) as client:
            with client.stream("POST", url, content=body_bytes, headers=headers) as resp:
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
                        
                        # Parse and cache session IDs
                        if thread_id:
                            s_ids = extract_session_ids_from_line(line)
                            if s_ids:
                                THREAD_CACHE[thread_id] = s_ids
                                log(f"Cached stream session for {thread_id}: {s_ids}")
                                
                        for t in _extract_texts_from_line(line):
                            if len(t) > len(prev_text):
                                delta = clean_text(t[len(prev_text):])
                                if delta:
                                    yield delta
                                prev_text = t



