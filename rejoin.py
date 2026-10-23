#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GARNT TOOL REJOIN - Roblox auto-rejoin for UG Phone (Android) + Termux.

Single-file CLI tool:
  * detects every installed clone matching the "com.roblox." prefix
  * keeps N instances alive in parallel behind one global launch gate
  * root: process / foreground checks, force-stop, screencap,
    .ROBLOSECURITY injection into each clone's WebView cookie DB
  * non-root: Roblox presence API (with a cookie) or timed rejoin
  * local-only heartbeat receiver (127.0.0.1) fed by a generated Lua script
  * RAM / CPU friendly: shared shell snapshots, one time grid, backoff
  * Discord webhook with per-event toggles and periodic status reports

Data lives in /sdcard/Download/RejoinTool/ (falls back to ~/RejoinTool).
Use only on your own accounts and your own devices.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

try:
    import requests  # type: ignore
except Exception:  # pip install requests
    requests = None

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich import box

APP_NAME = "GARNT TOOL REJOIN"
APP_SUBTITLE = "ควายเผือก rejoin"
PREFIX = "com.roblox."
VERSION = "1.0"
AUTH_URL = "https://users.roblox.com/v1/users/authenticated"
PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"

LOGO = r"""
 ██████╗  █████╗ ██████╗ ███╗   ██╗████████╗
██╔════╝ ██╔══██╗██╔══██╗████╗  ██║╚══██╔══╝
██║  ███╗███████║██████╔╝██╔██╗ ██║   ██║
██║   ██║██╔══██║██╔══██╗██║╚██╗██║   ██║
╚██████╔╝██║  ██║██║  ██║██║ ╚████║   ██║
 ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝
"""

STATES = {
    "idle": "รอเริ่ม", "launching": "กำลังเปิด", "grace": "ผ่อนผัน",
    "in_game": "อยู่ในเกม", "out": "ไม่อยู่ในเกม", "unknown": "ไม่ทราบ",
    "backoff": "รอคิวใหม่", "stopped": "หยุด",
}

# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #


def _writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


@dataclass
class Paths:
    base: str
    config: str
    cookie: str
    logs: str
    screenshots: str
    heartbeat: str


def resolve_paths() -> Paths:
    """Absolute paths only: the tool must work from any cwd."""
    base = ""
    for candidate in ("/sdcard/Download/RejoinTool", "/storage/emulated/0/Download/RejoinTool"):
        if _writable_dir(candidate):
            base = candidate
            break
    if not base:
        base = os.path.join(os.path.expanduser("~"), "RejoinTool")
        os.makedirs(base, exist_ok=True)
    paths = Paths(
        base=base,
        config=os.path.join(base, "config.json"),
        cookie=os.path.join(base, "cookie.txt"),
        logs=os.path.join(base, "logs"),
        screenshots=os.path.join(base, "screenshots"),
        heartbeat=os.path.join(base, "heartbeat"),
    )
    for folder in (paths.logs, paths.screenshots, paths.heartbeat):
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass
    return paths


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

DEFAULTS: Dict[str, Any] = {
    "game": {"raw": "", "uri": "", "place_id": 0, "link_code": "", "kind": ""},
    "instances": [],
    "interval": 30,
    "launch_delay": 15,
    "auto": {
        "start_on_launch": True, "wake_lock": True, "force_stop": True,
        "foreground_check": False, "screenshot_on_crash": True,
        "cookie_login_on_start": False, "presence_check": True,
        "timed_rejoin": False, "timed_rejoin_min": 20, "join_grace": 90,
    },
    "heartbeat": {"enabled": True, "port": 8765, "script_interval": 15, "timeout": 90},
    "performance": {
        "shared_snapshot": True, "snapshot_ttl": 3, "presence_cache_sec": 60,
        "clear_ram": True, "clear_ram_min": 30, "ram_low_mb": 300,
        "ram_min_gap_sec": 60, "am_kill_all": False, "nice": 10, "ui_refresh_sec": 2,
    },
    "webhook": {
        "enabled": False, "url": "",
        "events": {"start": True, "crash": True, "rejoin": True, "stop": True, "status": False},
        "attach_screenshot": False, "periodic_min": 10,
    },
    "backoff": {"base": 10, "cap": 300, "reset_sec": 300},
}


def deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in (overlay or {}).items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("config root must be an object")
            data = loaded
        except Exception:
            backup = "%s.corrupt-%s" % (path, datetime.now().strftime("%Y%m%d-%H%M%S"))
            try:
                shutil.copy2(path, backup)
            except Exception:
                pass
            data = {}
    return deep_merge(copy.deepcopy(DEFAULTS), data)


