"""Multimodal: Scotty resumable upload for Gemini image input."""
import json
import base64
import urllib.request
import urllib.parse
import time
import ssl
import re

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

from .config import CONFIG
from .gemini import load_cookie, make_sapisidhash, _get_ssl_ctx, log, _account_prefix, _SELECTED_UA, _SELECTED_IMPERSONATE


def fetch_image_bytes(url: str) -> bytes:
    """Fetch image bytes from URL, using curl_cffi if available."""
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
    """Upload image via Scotty resumable upload. Returns file reference path."""
    cookie_str, sapisid = load_cookie(cookie_override)
    auth_user = auth_user_override if auth_user_override is not None else CONFIG.get("auth_user")
    prefix = ""
    if auth_user is not None and str(auth_user) != "0":
        prefix = f"/u/{auth_user}"
    
    app_url = f"https://gemini.google.com{prefix}/app"
    headers = {
        "User-Agent": _SELECTED_UA,
        "Cookie": cookie_str
    }
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