def save_config(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# logging + secret scrubbing
# --------------------------------------------------------------------------- #

_COOKIE_KV = re.compile(r"(?i)(\.ROBLOSECURITY\s*=\s*)[^\s\"'<>;,]+")
_COOKIE_WARN = re.compile(r"(?i)(_\|WARNING:-DO-NOT-SHARE-THIS)[^\s\"'<>]*")
_WEBHOOK = re.compile(r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/[^\s\"'<>]+")


def scrub(text: Any) -> str:
    """Strip cookies and webhook tokens from anything logged or displayed."""
    value = str(text)
    value = _WEBHOOK.sub("https://discord.com/api/webhooks/***", value)
    value = _COOKIE_KV.sub(r"\1***", value)
    return _COOKIE_WARN.sub("***", value)


def mask_cookie(raw: str, tail: int = 4) -> str:
    token = normalize_cookie(raw)
    if not token:
        return "(ว่าง)"
    if len(token) <= tail:
        return "*" * len(token)
    return "****" + token[-tail:] + " (%d ตัวอักษร)" % len(token)


class ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def setup_logger(log_dir: str) -> logging.Logger:
    logger = logging.getLogger("rejoin")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    os.makedirs(log_dir, exist_ok=True)
    handler = TimedRotatingFileHandler(
        os.path.join(log_dir, "rejoin.log"), when="midnight", backupCount=7, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
    handler.addFilter(ScrubFilter())
    logger.addHandler(handler)
    return logger


# --------------------------------------------------------------------------- #
# shell helpers
# --------------------------------------------------------------------------- #


def run_shell(cmd: str, timeout: int = 20, root: bool = False) -> Tuple[int, str, str]:
    argv = ["su", "-c", cmd] if root else ["sh", "-c", cmd]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", "คำสั่งไม่พบในระบบ"
    except subprocess.TimeoutExpired:
        return 124, "", "หมดเวลา"
    except Exception as exc:
        return 1, "", str(exc)


def detect_root() -> bool:
    code, out, err = run_shell("su -c id", timeout=10)
    return code == 0 and "uid=0" in (out or err)


def read_memory() -> Tuple[int, int]:
    """(available MB, total MB) from /proc/meminfo."""
    try:
        info: Dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.strip().split()
                if parts:
                    info[key.strip()] = int(parts[0])
        return info.get("MemAvailable", info.get("MemFree", 0)) // 1024, info.get("MemTotal", 0) // 1024
    except Exception:
        return 0, 0


def clear_ram(is_root: bool) -> Tuple[bool, int, int]:
    before, _ = read_memory()
    ok = False
    if is_root:
        code, _, _ = run_shell("sync; echo 3 > /proc/sys/vm/drop_caches", root=True)
        ok = code == 0
    after, _ = read_memory()
    return ok, before, after


def prune_screenshots(folder: str, keep: int = 20) -> None:
    try:
        files = [os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(".png")]
        files.sort(key=os.path.getmtime, reverse=True)
        for stale in files[keep:]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except Exception:
        pass


def take_screenshot(is_root: bool, folder: str, keep: int = 20) -> Optional[str]:
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "shot-%s.png" % datetime.now().strftime("%Y%m%d-%H%M%S"))
        run_shell("screencap -p %s" % shlex.quote(path), root=is_root, timeout=25)
        prune_screenshots(folder, keep)
        return path if os.path.exists(path) else None
    except Exception:
        return None


def wake_lock(is_root: bool) -> None:
    run_shell("termux-wake-lock", timeout=10)
    if is_root:
        run_shell("echo garnt-tool > /sys/power/wake_lock", root=True)


def wake_unlock(is_root: bool) -> None:
    run_shell("termux-wake-unlock", timeout=10)
    if is_root:
        run_shell("echo garnt-tool > /sys/power/wake_unlock", root=True)


# --------------------------------------------------------------------------- #
# game link parsing
# --------------------------------------------------------------------------- #

_PLACE_PATH = re.compile(r"/games/(\d{3,})", re.I)
_ROBLOX_HOST = re.compile(r"(^|\.)roblox\.com$", re.I)


def _first_int(values: Optional[List[str]]) -> int:
    if not values:
        return 0
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return 0


def parse_game_link(raw: str) -> Dict[str, Any]:
    """Place ID, game URL, private-server link, share link or roblox:// link."""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("ลิงก์ว่างเปล่า")

    if text.isdigit():
        place = int(text)
        return {"raw": text, "kind": "place_id", "place_id": place, "link_code": "",
                "uri": "https://www.roblox.com/games/%d/" % place}

    if text.startswith("roblox://"):
        tail = text[len("roblox://"):].lstrip("/")
        if "?" in tail:
            tail = tail.split("?", 1)[1]
        query = parse_qs(tail)
        place = _first_int(query.get("placeID") or query.get("placeId"))
        return {"raw": text, "kind": "roblox_scheme", "place_id": place,
                "link_code": (query.get("linkCode") or [""])[0], "uri": text}

    candidate = text if "://" in text else "https://" + text.lstrip("/")
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host and not _ROBLOX_HOST.search(host):
        raise ValueError("ลิงก์นี้ไม่ใช่ลิงก์ของ roblox.com")

    query = parse_qs(parsed.query)
    place = _first_int(query.get("placeId")) or _first_int(query.get("placeID"))
    if not place:
        match = _PLACE_PATH.search(parsed.path)
        place = int(match.group(1)) if match else 0
    code = (query.get("privateServerLinkCode") or [""])[0] or (query.get("linkCode") or [""])[0]

    if place and code:
        kind, uri = "private_server", "roblox://placeID=%d&linkCode=%s" % (place, code)
    elif place:
        kind, uri = "game", "https://www.roblox.com/games/%d/" % place
    else:
        kind, uri = "share", candidate  # the Roblox app resolves share codes itself
    return {"raw": text, "kind": kind, "place_id": place or 0, "link_code": code, "uri": uri}


# --------------------------------------------------------------------------- #
# packages, cookies, Roblox API
# --------------------------------------------------------------------------- #


def scan_packages(prefix: str = PREFIX) -> List[str]:
    code, out, _ = run_shell("pm list packages", timeout=30)
    found = set()
    if code == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                name = line.split(":", 1)[1].strip()
                if name.startswith(prefix):
                    found.add(name)
    return sorted(found)


def normalize_cookie(raw: str) -> str:
    value = (raw or "").strip().strip('"').strip("'").strip()
    if value.lower().startswith(".roblosecurity="):
        value = value.split("=", 1)[1]
    return value.strip().strip('"').strip("'")


def load_cookies(path: str) -> List[str]:
    tokens: List[str] = []
    if not os.path.exists(path):
        return tokens
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            token = normalize_cookie(line)
            if token:
                tokens.append(token)
    return tokens


def validate_cookie(cookie: str) -> Optional[Dict[str, Any]]:
    if requests is None:
        return None
    try:
        resp = requests.get(AUTH_URL, cookies={".ROBLOSECURITY": normalize_cookie(cookie)}, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        return None
    return None


def fetch_presence(cookie: str, user_id: int) -> Optional[bool]:
    if requests is None or not user_id:
        return None
    try:
        resp = requests.post(
            PRESENCE_URL, json={"userIds": [user_id]},
            cookies={".ROBLOSECURITY": normalize_cookie(cookie)}, timeout=12,
        )
        if resp.status_code != 200:
            return None
        presences = resp.json().get("userPresences") or []
        if not presences:
            return None
        return int(presences[0].get("userPresenceType", 0)) == 2
    except Exception:
        return None


def is_valid_webhook(url: str) -> bool:
    return bool(re.match(
        r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$",
        (url or "").strip(),
    ))


# --------------------------------------------------------------------------- #
# cookie injection (root)
# --------------------------------------------------------------------------- #

WEBVIEW_DBS = ("app_webview/Default/Cookies", "app_webview/Cookies",
               "app_webview/Default/Network/Cookies")
_CHROME_EPOCH = 11644473600


def _chrome_time(offset: int = 0) -> int:
    return int((time.time() + offset + _CHROME_EPOCH) * 1_000_000)


def inject_cookie_into_db(db_path: str, cookie: str) -> bool:
    """Write .ROBLOSECURITY into a Chromium/WebView cookie DB using its real schema."""
    token = normalize_cookie(cookie)
    if not token:
        return False
    values = {
        "host_key": ".roblox.com", "name": ".ROBLOSECURITY", "value": token, "path": "/",
        "creation_utc": _chrome_time(), "expires_utc": _chrome_time(60 * 60 * 24 * 30),
        "last_access_utc": _chrome_time(), "last_update_utc": _chrome_time(),
        "has_expires": 1, "is_persistent": 1, "is_secure": 1, "is_httponly": 1,
        "samesite": -1, "priority": 1, "encrypted_value": b"", "sync": 0,
        "source_scheme": 2, "source_port": 443, "has_cross_site_ancestor": 0,
        "source_type": 0, "is_same_party": 0, "is_monolithic": 0,
    }
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(cookies)")
        columns = cursor.fetchall()
        if not columns:
            return False
        cols: List[str] = []
        vals: List[Any] = []
        for col in columns:
            name, ctype, notnull, default, pk = col[1], (col[2] or "").upper(), bool(col[3]), col[4], bool(col[5])
            if pk:
                continue
            if name in values:
                cols.append(name)
                vals.append(values[name])
            elif notnull and default is None:
                cols.append(name)
                vals.append(b"" if "BLOB" in ctype else "" if "INT" not in ctype else 0)
        cursor.execute(
            "SELECT rowid FROM cookies WHERE host_key=? AND name=? AND path=?",
            (".roblox.com", ".ROBLOSECURITY", "/"),
        )
        row = cursor.fetchone()
        if row:
            updates = [(n, v) for n, v in zip(cols, vals) if n not in ("host_key", "name", "path")]
            cursor.execute(
                "UPDATE cookies SET %s WHERE rowid=?" % ", ".join("%s=?" % n for n, _ in updates),
                [v for _, v in updates] + [row[0]],
            )
        else:
            cursor.execute(
                "INSERT INTO cookies (%s) VALUES (%s)"
                % (", ".join(cols), ", ".join("?" for _ in cols)),
                vals,
            )
        conn.commit()
        return True
    finally:
        conn.close()


def find_webview_cookie_db(package: str) -> Optional[str]:
    for relative in WEBVIEW_DBS:
        path = "/data/data/%s/%s" % (package, relative)
        code, out, _ = run_shell("ls -l %s" % shlex.quote(path), root=True, timeout=10)
        if code == 0 and "Cookies" in out:
            return path
    return None


def inject_cookie_package(package: str, cookie: str, logger: logging.Logger) -> Tuple[bool, str]:
    """Root flow: force-stop, copy DB out, edit, copy back with owner/mode/SELinux."""
    remote = find_webview_cookie_db(package)
    if not remote:
        return False, "ไม่พบฐานข้อมูลคุกกี้"
    run_shell("am force-stop %s" % package, root=True, timeout=20)
    _, uid_out, _ = run_shell("stat -c %%u /data/data/%s" % package, root=True, timeout=10)
    uid = uid_out.strip() or "0"
    tmp_dir = tempfile.mkdtemp(prefix="cookiejail-")
    local_db = os.path.join(tmp_dir, "Cookies")
    try:
        run_shell("cp %s %s" % (shlex.quote(remote), shlex.quote(local_db)), root=True, timeout=25)
        if not os.path.exists(local_db):
            return False, "คัดลอกฐานข้อมูลออกมาไม่ได้"
        os.chmod(local_db, 0o600)
        if not inject_cookie_into_db(local_db, cookie):
            return False, "โครงสร้างตาราง cookies ไม่รองรับ"
        run_shell("cp %s %s" % (shlex.quote(local_db), shlex.quote(remote)), root=True, timeout=25)
        run_shell("chown %s:%s %s && chmod 660 %s" % (uid, uid, shlex.quote(remote), shlex.quote(remote)),
                  root=True, timeout=15)
        run_shell("restorecon -R /data/data/%s/app_webview" % package, root=True, timeout=15)
        logger.info("inject cookie ok: %s", package)
        return True, "เขียนคุกกี้สำเร็จ"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# heartbeat receiver (localhost only)
# --------------------------------------------------------------------------- #


class HeartbeatStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}

    def update(self, instance_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            record = self._data.setdefault(instance_id, {})
            record.update(payload)
            record["ts"] = time.time()

    def get(self, instance_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._data.get(instance_id)
            return dict(record) if record else None


class HeartbeatHandler(BaseHTTPRequestHandler):
    store: HeartbeatStore = HeartbeatStore()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/ping":
            self._reply(200, "pong")
            return
        if parsed.path == "/hb":
            query = parse_qs(parsed.query)
            instance_id = (query.get("id") or [""])[0].strip()
            if not instance_id:
                self._reply(400, "missing id")
                return
            self.store.update(instance_id, {
                "user": (query.get("user") or [""])[0],
                "uid": _first_int(query.get("uid")),
                "place": _first_int(query.get("place")),
                "job": (query.get("job") or [""])[0],
            })
            self._reply(200, "ok")
            return
        self._reply(404, "not found")

    def _reply(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        return


class HeartbeatServer:
    def __init__(self, port: int, store: HeartbeatStore) -> None:
        self.port = port
        self.store = store
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if self.httpd is not None:
            return True
        handler = type("BoundHandler", (HeartbeatHandler,), {"store": self.store})
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        except OSError:
            self.httpd = None
            return False
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
        self.httpd = None
        self.thread = None


LUA_TEMPLATE = """-- GARNT TOOL REJOIN - heartbeat for {package}
-- Generated {stamp}. Run inside Roblox with your own executor (ban risk is yours).

if _G.GARNT_HEARTBEAT_RUNNING then
    return
end
_G.GARNT_HEARTBEAT_RUNNING = true

local CONFIG = {{
    id = "{package}",
    port = {port},
    interval = {interval},
    alive_window = 5,
}}

local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local CoreGui = game:GetService("CoreGui")

local function now()
    local ok, value = pcall(function()
        return tick()
    end)
    if ok then
        return value
    end
    return os.time()
end

local function wait_step(seconds)
    if task and task.wait then
        task.wait(seconds)
    else
        wait(seconds)
    end
end

local last_heartbeat = now()
RunService.Heartbeat:Connect(function()
    last_heartbeat = now()
end)

local function url_encode(value)
    local text = tostring(value or ""):gsub("\\n", " ")
    return (text:gsub("[^%w%-%._~]", function(char)
        return string.format("%%%02X", string.byte(char))
    end))
end

local function http_get(url)
    local syn = rawget(_G, "syn") or (getgenv and getgenv().syn)
    if syn and type(syn.request) == "function" then
        local ok, res = pcall(syn.request, {{ Url = url, Method = "GET" }})
        if ok and type(res) == "table" and (res.StatusCode or 200) < 400 then
            return true
        end
    end
    local request = rawget(_G, "request")
    if type(request) == "function" then
        local ok, res = pcall(request, {{ Url = url, Method = "GET" }})
        if ok and type(res) == "table" and (res.StatusCode or 200) < 400 then
            return true
        end
    end
    local http_request = rawget(_G, "http_request")
    if type(http_request) == "function" then
        local ok, res = pcall(http_request, {{ Url = url }})
        if ok and type(res) == "table" and (res.StatusCode or 200) < 400 then
            return true
        end
    end
    return (pcall(function()
        game:HttpGet(url)
    end))
end

local function has_error_prompt()
    local ok, found = pcall(function()
        for _, node in ipairs(CoreGui:GetDescendants()) do
            if node:IsA("TextLabel") and node.Visible then
                local text = string.lower(tostring(node.Text or ""))
                if text ~= "" and (
                    string.find(text, "disconnected", 1, true)
                    or string.find(text, "lost connection", 1, true)
                    or string.find(text, "connection lost", 1, true)
                    or string.find(text, "rejoin", 1, true)
                ) then
                    return true
                end
            end
        end
        return false
    end)
    return ok and found
end

local function build_url()
    local player = Players.LocalPlayer
    local place_id = 0
    local job_id = ""
    pcall(function()
        place_id = game.PlaceId
    end)
    pcall(function()
        job_id = tostring(game.JobId or "")
    end)
    return string.format(
        "http://127.0.0.1:%d/hb?id=%s&user=%s&uid=%d&place=%d&job=%s",
        CONFIG.port,
        url_encode(CONFIG.id),
        url_encode(player and player.Name or "unknown"),
        tonumber(player and player.UserId or 0) or 0,
        tonumber(place_id) or 0,
        url_encode(job_id)
    )
end

local function main_loop()
    while true do
        local player = Players.LocalPlayer
        local loaded = false
        pcall(function()
            loaded = game:IsLoaded()
        end)
        local engine_alive = (now() - last_heartbeat) <= CONFIG.alive_window
        if player ~= nil and loaded and engine_alive and not has_error_prompt() then
            http_get(build_url())
        end
        wait_step(CONFIG.interval)
    end
end

if task and task.spawn then
    task.spawn(main_loop)
else
    spawn(main_loop)
end
"""


def render_lua(package: str, port: int, interval: int) -> str:
    return LUA_TEMPLATE.format(
        package=package, port=int(port), interval=int(interval),
        stamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def write_lua_files(packages: List[str], folder: str, port: int, interval: int) -> List[str]:
    os.makedirs(folder, exist_ok=True)
    written = []
    for package in packages:
        path = os.path.join(folder, "%s.lua" % package)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render_lua(package, port, interval))
        written.append(path)
    return written


# --------------------------------------------------------------------------- #
# discord webhook
# --------------------------------------------------------------------------- #


class Discord:
    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger) -> None:
        self.cfg = cfg
        self.logger = logger
        self._lock = threading.Lock()

    def active(self) -> bool:
        return bool(self.cfg["webhook"].get("enabled")) and is_valid_webhook(
            self.cfg["webhook"].get("url", "")
        )

    def send(self, event: str, content: str, screenshot: Optional[str] = None,
             sync: bool = False) -> bool:
        if not self.active():
            return False
        if not self.cfg["webhook"]["events"].get(event, False):
            return False
        if sync:
            return self._post(content, screenshot)
        threading.Thread(target=self._post, args=(content, screenshot), daemon=True).start()
        return True

    def _post(self, content: str, screenshot: Optional[str]) -> bool:
        if requests is None:
            return False
        url = self.cfg["webhook"]["url"]
        payload = {"username": APP_NAME, "content": scrub(content)[:1900]}
        with self._lock:
            for _ in range(4):
                handle = None
                try:
                    if screenshot and os.path.exists(screenshot):
                        handle = open(screenshot, "rb")
                        resp = requests.post(
                            url, data=payload,
                            files={"file": (os.path.basename(screenshot), handle)}, timeout=20,
                        )
                    else:
                        resp = requests.post(url, json=payload, timeout=15)
                except Exception as exc:
                    self.logger.warning("webhook error: %s", exc)
                    return False
                finally:
                    if handle:
                        handle.close()
                if resp.status_code in (200, 204):
                    return True
                if resp.status_code == 429:
                    delay = 1.0
                    try:
                        delay = float(resp.json().get("retry_after", 1))
                    except Exception:
                        pass
                    time.sleep(min(6.0, max(0.5, delay)))
                    continue
                self.logger.warning("webhook status %s", resp.status_code)
                return False
        return False


# --------------------------------------------------------------------------- #
# performance: one shared shell snapshot per cycle
# --------------------------------------------------------------------------- #


class ShellSnapshot:
    def __init__(self, ttl: int = 3) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._ts = 0.0
        self._processes: List[str] = []
        self._foreground = ""

    def collect(self) -> None:
        code, out, _ = run_shell("ps -A", timeout=15)
        names: List[str] = []
        if code == 0:
            for line in out.splitlines()[1:]:
                parts = line.split()
                if parts:
                    names.append(parts[-1])
        foreground = ""
        code, out, _ = run_shell(
            "dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity'", timeout=15
        )
        if code == 0 and out:
            for line in out.splitlines():
                if "{" in line and "/" in line:
                    foreground = line.split("{")[-1].split()[0].split("/")[0]
                    break
        with self._lock:
            self._processes = names
            self._foreground = foreground
            self._ts = time.time()

    def refresh(self, force: bool = False) -> None:
        if not force and time.time() - self._ts < self.ttl:
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            self.collect()
        finally:
            self._lock.release()

    def has_process(self, package: str) -> bool:
        with self._lock:
            return any(package in name for name in self._processes)

    def in_foreground(self, package: str) -> bool:
        with self._lock:
            return self._foreground == package


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #


def backoff_delay(fails: int, base: int = 10, cap: int = 300) -> int:
    if fails <= 0:
        return 0
    return min(cap, base * (2 ** min(fails - 1, 10)))


class LaunchGate:
    """Any two launches are at least `delay` seconds apart, globally."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.next_at = 0.0
        self._lock = threading.Lock()

    def reserve(self, now: Optional[float] = None) -> float:
        moment = time.time() if now is None else now
        with self._lock:
            slot = max(moment, self.next_at)
            self.next_at = slot + self.delay
            return slot


@dataclass
class Instance:
    package: str
    account: str = "-"
    state: str = "idle"
    fails: int = 0
    rejoins: int = 0
    detail: str = ""
    cookie: str = ""
    user_id: int = 0
    launched_at: float = 0.0
    last_hb: float = 0.0
    ever_hb: bool = False
    cooldown_until: float = 0.0
    next_check: float = 0.0
    stable_since: float = 0.0
    next_timed: float = 0.0


def fmt_uptime(seconds: float) -> str:
    total = int(max(0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d ชม. %d นาที" % (hours, minutes)
    if minutes:
        return "%d นาที %d วิ" % (minutes, secs)
    return "%d วิ" % secs


def fmt_next(inst: Instance) -> str:
    remaining = max(inst.next_check, inst.cooldown_until) - time.time()
    return "-" if remaining <= 0 else "%d วิ" % int(remaining)


class Engine:
    def __init__(self, cfg: Dict[str, Any], instances: List[Instance], logger: logging.Logger,
                 is_root: bool, paths: Paths, discord: Discord, store: HeartbeatStore) -> None:
        self.cfg = cfg
        self.instances = instances
        self.logger = logger
        self.root = is_root
        self.paths = paths
        self.discord = discord
        self.store = store
        self.stopped = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.events: deque = deque(maxlen=7)
        self.started_at = time.time()
        self.launch_gate = LaunchGate(float(cfg.get("launch_delay", 15) or 15))
        self.snapshot = ShellSnapshot(int(cfg["performance"].get("snapshot_ttl", 3) or 3))
        self.state_lock = threading.Lock()
        self._next_cycle = time.time()
        self._next_ram = time.time() + 60
        self._last_ram_clear = 0.0
        self._next_status = time.time() + 60 * float(cfg["webhook"].get("periodic_min", 0) or 0)
        self._presence: Dict[int, Tuple[float, Optional[bool]]] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.stopped.clear()
        self.started_at = time.time()
        self._next_cycle = time.time()
        for inst in self.instances:
            inst.next_check = 0.0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.log_event("เริ่มตรวจสอบ %d อินสแตนซ์" % len(self.instances))
        self.discord.send("start", "**%s** เริ่มทำงาน\nอินสแตนซ์: %d\nโหมด: %s" % (
            APP_NAME, len(self.instances), "root" if self.root else "non-root"))

    def stop(self) -> None:
        self.stopped.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        free, total = read_memory()
        self.discord.send("stop", "**%s** หยุดทำงาน\nuptime: %s\nแรมว่าง: %d / %d MB" % (
            APP_NAME, fmt_uptime(time.time() - self.started_at), free, total), sync=True)

    def log_event(self, message: str) -> None:
        with self.state_lock:
            self.events.appendleft("%s  %s" % (datetime.now().strftime("%H:%M:%S"), scrub(message)))
        self.logger.info(message)

    # -- loop --------------------------------------------------------------

    def _run(self) -> None:
        while not self.stopped.is_set():
            now = time.time()
            if now >= self._next_cycle:
                interval = max(5, int(self.cfg.get("interval", 30) or 30))
                try:
                    self._cycle(interval)
                except Exception as exc:
                    self.log_event("รอบตรวจผิดพลาด: %s" % exc)
                    self.logger.exception("cycle failed")
                self._next_cycle = max(now + interval, self._next_cycle + interval)
            try:
                self._maybe_clear_ram(now)
                self._maybe_status_report(now)
            except Exception:
                pass
            time.sleep(0.4)

    def _cycle(self, grid: int) -> None:
        if self.cfg["performance"].get("shared_snapshot", True):
            self.snapshot.refresh()
        for inst in self.instances:
            if inst.state == "stopped":
                continue
            now = time.time()
            if now < inst.cooldown_until:
                inst.next_check = inst.cooldown_until
                continue
            inst.next_check = now + grid
            verdict, detail = self._evaluate(inst, now)
            if verdict == "ok":
                self._mark_ok(inst, detail)
            elif verdict == "grace":
                inst.state, inst.detail = "grace", detail
            elif verdict == "unknown":
                inst.state, inst.detail = "unknown", detail
                self._maybe_timed_rejoin(inst, now)
            else:
                self._mark_failure(inst, detail)

    def _evaluate(self, inst: Instance, now: float) -> Tuple[str, str]:
        auto, hb = self.cfg["auto"], self.cfg["heartbeat"]
        grace = float(auto.get("join_grace", 90) or 90)
        if hb.get("enabled"):
            record = self.store.get(inst.package)
            if record:
                inst.last_hb = float(record.get("ts", 0))
                inst.ever_hb = True
                if record.get("user"):
                    inst.account = str(record.get("user"))
            if inst.ever_hb:
                timeout = float(hb.get("timeout", 90) or 90)
                if now - inst.last_hb > timeout:
                    return "fail", "heartbeat เงียบเกิน %d วิ" % int(timeout)
                if inst.last_hb < inst.launched_at and now - inst.launched_at > grace:
                    return "fail", "ไม่ได้ heartbeat หลังเปิดใหม่"
                return "ok", "heartbeat ปกติ"
        in_game, detail = self._probe(inst)
        if in_game is True:
            return "ok", detail
        if in_game is None:
            return "unknown", detail
        if now - inst.launched_at < grace:
            return "grace", "รอเข้าเกม (%d วิ)" % int(grace - (now - inst.launched_at))
        return "fail", detail

    def _probe(self, inst: Instance) -> Tuple[Optional[bool], str]:
        if self.root:
            self.snapshot.refresh()
            if not self.snapshot.has_process(inst.package):
                return False, "ไม่พบโปรเซส"
            if self.cfg["auto"].get("foreground_check") and not self.snapshot.in_foreground(inst.package):
                return False, "ไม่ได้อยู่หน้าจอ"
            return True, "โปรเซสทำงาน"
        if inst.cookie and self.cfg["auto"].get("presence_check", True):
            present = self._presence_of(inst)
            if present is None:
                return None, "presence ใช้งานไม่ได้"
            return present, "presence API"
        return None, "ไม่มีคุกกี้"

    def _presence_of(self, inst: Instance) -> Optional[bool]:
        ttl = int(self.cfg["performance"].get("presence_cache_sec", 60) or 60)
        cached = self._presence.get(inst.user_id or 0)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]
        value = fetch_presence(inst.cookie, inst.user_id)
        self._presence[inst.user_id or 0] = (time.time(), value)
        return value

    def _mark_ok(self, inst: Instance, detail: str) -> None:
        now = time.time()
        inst.state, inst.detail = "in_game", detail
        if not inst.stable_since:
            inst.stable_since = now
        if now - inst.stable_since >= float(self.cfg["backoff"].get("reset_sec", 300) or 300):
            if inst.fails:
                self.log_event("%s เสถียรต่อเนื่อง — รีเซ็ตตัวนับ" % inst.package)
            inst.fails = 0

    def _mark_failure(self, inst: Instance, detail: str) -> None:
        now = time.time()
        inst.fails += 1
        inst.stable_since = 0.0
        wait = backoff_delay(inst.fails, int(self.cfg["backoff"].get("base", 10) or 10),
                             int(self.cfg["backoff"].get("cap", 300) or 300))
        inst.state = "backoff"
        inst.detail = "%s · รอ %d วิ" % (detail, wait)
        inst.cooldown_until = now + wait
        self.log_event("%s ล้มเหลว (%s) → รอ %d วิ" % (inst.package, detail, wait))
        self.discord.send("crash", "**หลุด / ไม่ตอบสนอง**\nแพ็กเกจ: %s\nสาเหตุ: %s\nพลาดสะสม: %d"
                          % (inst.package, detail, inst.fails),
                          screenshot=self._crash_shot(inst))
        self.request_relaunch(inst, "rejoin")

    def _crash_shot(self, inst: Instance) -> Optional[str]:
        if not (self.root and self.cfg["auto"].get("screenshot_on_crash")):
            return None
        if not self.cfg["webhook"].get("attach_screenshot"):
            return None
        return take_screenshot(self.root, self.paths.screenshots)

    def _maybe_timed_rejoin(self, inst: Instance, now: float) -> None:
        auto = self.cfg["auto"]
        if not auto.get("timed_rejoin"):
            return
        minutes = float(auto.get("timed_rejoin_min", 20) or 20)
        if now >= inst.next_timed:
            inst.next_timed = now + minutes * 60
            inst.detail = "rejoin ตามเวลา (%d นาที)" % int(minutes)
            self.request_relaunch(inst, "timed")

    def request_relaunch(self, inst: Instance, reason: str) -> None:
        threading.Thread(target=self._relaunch, args=(inst, reason), daemon=True).start()

    def _relaunch(self, inst: Instance, reason: str) -> None:
        delay = self.launch_gate.reserve() - time.time()
        if delay > 0:
            inst.state = "backoff"
            inst.detail = "รอคิวเปิดอีก %d วิ" % int(delay)
            time.sleep(delay)
        uri = self.cfg["game"].get("uri", "")
        if not uri:
            inst.state, inst.detail = "idle", "ยังไม่ได้ตั้งลิงก์เกม"
            return
        inst.state, inst.detail = "launching", "กำลังเปิดแอป…"
        if self.root and self.cfg["auto"].get("force_stop"):
            run_shell("am force-stop %s" % inst.package, root=True, timeout=20)
        ok, method = self._am_start(inst.package, uri), "am start"
        if not ok:
            ok, method = self._monkey(inst.package), "monkey"
        inst.launched_at = time.time()
        inst.stable_since = 0.0
        if not ok:
            inst.state, inst.detail = "out", "เปิดแอปไม่สำเร็จ"
            self.log_event("%s เปิดแอปไม่สำเร็จ" % inst.package)
            return
        if reason in ("rejoin", "timed"):
            inst.rejoins += 1
        inst.state, inst.detail = "grace", "เปิดด้วย %s" % method
        self.log_event("%s เปิดใหม่ด้วย %s (%s)" % (
            inst.package, method, "รีจอยน์" if reason in ("rejoin", "timed") else "เริ่มต้น"))
        if reason in ("rejoin", "timed"):
            self.discord.send("rejoin", "**รีจอยน์**\nแพ็กเกจ: %s\nวิธี: %s\nครั้งที่: %d"
                              % (inst.package, method, inst.rejoins))

    def _am_start(self, package: str, uri: str) -> bool:
        cmd = "am start -a android.intent.action.VIEW -d %s -p %s" % (
            shlex.quote(uri), shlex.quote(package))
        if run_shell(cmd, timeout=25, root=self.root)[0] == 0:
            return True
        return run_shell(cmd, timeout=25, root=False)[0] == 0

    def _monkey(self, package: str) -> bool:
        cmd = "monkey -p %s -c android.intent.category.LAUNCHER 1" % package
        return run_shell(cmd, timeout=25, root=self.root)[0] == 0

    # -- periodic ----------------------------------------------------------

    def _maybe_clear_ram(self, now: float) -> None:
        perf = self.cfg["performance"]
        if not perf.get("clear_ram") or now < self._next_ram:
            return
        self._next_ram = now + 60
        free, _ = read_memory()
        low = bool(free) and free < int(perf.get("ram_low_mb", 300) or 300)
        due = now - self._last_ram_clear >= float(perf.get("clear_ram_min", 30) or 30) * 60
        gap_ok = now - self._last_ram_clear >= float(perf.get("ram_min_gap_sec", 60) or 60)
        if (low or due) and gap_ok:
            ok, before, after = clear_ram(self.root)
            self._last_ram_clear = now
            self.log_event("ล้างแรม: %d → %d MB (%s)" % (
                before, after, "สำเร็จ" if ok else "ต้องใช้ root"))

    def _maybe_status_report(self, now: float) -> None:
        minutes = float(self.cfg["webhook"].get("periodic_min", 0) or 0)
        if minutes <= 0 or not self.discord.active() or now < self._next_status:
            return
        self._next_status = now + minutes * 60
        free, total = read_memory()
        lines = ["**รายงานสถานะ %s**" % APP_NAME,
                 "uptime: %s · แรมว่าง %d / %d MB" % (fmt_uptime(now - self.started_at), free, total)]
        for inst in self.instances:
            lines.append("%s %s · %s · พลาด %d · รีจอยน์ %d" % (
                inst.package, STATES.get(inst.state, inst.state), inst.account,
                inst.fails, inst.rejoins))
        shot = take_screenshot(self.root, self.paths.screenshots) if self.root else None
        self.discord.send("status", "\n".join(lines), screenshot=shot)


def build_dashboard(engine: Engine) -> Any:
    free, total = read_memory()
    header = Text.assemble(
        (APP_NAME, "bold green"), ("  ·  %s" % APP_SUBTITLE, "dim"), ("\n", ""),
        ("uptime ", "dim"), (fmt_uptime(time.time() - engine.started_at), "white"),
        ("   แรมว่าง ", "dim"), ("%d / %d MB" % (free, total), "white"),
    )
    table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    for column, kwargs in (
        ("#", {"justify": "right", "style": "dim", "width": 3}),
        ("แพ็กเกจ", {"style": "cyan", "no_wrap": True}),
        ("บัญชี", {}), ("สถานะ", {}),
        ("พลาด", {"justify": "right", "width": 5}),
        ("รีจอยน์", {"justify": "right", "width": 7}),
        ("ตรวจถัดไป", {"justify": "right", "width": 10}),
        ("รายละเอียด", {"overflow": "fold"}),
    ):
        table.add_column(column, **kwargs)
    colors = {"in_game": "green", "grace": "yellow", "backoff": "red", "out": "red",
              "unknown": "magenta", "launching": "cyan", "idle": "dim", "stopped": "dim"}
    for index, inst in enumerate(engine.instances, start=1):
        table.add_row(
            str(index), inst.package, inst.account,
            Text(STATES.get(inst.state, inst.state), style=colors.get(inst.state, "white")),
            str(inst.fails), str(inst.rejoins), fmt_next(inst), inst.detail,
        )
    events = Table(box=box.SIMPLE, expand=True, pad_edge=False, show_header=False)
    events.add_column("event", style="dim", overflow="fold")
    with engine.state_lock:
        snapshot = list(engine.events)
    for line in snapshot or ["ยังไม่มีเหตุการณ์"]:
        events.add_row(line)
    return Panel(
        Group(header, Rule(style="grey27"), table, Rule("เหตุการณ์ล่าสุด", style="grey27"), events),
        title="[bold green]สถานะสด[/bold green]",
        subtitle="[dim]Ctrl+C เพื่อหยุดและกลับเมนู[/dim]",
        border_style="green",
    )


# --------------------------------------------------------------------------- #
# app + menus
# --------------------------------------------------------------------------- #


class App:
    def __init__(self) -> None:
        self.console = Console()
        self.paths = resolve_paths()
        self.cfg = load_config(self.paths.config)
        self.logger = setup_logger(self.paths.logs)
        self.root = detect_root()
        self.discord = Discord(self.cfg, self.logger)
        self.store = HeartbeatStore()
        self.hb_server = HeartbeatServer(
            int(self.cfg["heartbeat"].get("port", 8765) or 8765), self.store)
        self.instances: List[Instance] = []
        self.engine: Optional[Engine] = None
        try:
            os.nice(int(self.cfg["performance"].get("nice", 10) or 10))
        except Exception:
            pass
        self.save()

    def save(self) -> None:
        save_config(self.paths.config, self.cfg)

    def load_instances(self) -> None:
        self.instances = [Instance(package=name) for name in (self.cfg.get("instances") or [])]

    def ensure_instances(self) -> bool:
        if self.instances:
            return True
        packages = self.cfg.get("instances") or []
        if not packages:
            self.console.print("[yellow]ยังไม่ได้เลือกอินสแตนซ์ — กำลังสแกนแพ็กเกจ…[/yellow]")
            packages = self.menu_scan(silent=True)
            if not packages:
                return False
        self.cfg["instances"] = packages
        self.save()
        self.load_instances()
        return bool(self.instances)

    def ensure_game(self) -> bool:
        if self.cfg["game"].get("uri"):
            return True
        self.menu_game_link()
        return bool(self.cfg["game"].get("uri"))

    def banner(self) -> None:
        self.console.clear()
        self.console.print(LOGO, style="bold green", highlight=False)
        self.console.print("        [bold white]%s[/bold white]\n" % APP_SUBTITLE)
        free, total = read_memory()
        self.console.print("[dim]UG Phone · Termux · Python %s · โหมด %s · แรมว่าง %d/%d MB · %s[/dim]" % (
            ".".join(str(part) for part in sys.version_info[:2]),
            "root" if self.root else "non-root", free, total, self.paths.base))
        self.console.print()

    def menu_panel(self, rows: List[Tuple[str, str]], title: str) -> None:
        table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
        table.add_column("no", style="bold green", width=3)
        table.add_column("item")
        for number, label in rows:
            table.add_row(number, label)
        self.console.print(Panel(table, title=title, title_align="left", border_style="grey30"))

    def main_menu(self) -> None:
        rows = [
            ("1", "เริ่ม auto-rejoin"), ("2", "แสดงอินสแตนซ์ / สถานะ"),
            ("3", "ตั้งค่า (ลิงก์เกม, รอบตรวจ, ดีเลย์)"), ("4", "สแกนแพ็กเกจ"),
            ("5", "ล็อกอินคุกกี้ (ไม่บังคับ)"), ("6", "ตั้งค่าอัตโนมัติ"),
            ("7", "Discord webhook"), ("0", "ออก"),
        ]
        handlers = {
            "1": self.menu_start, "2": self.menu_status, "3": self.menu_settings,
            "4": self.menu_scan, "5": self.menu_cookie, "6": self.menu_auto,
            "7": self.menu_webhook,
        }
        while True:
            self.banner()
            self.menu_panel(rows, "เมนู")
            choice = Prompt.ask("[green]เลือกเมนู[/green]",
                                choices=[row[0] for row in rows], default="1")
            if choice == "0":
                self.shutdown()
                return
            try:
                handlers[choice]()
            except KeyboardInterrupt:
                self.console.print("[dim]ยกเลิก[/dim]")
            Prompt.ask("[dim]กด Enter เพื่อกลับเมนู[/dim]", default="", show_default=False)

    # -- 1: run ------------------------------------------------------------

    def menu_start(self) -> None:
        if not self.ensure_game() or not self.ensure_instances():
            self.console.print("[red]ยังเริ่มไม่ได้ — ต้องมีลิงก์เกมและอินสแตนซ์[/red]")
            return
        if self.cfg["auto"].get("cookie_login_on_start"):
            self.apply_cookies(silent=True)
        if self.cfg["auto"].get("wake_lock"):
            wake_lock(self.root)
        if self.cfg["heartbeat"].get("enabled"):
            port = int(self.cfg["heartbeat"].get("port", 8765) or 8765)
            self.hb_server = HeartbeatServer(port, self.store)
            if not self.hb_server.start():
                self.console.print("[yellow]เปิดพอร์ต %d ไม่ได้ — heartbeat จะถูกข้าม[/yellow]" % port)
                self.logger.warning("heartbeat port busy: %s", port)
        self.engine = Engine(self.cfg, self.instances, self.logger, self.root,
                             self.paths, self.discord, self.store)
        self.engine.start()
        refresh = float(self.cfg["performance"].get("ui_refresh_sec", 2) or 2)
        try:
            with Live(build_dashboard(self.engine), console=self.console,
                      refresh_per_second=max(1, int(round(1 / max(refresh, 0.5)))),
                      screen=False) as live:
                while self.engine.thread and self.engine.thread.is_alive():
                    time.sleep(min(0.5, refresh))
                    live.update(build_dashboard(self.engine))
        except KeyboardInterrupt:
            pass
        finally:
            self.engine.stop()
            self.engine = None
            self.hb_server.stop()
            if self.cfg["auto"].get("wake_lock"):
                wake_unlock(self.root)
            self.console.print("[dim]หยุดการทำงานแล้ว[/dim]")

    # -- 2: status ---------------------------------------------------------

    def menu_status(self) -> None:
        self.banner()
        if not self.ensure_instances():
            self.console.print("[red]ยังไม่มีอินสแตนซ์ — ใช้เมนู 4 เพื่อสแกน[/red]")
            return
        snapshot = ShellSnapshot(0)
        snapshot.refresh(force=True)
        table = Table(box=box.SIMPLE_HEAD)
        for column in ("#", "แพ็กเกจ", "โปรเซส", "หน้าจอ", "บัญชี", "สถานะ"):
            table.add_column(column, style="cyan" if column == "แพ็กเกจ" else "")
        for index, inst in enumerate(self.instances, start=1):
            running = snapshot.has_process(inst.package) if self.root else None
            table.add_row(
                str(index), inst.package,
                ("ทำงาน" if running else "ไม่พบ") if self.root else "-",
                ("ใช่" if snapshot.in_foreground(inst.package) else "ไม่") if self.root else "-",
                inst.account, STATES.get(inst.state, inst.state))
        self.console.print(table)
        self.console.print("[dim]โหมด %s · %d อินสแตนซ์ · heartbeat %s[/dim]" % (
            "root" if self.root else "non-root", len(self.instances),
            "เปิด" if self.cfg["heartbeat"].get("enabled") else "ปิด"))

    # -- 3: settings -------------------------------------------------------

    def menu_game_link(self) -> None:
        current = self.cfg["game"].get("raw", "") or ""
        raw = Prompt.ask("[green]ลิงก์เกม / Place ID[/green]",
                         default=current, show_default=bool(current))
        try:
            parsed = parse_game_link(raw)
        except ValueError as exc:
            self.console.print("[red]%s[/red]" % exc)
            return
        self.cfg["game"] = parsed
        self.save()
        self.console.print("[green]บันทึกแล้ว[/green] · place %s · %s\n[dim]uri: %s[/dim]" % (
            parsed["place_id"], parsed["kind"], parsed["uri"]))
        self.logger.info("game link set: %s", parsed["uri"])

    def menu_settings(self) -> None:
        while True:
            self.banner()
            self.menu_panel([
                ("1", "ลิงก์เกม: %s" % (self.cfg["game"].get("uri") or "-")),
                ("2", "รอบตรวจ: %d วิ" % int(self.cfg.get("interval", 30))),
                ("3", "ดีเลย์ก่อนเปิดแอป: %d วิ" % int(self.cfg.get("launch_delay", 15))),
                ("0", "กลับ"),
            ], "ตั้งค่า")
            choice = Prompt.ask("เลือก", choices=["1", "2", "3", "0"], default="0")
            if choice == "0":
                return
            if choice == "1":
                self.menu_game_link()
            elif choice == "2":
                self.cfg["interval"] = max(5, IntPrompt.ask(
                    "รอบตรวจ (วินาที)", default=int(self.cfg.get("interval", 30))))
                self.save()
            elif choice == "3":
                self.cfg["launch_delay"] = max(0, IntPrompt.ask(
                    "ดีเลย์ก่อนเปิดแอป (วินาที)", default=int(self.cfg.get("launch_delay", 15))))
                self.save()

    # -- 4: scan -----------------------------------------------------------

    def menu_scan(self, silent: bool = False) -> List[str]:
        if not silent:
            self.banner()
        with self.console.status("[green]กำลังสแกนแพ็กเกจ…[/green]"):
            found = scan_packages()
        if not found:
            self.console.print("[yellow]ไม่พบแพ็กเกจที่ขึ้นต้นด้วย %s[/yellow]\n"
                               "[dim]พิมพ์ชื่อแพ็กเกจเอง คั่นด้วยจุลภาค[/dim]" % PREFIX)
            found = [item.strip() for item in Prompt.ask("แพ็กเกจ", default="").split(",") if item.strip()]
            if not found:
                return []
        table = Table(box=box.SIMPLE_HEAD)
        table.add_column("#", style="dim", justify="right")
        table.add_column("แพ็กเกจ", style="cyan")
        table.add_column("เลือกอยู่")
        selected = set(self.cfg.get("instances") or [])
        for index, name in enumerate(found, start=1):
            table.add_row(str(index), name, "✓" if name in selected else "")
        self.console.print(table)
        answer = Prompt.ask("เลือกที่จะรัน ([green]1,3[/green] หรือ [green]all[/green])",
                            default="all").strip().lower()
        if answer in ("all", "*"):
            chosen = list(found)
        else:
            chosen = []
            for token in answer.replace(" ", "").split(","):
                if token.isdigit() and 1 <= int(token) <= len(found):
                    chosen.append(found[int(token) - 1])
                elif token in found:
                    chosen.append(token)
            chosen = list(dict.fromkeys(chosen))
        if chosen:
            self.cfg["instances"] = chosen
            self.save()
            self.load_instances()
            self.console.print("[green]เลือก %d อินสแตนซ์[/green]" % len(chosen))
        return chosen

    # -- 5: cookies --------------------------------------------------------

    def apply_cookies(self, silent: bool = False) -> None:
        cookies = load_cookies(self.paths.cookie)
        if not cookies:
            if not silent:
                self.console.print("[yellow]ไม่พบคุกกี้ใน %s[/yellow]\n"
                                   "[dim]หนึ่งคุกกี้ต่อบรรทัด ไม่ต้องใส่เครื่องหมายคำพูด[/dim]"
                                   % self.paths.cookie)
            return
        if not self.instances:
            self.load_instances()
        if not self.instances:
            return
        table = Table(box=box.SIMPLE_HEAD)
        for column in ("แพ็กเกจ", "บัญชี", "คุกกี้", "ผลลัพธ์"):
            table.add_column(column, overflow="fold")
        for index, inst in enumerate(self.instances):
            token = cookies[index % len(cookies)]
            valid = validate_cookie(token)
            if not valid:
                table.add_row(inst.package, "-", mask_cookie(token), "ไม่ผ่านการตรวจสอบ")
                continue
            inst.cookie = token
            inst.account = valid.get("name") or "-"
            inst.user_id = int(valid.get("id") or 0)
            status = "ตรวจสอบแล้ว"
            if self.root:
                ok, detail = inject_cookie_package(inst.package, token, self.logger)
                status = detail if ok else "เขียนไม่ได้: %s" % detail
            table.add_row(inst.package, inst.account, mask_cookie(token), status)
        self.console.print(table)
        if not self.root:
            self.console.print("[dim]โหมด non-root: ตรวจสอบเท่านั้น ไม่สามารถเขียนคุกกี้ลง clone ได้[/dim]")

    def menu_cookie(self) -> None:
        self.banner()
        if self.ensure_instances():
            self.apply_cookies()

    # -- 6: auto settings --------------------------------------------------

    AUTO_TOGGLES = [
        ("start_on_launch", "เริ่มอัตโนมัติเมื่อเปิดโปรแกรม"),
        ("wake_lock", "Wake-lock"),
        ("force_stop", "Force-stop ก่อนเปิดใหม่ (root)"),
        ("foreground_check", "ตรวจจอหน้าบนสุด (root)"),
        ("screenshot_on_crash", "แคปภาพเมื่อ crash (root)"),
        ("cookie_login_on_start", "โหลดคุกกี้ตอนเริ่ม"),
        ("presence_check", "ตรวจ presence (non-root)"),
        ("timed_rejoin", "Rejoin ตามเวลาเมื่อไม่รู้สถานะ"),
    ]

    def menu_auto(self) -> None:
        while True:
            self.banner()
            auto = self.cfg["auto"]
            rows = []
            for index, (key, label) in enumerate(self.AUTO_TOGGLES, start=1):
                state = "[green]เปิด[/green]" if auto.get(key) else "[red]ปิด[/red]"
                rows.append((str(index), "%s  ·  %s" % (label, state)))
            rows.append(("9", "ช่วงผ่อนผันหลังเปิดแอป: %d วิ" % int(auto.get("join_grace", 90))))
            rows.extend([("h", "Heartbeat →"), ("p", "Performance →"), ("0", "กลับ")])
            self.menu_panel(rows, "ตั้งค่าอัตโนมัติ")
            choice = Prompt.ask("เลือก", choices=[r[0] for r in rows], default="0")
            if choice == "0":
                self.save()
                return
            if choice == "h":
                self.menu_heartbeat()
                continue
            if choice == "p":
                self.menu_performance()
                continue
            if choice == "9":
                auto["join_grace"] = max(10, IntPrompt.ask(
                    "ช่วงผ่อนผัน (วินาที)", default=int(auto.get("join_grace", 90))))
            else:
                key = self.AUTO_TOGGLES[int(choice) - 1][0]
                auto[key] = not bool(auto.get(key))
                if key == "timed_rejoin" and auto[key]:
                    auto["timed_rejoin_min"] = max(1, IntPrompt.ask(
                        "rejoin ทุกกี่นาที", default=int(auto.get("timed_rejoin_min", 20))))
            self.save()

    def menu_heartbeat(self) -> None:
        while True:
            self.banner()
            hb = self.cfg["heartbeat"]
            self.menu_panel([
                ("1", "เปิดใช้งาน  ·  %s" % ("[green]เปิด[/green]" if hb.get("enabled") else "[red]ปิด[/red]")),
                ("2", "พอร์ต: %d" % int(hb.get("port", 8765))),
                ("3", "สคริปต์ส่งทุก: %d วิ" % int(hb.get("script_interval", 15))),
                ("4", "timeout: %d วิ" % int(hb.get("timeout", 90))),
                ("5", "สร้างไฟล์ .lua ลง heartbeat/"),
                ("0", "กลับ"),
            ], "Heartbeat")
            self.console.print("[dim]คำเตือน: การใช้ executor อาจขัดกับข้อกำหนดของ Roblox และเสี่ยงถูกแบน[/dim]")
            choice = Prompt.ask("เลือก", choices=["1", "2", "3", "4", "5", "0"], default="0")
            if choice == "0":
                self.save()
                return
            if choice == "1":
                hb["enabled"] = not bool(hb.get("enabled"))
            elif choice == "2":
                hb["port"] = max(1024, IntPrompt.ask("พอร์ต", default=int(hb.get("port", 8765))))
            elif choice == "3":
                hb["script_interval"] = max(5, IntPrompt.ask(
                    "วินาที", default=int(hb.get("script_interval", 15))))
            elif choice == "4":
                hb["timeout"] = max(30, IntPrompt.ask(
                    "timeout (วินาที)", default=int(hb.get("timeout", 90))))
            elif choice == "5":
                packages = [inst.package for inst in self.instances] or list(
                    self.cfg.get("instances") or [])
                if not packages:
                    self.console.print("[yellow]ยังไม่มีอินสแตนซ์ — สแกนก่อน[/yellow]")
                else:
                    written = write_lua_files(packages, self.paths.heartbeat,
                                              int(hb.get("port", 8765)),
                                              int(hb.get("script_interval", 15)))
                    self.console.print("[green]สร้าง %d ไฟล์ใน %s[/green]"
                                       % (len(written), self.paths.heartbeat))
            self.save()

    def menu_performance(self) -> None:
        while True:
            self.banner()
            perf = self.cfg["performance"]
            on = lambda value: "[green]เปิด[/green]" if value else "[red]ปิด[/red]"
            self.menu_panel([
                ("1", "แชร์ snapshot ต่อรอบ  ·  %s" % on(perf.get("shared_snapshot", True))),
                ("2", "TTL snapshot: %d วิ" % int(perf.get("snapshot_ttl", 3))),
                ("3", "แคช presence: %d วิ" % int(perf.get("presence_cache_sec", 60))),
                ("4", "ล้างแรมอัตโนมัติ  ·  %s" % on(perf.get("clear_ram"))),
                ("5", "ล้างแรมทุก: %d นาที" % int(perf.get("clear_ram_min", 30))),
                ("6", "เกณฑ์แรมต่ำ: %d MB" % int(perf.get("ram_low_mb", 300))),
                ("7", "am kill-all  ·  %s" % on(perf.get("am_kill_all"))),
                ("8", "รีเฟรชหน้าจอ: %s วิ" % perf.get("ui_refresh_sec", 2)),
                ("9", "ล้างแรมเดี๋ยวนี้ (root)"),
                ("0", "กลับ"),
            ], "Performance")
            choice = Prompt.ask("เลือก", choices=[str(i) for i in range(0, 10)], default="0")
            if choice == "0":
                self.save()
                return
            if choice == "1":
                perf["shared_snapshot"] = not bool(perf.get("shared_snapshot", True))
            elif choice == "2":
                perf["snapshot_ttl"] = max(1, IntPrompt.ask(
                    "TTL (วินาที)", default=int(perf.get("snapshot_ttl", 3))))
            elif choice == "3":
                perf["presence_cache_sec"] = max(10, IntPrompt.ask(
                    "แคช (วินาที)", default=int(perf.get("presence_cache_sec", 60))))
            elif choice == "4":
                perf["clear_ram"] = not bool(perf.get("clear_ram"))
            elif choice == "5":
                perf["clear_ram_min"] = max(5, IntPrompt.ask(
                    "ทุกกี่นาที", default=int(perf.get("clear_ram_min", 30))))
            elif choice == "6":
                perf["ram_low_mb"] = max(100, IntPrompt.ask(
                    "MB", default=int(perf.get("ram_low_mb", 300))))
            elif choice == "7":
                perf["am_kill_all"] = not bool(perf.get("am_kill_all"))
                if perf["am_kill_all"]:
                    self.console.print("[yellow]คำเตือน: am kill-all อาจฆ่า clone ที่อยู่เบื้องหลัง[/yellow]")
            elif choice == "8":
                perf["ui_refresh_sec"] = min(10, max(1, IntPrompt.ask(
                    "วินาที", default=int(float(perf.get("ui_refresh_sec", 2))))))
            elif choice == "9":
                ok, before, after = clear_ram(self.root)
                self.console.print("แรม: %d → %d MB (%s)" % (
                    before, after, "สำเร็จ" if ok else "ต้องใช้ root"))
            self.save()

    # -- 7: webhook --------------------------------------------------------

    def menu_webhook(self) -> None:
        while True:
            self.banner()
            hook = self.cfg["webhook"]
            events = hook.get("events", {})
            on = lambda value: "[green]เปิด[/green]" if value else "[red]ปิด[/red]"
            self.menu_panel([
                ("1", "เปิดใช้งาน  ·  %s" % on(hook.get("enabled"))),
                ("2", "URL: %s" % (scrub(hook.get("url", "")) or "-")),
                ("3", "เหตุการณ์: เริ่ม %s · crash %s · rejoin %s · หยุด %s · status %s" % tuple(
                    "✓" if events.get(key) else "—"
                    for key in ("start", "crash", "rejoin", "stop", "status"))),
                ("4", "แนบภาพหน้าจอ (root)  ·  %s" % on(hook.get("attach_screenshot"))),
                ("5", "รายงานสถานะทุก: %d นาที (0 = ปิด)" % int(hook.get("periodic_min", 10))),
                ("6", "ส่งข้อความทดสอบ"),
                ("0", "กลับ"),
            ], "Discord webhook")
            choice = Prompt.ask("เลือก", choices=["1", "2", "3", "4", "5", "6", "0"], default="0")
            if choice == "0":
                self.save()
                return
            if choice == "1":
                hook["enabled"] = not bool(hook.get("enabled"))
            elif choice == "2":
                url = Prompt.ask("URL ของ webhook", default=hook.get("url", ""), show_default=False)
                if not url or is_valid_webhook(url):
                    hook["url"] = url.strip()
                else:
                    self.console.print("[red]URL ไม่ถูกต้อง (ต้องขึ้นต้นด้วย https://discord.com/api/webhooks/)[/red]")
            elif choice == "3":
                sub = Prompt.ask("สลับเหตุการณ์ start/crash/rejoin/stop/status",
                                 default="status").strip().lower()
                if sub in events:
                    events[sub] = not bool(events.get(sub))
            elif choice == "4":
                if not self.root:
                    self.console.print("[yellow]ต้องใช้ root[/yellow]")
                else:
                    hook["attach_screenshot"] = not bool(hook.get("attach_screenshot"))
            elif choice == "5":
                hook["periodic_min"] = max(0, IntPrompt.ask(
                    "นาที (0 = ปิด)", default=int(hook.get("periodic_min", 10))))
            elif choice == "6":
                if not is_valid_webhook(hook.get("url", "")):
                    self.console.print("[red]ยังไม่มี URL ที่ถูกต้อง[/red]")
                else:
                    ok = self.discord._post("**%s** ข้อความทดสอบ" % APP_NAME, None)
                    self.console.print("[green]ส่งแล้ว[/green]" if ok else "[red]ส่งไม่สำเร็จ[/red]")
            self.save()

    def shutdown(self) -> None:
        if self.engine:
            self.engine.stop()
        self.hb_server.stop()
        if self.cfg["auto"].get("wake_lock"):
            wake_unlock(self.root)
        self.console.print("[dim]ออกจากโปรแกรม[/dim]")


# --------------------------------------------------------------------------- #
# self test - runs anywhere, no device needed
# --------------------------------------------------------------------------- #


def selftest() -> int:
    results: List[Tuple[str, bool, str]] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        results.append((name, bool(ok), note))

    # 1. link parsing
    cases = [
        ("1818", 1818, "", "place_id"),
        ("https://www.roblox.com/games/920587237/Adopt-Me", 920587237, "", "game"),
        ("https://www.roblox.com/games/1818/Classic-Crossroads?privateServerLinkCode=ABC123",
         1818, "ABC123", "private_server"),
        ("roblox://placeID=1818&linkCode=XYZ", 1818, "XYZ", "roblox_scheme"),
        ("https://www.roblox.com/share?code=abc&type=Server", 0, "", "share"),
    ]
    for raw, place, code, kind in cases:
        parsed = parse_game_link(raw)
        check("parse %s" % kind,
              parsed["place_id"] == place and parsed["link_code"] == code and parsed["kind"] == kind,
              parsed["uri"])
    check("private uri shape",
          parse_game_link(cases[2][0])["uri"] == "roblox://placeID=1818&linkCode=ABC123")
    try:
        parse_game_link("https://example.com/games/1")
        check("reject foreign host", False)
    except ValueError:
        check("reject foreign host", True)

    # 2. cookie masking + scrubbing
    cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--abc123456789"
    masked = mask_cookie(cookie)
    check("mask hides token", cookie[20:] not in masked and masked.startswith("****"), masked)
    dirty = "cookie=.ROBLOSECURITY=%s hook=https://discord.com/api/webhooks/123/abcDEF" % cookie
    clean = scrub(dirty)
    check("scrub removes cookie", "DO-NOT-SHARE" not in clean and "abc123456789" not in clean)
    check("scrub removes webhook", "abcDEF" not in clean)

    workdir = tempfile.mkdtemp(prefix="rejointest-")
    try:
        # 3. config defaults, atomic save, corrupt recovery
        cfg_path = os.path.join(workdir, "config.json")
        loaded = load_config(cfg_path)
        check("defaults merged", loaded["interval"] == 30 and loaded["auto"]["join_grace"] == 90)
        loaded["interval"] = 45
        save_config(cfg_path, loaded)
        check("atomic save round trip", load_config(cfg_path)["interval"] == 45)
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        recovered = load_config(cfg_path)
        backups = [n for n in os.listdir(workdir) if ".corrupt-" in n]
        check("corrupt config repaired", recovered["interval"] == 30, "backup=%d" % len(backups))
        check("corrupt file backed up", len(backups) == 1)

        # 4. sqlite injection on a mock Chromium DB
        db_path = os.path.join(workdir, "Cookies")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE cookies (creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, "
            "top_frame_site_key TEXT, name TEXT NOT NULL, value TEXT NOT NULL, "
            "encrypted_value BLOB NOT NULL, path TEXT NOT NULL, expires_utc INTEGER NOT NULL, "
            "is_secure INTEGER NOT NULL, is_httponly INTEGER NOT NULL, last_access_utc INTEGER NOT NULL, "
            "has_expires INTEGER NOT NULL, is_persistent INTEGER NOT NULL, priority INTEGER NOT NULL, "
            "samesite INTEGER NOT NULL, source_scheme INTEGER NOT NULL, source_port INTEGER NOT NULL, "
            "is_monolithic INTEGER, source_type INTEGER, has_cross_site_ancestor INTEGER)")
        insert_sql = ("INSERT INTO cookies (creation_utc,host_key,name,value,encrypted_value,path,"
                      "expires_utc,is_secure,is_httponly,last_access_utc,has_expires,is_persistent,"
                      "priority,samesite,source_scheme,source_port) "
                      "VALUES (0,'%s','%s','%s',X'','/',0,1,1,0,1,1,1,-1,2,443)")
        conn.execute(insert_sql % (".roblox.com", ".ROBLOSECURITY", "old"))
        conn.execute(insert_sql % (".example.com", "x", "y"))
        conn.commit()
        conn.close()
        injected = inject_cookie_into_db(db_path, ".ROBLOSECURITY=" + cookie)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT value FROM cookies WHERE host_key='.roblox.com'").fetchone()
        count = conn.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
        other = conn.execute("SELECT value FROM cookies WHERE host_key='.example.com'").fetchone()[0]
        conn.close()
        check("sqlite update in place", injected and row and row[0] == cookie)
        check("sqlite no duplicate row", count == 2, "rows=%d" % count)
        check("sqlite other rows untouched", other == "y")

        # 5. cookie file parsing
        cookie_path = os.path.join(workdir, "cookie.txt")
        with open(cookie_path, "w", encoding="utf-8") as fh:
            fh.write("# comment\n\n.ROBLOSECURITY=AAA111\n\"BBB222\"\n")
        check("cookie file parsing", load_cookies(cookie_path) == ["AAA111", "BBB222"])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # 6. backoff
    sequence = [backoff_delay(fails) for fails in range(1, 8)]
    check("backoff 10..300", sequence[:3] == [10, 20, 40] and max(sequence) <= 300, str(sequence))
    check("backoff capped", backoff_delay(20) == 300)

    # 7. global launch gate stagger
    gate = LaunchGate(15)
    slots = [gate.reserve(1_000_000.0 + offset) for offset in (0, 1, 2, 20, 21, 60)]
    gaps = [slots[i + 1] - slots[i] for i in range(len(slots) - 1)]
    check("launch gate spacing >= 15s", all(gap >= 15 for gap in gaps), str(gaps))

    # 8. heartbeat store + lua template
    lua = render_lua("com.roblox.clienv", 8765, 15)
    check("lua instance id + port", 'id = "com.roblox.clienv"' in lua and "port = 8765" in lua)
    check("lua double-run guard", "_G.GARNT_HEARTBEAT_RUNNING" in lua)
    check("lua has fallback transport", "game:HttpGet" in lua and "syn.request" in lua)
    store = HeartbeatStore()
    store.update("com.roblox.client", {"user": "tester", "uid": 42})
    record = store.get("com.roblox.client")
    check("heartbeat store", bool(record) and record["user"] == "tester" and record["ts"] > 0)

    # 9. helpers
    check("uptime format", fmt_uptime(3725).startswith("1 ชม."), fmt_uptime(3725))
    check("webhook validation",
          is_valid_webhook("https://discord.com/api/webhooks/1/abc")
          and not is_valid_webhook("nope"))

    passed = sum(1 for _, ok, _ in results if ok)
    print("GARNT TOOL REJOIN - self test")
    print("-" * 62)
    for name, ok, note in results:
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  ·  " + note) if note else ""))
    print("-" * 62)
    print("%d/%d ผ่าน" % (passed, len(results)))
    return 0 if passed == len(results) else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="rejoin.py",
                                     description="%s — %s" % (APP_NAME, APP_SUBTITLE))
    parser.add_argument("--selftest", action="store_true", help="run internal logic tests")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if requests is None:
        Console().print("[red]ไม่พบไลบรารี requests[/red] — ติดตั้งด้วย: "
                        "[green]pip install rich requests[/green]")
    app = App()
    try:
        app.main_menu()
    except KeyboardInterrupt:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())


