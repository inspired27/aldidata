# -*- coding: utf-8 -*-
from __future__ import annotations

from flask import Flask, render_template_string, request, redirect, url_for, jsonify
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import time, json, os, uuid, threading, re
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# ============================================================
# CONFIG
# ============================================================
ALDI_USERNAME = os.getenv("ALDI_USERNAME", "").strip()
ALDI_PASSWORD = os.getenv("ALDI_PASSWORD", "").strip()

OVERVIEW_URL = "https://my.aldimobile.com.au/admin/s/5620272/shareddataoverview"
LOGIN_PAGE_URL = "https://my.aldimobile.com.au/login/"
LOGIN_POST_URL = "https://my.aldimobile.com.au/login_check"
BALANCE_URL_TMPL = "https://my.aldimobile.com.au/admin/s/{mobile}/shareddataajax/balance"

# Your mobiles (4)
MOBILES = [
    "0466008129",
    "0466008170",
    "0494584269",
    "0415100346",
]

# Friendly labels (prefix)
MOBILE_LABELS = {
    "0415100346": "Pablo",
    "0466008129": "Josh",
    "0466008170": "Sam",
    "0494584269": "Spare",
}

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "schedule_matrix.json")

# Cache / performance
CACHE_TTL_SECONDS = 20
LIMIT_CACHE_TTL_SECONDS = 1800
SESSION_OK_TTL_SECONDS = 900
BALANCE_WORKERS = 6

# Manual update polling
POLL_INTERVAL_SECONDS = 2
POLL_TIMEOUT_SECONDS = 45

# Progress shared across gunicorn workers
PROGRESS_PATH = os.getenv("PROGRESS_PATH", "/tmp/aldiapp_progress.json")

# Only one scheduler instance across gunicorn workers
SCHED_LOCK_PATH = os.getenv("SCHED_LOCK_PATH", "/tmp/aldiapp_scheduler.lock")

UPSTREAM_UNREACHABLE_MSG = "Cannot connect to ALDI Mobile (network/DNS)."

app = Flask(__name__)

# ============================================================
# SHARED STATE
# ============================================================
_SESSION: requests.Session | None = None
_LAST_LOGIN_OK_TS = 0.0

cache: dict[str, dict] = {}
_LIMIT_CACHE = {"ts": 0.0, "limits": {}}
_CACHE_LOCK = threading.Lock()

scheduler: BackgroundScheduler | None = None
_sched_lock_fd = None


# ============================================================
# UTIL
# ============================================================
def display_name(mobile: str) -> str:
    lbl = MOBILE_LABELS.get(mobile)
    return f"{lbl} \u2013 {mobile}" if lbl else mobile


def now_ts_str(tzname: str) -> str:
    try:
        tz = pytz.timezone(tzname)
    except Exception:
        tz = pytz.timezone("Australia/Brisbane")
    return datetime.now(tz).strftime("%a %d %b %H:%M")


def server_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _error_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _url_path(url: str) -> str:
    try:
        return urlparse(url).path or "/"
    except Exception:
        return "<unknown>"


def _mb_to_gb(mb_str: str | int | float | None) -> float:
    try:
        mb = float(str(mb_str).strip())
        return mb / 1024.0
    except Exception:
        return 0.0


def _fmt_gb(v: float | None) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}GB"
    except Exception:
        return "—"


def _parse_float_from_text(s: str) -> float | None:
    s = (s or "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _status_class(status: str) -> str:
    s = (status or "").strip()
    if s == "Error":
        return "err"
    if s in {"Pending", "Loading..."}:
        return "pending"
    return "ok"


# ============================================================
# SESSION + HTTP
# ============================================================
class UpstreamError(Exception):
    def __init__(self, error_code: str, user_message: str, stage: str = "", *, http_status: int | None = None):
        super().__init__(user_message)
        self.error_code = error_code
        self.user_message = user_message
        self.stage = stage
        self.http_status = http_status


def _classify_request_exception(err: Exception) -> tuple[str, str]:
    if isinstance(err, requests.exceptions.SSLError):
        return "TLS_FAIL", "Cannot establish a secure connection to ALDI Mobile (TLS)."
    if isinstance(err, requests.exceptions.Timeout):
        return "OUTBOUND_TIMEOUT", UPSTREAM_UNREACHABLE_MSG
    if isinstance(err, requests.exceptions.ConnectionError):
        low = str(err).lower()
        dns_markers = [
            "name resolution",
            "name or service not known",
            "temporary failure in name resolution",
            "nodename nor servname",
            "getaddrinfo",
            "failed to resolve",
        ]
        if any(m in low for m in dns_markers):
            return "DNS_FAIL", UPSTREAM_UNREACHABLE_MSG
        return "OUTBOUND_CONNECT_FAIL", UPSTREAM_UNREACHABLE_MSG
    return "OUTBOUND_REQUEST_FAIL", "ALDI Mobile request failed."


def _http_error_code(status_code: int) -> str:
    if status_code == 403:
        return "HTTP_403"
    if 500 <= status_code <= 599:
        return "HTTP_5XX"
    return f"HTTP_{status_code}"


def _http_error_message(status_code: int) -> str:
    if status_code == 403:
        return "ALDI Mobile refused access (HTTP 403)."
    if 500 <= status_code <= 599:
        return "ALDI Mobile is unavailable (HTTP 5xx)."
    return f"ALDI Mobile request failed (HTTP {status_code})."


def _raise_if_http_error(r: requests.Response, method: str, url: str):
    if r.status_code >= 400:
        code = _http_error_code(r.status_code)
        raise UpstreamError(code, _http_error_message(r.status_code), stage=f"{method} {_url_path(url)}", http_status=r.status_code)


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.trust_env = False
        s.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        _SESSION = s
    return _SESSION


def _http_get(url: str, **kwargs) -> requests.Response:
    try:
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)
        return get_session().get(url, **kwargs)
    except requests.RequestException as e:
        code, msg = _classify_request_exception(e)
        raise UpstreamError(code, msg, stage=f"GET {_url_path(url)}") from e


def _http_post(url: str, **kwargs) -> requests.Response:
    try:
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)
        return get_session().post(url, **kwargs)
    except requests.RequestException as e:
        code, msg = _classify_request_exception(e)
        raise UpstreamError(code, msg, stage=f"POST {_url_path(url)}") from e


def _http_head(url: str, **kwargs) -> requests.Response:
    try:
        kwargs.setdefault("timeout", 5)
        kwargs.setdefault("allow_redirects", True)
        return get_session().head(url, **kwargs)
    except requests.RequestException as e:
        code, msg = _classify_request_exception(e)
        raise UpstreamError(code, msg, stage=f"HEAD {_url_path(url)}") from e


def public_error_message(err: Exception) -> str:
    if isinstance(err, UpstreamError):
        return err.user_message
    if isinstance(err, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ProxyError)):
        return UPSTREAM_UNREACHABLE_MSG
    return "Operation failed"


def looks_like_login_page(html: str) -> bool:
    low = (html or "").lower()
    return ("login_password" in low) or ("login_check" in low and "csrf" in low)


def get_csrf_from_login_page(html: str) -> str | None:
    soup = BeautifulSoup(html or "", "html.parser")
    csrf = soup.find("input", attrs={"name": "_csrf_token"})
    return csrf.get("value") if csrf else None


def ensure_logged_in(progress_op_id: str | None = None):
    global _LAST_LOGIN_OK_TS
    if (time.time() - _LAST_LOGIN_OK_TS) < SESSION_OK_TTL_SECONDS:
        return

    progress_set(progress_op_id, "Authenticating...")

    ov = _http_get(OVERVIEW_URL)
    _raise_if_http_error(ov, "GET", OVERVIEW_URL)
    if not looks_like_login_page(ov.text):
        _LAST_LOGIN_OK_TS = time.time()
        progress_set(progress_op_id, "Authenticated")
        return

    progress_set(progress_op_id, "Opening login page...")
    lp = _http_get(LOGIN_PAGE_URL)
    _raise_if_http_error(lp, "GET", LOGIN_PAGE_URL)
    csrf = get_csrf_from_login_page(lp.text)
    if not csrf:
        lp2 = _http_get(LOGIN_PAGE_URL)
        _raise_if_http_error(lp2, "GET", LOGIN_PAGE_URL)
        csrf = get_csrf_from_login_page(lp2.text)

    if not csrf:
        raise UpstreamError("LOGIN_CSRF_MISSING", "Could not find CSRF token on login page.", stage="GET /login")

    if not ALDI_USERNAME or not ALDI_PASSWORD:
        raise UpstreamError("MISSING_CREDS", "Missing ALDI_USERNAME or ALDI_PASSWORD env vars.", stage="env")

    progress_set(progress_op_id, "Authenticating...")

    payload = {
        "login_user[login]": ALDI_USERNAME,
        "login_user[password]": ALDI_PASSWORD,
        "_csrf_token": csrf,
    }

    resp = _http_post(LOGIN_POST_URL, data=payload, headers={"Referer": LOGIN_PAGE_URL})
    _raise_if_http_error(resp, "POST", LOGIN_POST_URL)

    ov2 = _http_get(OVERVIEW_URL)
    _raise_if_http_error(ov2, "GET", OVERVIEW_URL)
    if looks_like_login_page(ov2.text):
        raise UpstreamError("LOGIN_FAILED", "Login failed.", stage="POST /login_check")

    _LAST_LOGIN_OK_TS = time.time()
    progress_set(progress_op_id, "Authenticated")


# ============================================================
# PROGRESS STORE (FILE-BASED, WORKER-SAFE)
# ============================================================
def _progress_read_all() -> dict:
    if not os.path.exists(PROGRESS_PATH):
        return {}
    try:
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            except Exception:
                pass
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _progress_write_all(d: dict):
    tmp = PROGRESS_PATH + ".tmp"
    if os.path.dirname(PROGRESS_PATH):
        os.makedirs(os.path.dirname(PROGRESS_PATH), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        json.dump(d, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PROGRESS_PATH)


def progress_init(msg: str = "Starting...") -> str:
    op_id = uuid.uuid4().hex
    d = _progress_read_all()
    d[op_id] = {
        "msg": msg,
        "seq": 1,
        "done": False,
        "ok": True,
        "result": None,
        "ts": time.time(),
    }
    _progress_write_all(d)
    return op_id


def progress_set(op_id: str | None, msg: str):
    if not op_id:
        return
    d = _progress_read_all()
    st = d.get(op_id)
    if not isinstance(st, dict):
        return
    st["msg"] = msg
    st["seq"] = int(st.get("seq", 0)) + 1
    st["ts"] = time.time()
    d[op_id] = st
    _progress_write_all(d)


def progress_done(op_id: str | None, ok: bool, result=None):
    if not op_id:
        return
    d = _progress_read_all()
    st = d.get(op_id)
    if not isinstance(st, dict):
        return
    st["done"] = True
    st["ok"] = bool(ok)
    st["result"] = result
    st["ts"] = time.time()
    d[op_id] = st
    _progress_write_all(d)


def progress_complete(op_id: str | None, result=None):
    progress_set(op_id, "Complete")
    progress_done(op_id, True, result)


# ============================================================
# LIMITS (SLOW) - FETCH ONCE FROM OVERVIEW FOR ALL MOBILES
# ============================================================
def _parse_limits_from_overview_html(html: str) -> dict[str, float | None]:
    soup = BeautifulSoup(html or "", "html.parser")
    out: dict[str, float | None] = {m: None for m in MOBILES}

    for m in MOBILES:
        service_div = soup.find("div", attrs={"data-service_number": m})
        if not service_div:
            continue
        panel = service_div.find_parent("div", class_="panel")
        if not panel:
            continue

        div = panel.find("div", id=lambda x: x and x.startswith("usageLimitDivconsumerUsageLimit"))
        txt = div.get_text(" ", strip=True) if div else ""
        out[m] = _parse_float_from_text(txt)

    return out


def get_limits(progress_op_id: str | None = None, force: bool = False) -> dict[str, float | None]:
    now = time.time()
    if (not force) and _LIMIT_CACHE.get("limits") and (now - float(_LIMIT_CACHE.get("ts", 0.0)) < LIMIT_CACHE_TTL_SECONDS):
        return dict(_LIMIT_CACHE["limits"])

    ensure_logged_in(progress_op_id)
    progress_set(progress_op_id, "Loading limits (overview)...")

    ov = _http_get(OVERVIEW_URL)
    _raise_if_http_error(ov, "GET", OVERVIEW_URL)
    if looks_like_login_page(ov.text):
        global _LAST_LOGIN_OK_TS
        _LAST_LOGIN_OK_TS = 0.0
        ensure_logged_in(progress_op_id)
        ov = _http_get(OVERVIEW_URL)
        _raise_if_http_error(ov, "GET", OVERVIEW_URL)

    limits = _parse_limits_from_overview_html(ov.text)
    _LIMIT_CACHE["ts"] = time.time()
    _LIMIT_CACHE["limits"] = dict(limits)
    return limits


# ============================================================
# BALANCE (FAST) - PER MOBILE ENDPOINT
# ============================================================
def _extract_balance_items(js: dict) -> tuple[float | None, float | None]:
    remaining_mb = None
    used_mb = None

    items = js.get("resource_items") or js.get("RESOURCE_BALANCE") or []
    for it in items:
        plan_name = (it.get("plan_name") or it.get("PLAN_NAME") or "").strip().lower()
        v = it.get("value") if "value" in it else it.get("VALUE")
        if not isinstance(plan_name, str):
            continue
        if "plan data remaining" in plan_name:
            remaining_mb = v
        elif "data usage counter" in plan_name:
            used_mb = v

    rem_gb = _mb_to_gb(remaining_mb) if remaining_mb is not None else None
    used_gb = _mb_to_gb(used_mb) if used_mb is not None else None
    return rem_gb, used_gb


def fetch_balance_json(mobile: str, progress_op_id: str | None = None) -> dict:
    url = BALANCE_URL_TMPL.format(mobile=mobile)

    r = _http_get(url, headers={"Accept": "application/json,*/*"})
    _raise_if_http_error(r, "GET", url)

    ctype = (r.headers.get("Content-Type") or "").lower()
    if ("application/json" not in ctype) or (r.text and r.text.lstrip().startswith("<")):
        global _LAST_LOGIN_OK_TS
        _LAST_LOGIN_OK_TS = 0.0
        ensure_logged_in(progress_op_id)
        r = _http_get(url, headers={"Accept": "application/json,*/*"})
        _raise_if_http_error(r, "GET", url)

    try:
        return r.json()
    except Exception:
        raise UpstreamError("BALANCE_PARSE_FAIL", "Failed to parse balance JSON.", stage=f"GET {_url_path(url)}")


# ============================================================
# STATUS LINE BUILDING
# ============================================================
def build_line_for_mobile(mobile: str, limits: dict[str, float | None], bal_json: dict) -> str:
    plan_remaining_gb, used_gb = _extract_balance_items(bal_json)
    lim = limits.get(mobile)

    remaining_gb = None
    if lim is not None and used_gb is not None and lim > 0:
        remaining_gb = max(lim - used_gb, 0.0)
    else:
        remaining_gb = plan_remaining_gb

    return f"Limit: {_fmt_gb(lim)}  >  Used: {_fmt_gb(used_gb)}  >  Remaining: {_fmt_gb(remaining_gb)}"


def cache_set(mobile: str, line: str, status: str, error_code: str | None = None, error_ts: str | None = None):
    with _CACHE_LOCK:
        cache[mobile] = {
            "ts": time.time(),
            "line": line,
            "status": status,
            "error_code": error_code,
            "error_ts": error_ts,
        }


def cache_get(mobile: str):
    with _CACHE_LOCK:
        item = cache.get(mobile)
    if not item:
        return None
    if time.time() - float(item.get("ts", 0.0)) > CACHE_TTL_SECONDS:
        return None
    return item


# ============================================================
# UPDATE LIMIT (MANUAL/SCHEDULER) - NEEDS OVERVIEW FORM/TOKEN
# ============================================================
def get_panel_for_mobile(html: str, mobile: str):
    soup = BeautifulSoup(html or "", "html.parser")
    service_div = soup.find("div", attrs={"data-service_number": mobile})
    if not service_div:
        raise Exception(f"Mobile {mobile} not found on overview page.")
    panel = service_div.find_parent("div", class_="panel")
    if not panel:
        raise Exception("Panel not found.")
    return panel


def submit_limit_form(mobile: str, value: str, op_id: str | None = None):
    ensure_logged_in(op_id)

    progress_set(op_id, "Loading overview for update...")
    ov = _http_get(OVERVIEW_URL)
    _raise_if_http_error(ov, "GET", OVERVIEW_URL)
    if looks_like_login_page(ov.text):
        global _LAST_LOGIN_OK_TS
        _LAST_LOGIN_OK_TS = 0.0
        ensure_logged_in(op_id)
        ov = _http_get(OVERVIEW_URL)
        _raise_if_http_error(ov, "GET", OVERVIEW_URL)

    progress_set(op_id, f"Finding {display_name(mobile)} in family plan...")
    panel = get_panel_for_mobile(ov.text, mobile)

    form = panel.find("form", class_="consumerDataLimitForm")
    if not form:
        raise Exception("Usage limit form not found.")

    form_id = form.get("id") or ""
    if not form_id.startswith("consumerUsageLimit"):
        raise Exception(f"Unexpected form id: {form_id}")

    suffix = form_id.replace("consumerUsageLimit", "")
    token_name = f"consumerUsageLimit{suffix}[_token]"
    token_input = form.find("input", attrs={"name": token_name})
    if not token_input or not token_input.get("value"):
        raise Exception("Per-form CSRF token missing.")

    payload = {
        f"consumerUsageLimit{suffix}[usageLimit]": str(value),
        token_name: token_input.get("value"),
        f"consumerUsageLimit{suffix}[submit]": "Update",
    }

    action = form.get("action")
    post_url = urljoin(OVERVIEW_URL, action) if action else OVERVIEW_URL

    progress_set(op_id, f"Submitting limit update ({display_name(mobile)} -> {value}GB)...")
    resp = _http_post(post_url, data=payload, headers={"Referer": OVERVIEW_URL})
    _raise_if_http_error(resp, "POST", post_url)

    try:
        new_lim = float(str(value).strip())
    except Exception:
        new_lim = None
    if isinstance(_LIMIT_CACHE.get("limits"), dict):
        _LIMIT_CACHE["limits"][mobile] = new_lim
        _LIMIT_CACHE["ts"] = time.time()

    with _CACHE_LOCK:
        cache.pop(mobile, None)


def wait_until_done(mobile: str, op_id: str | None = None):
    start = time.time()
    while True:
        ensure_logged_in(op_id)
        ov = _http_get(OVERVIEW_URL)
        _raise_if_http_error(ov, "GET", OVERVIEW_URL)
        panel = get_panel_for_mobile(ov.text, mobile)
        panel_text = " ".join(panel.get_text("\n", strip=True).split()).lower()
        pending = "pending" in panel_text

        if not pending:
            return True, round(time.time() - start, 1)

        progress_set(op_id, "Waiting for ALDI to finish pending update...")
        if time.time() - start > POLL_TIMEOUT_SECONDS:
            return False, round(time.time() - start, 1)

        time.sleep(POLL_INTERVAL_SECONDS)


def set_limit_and_wait(mobile: str, value: str, op_id: str | None = None):
    submit_limit_form(mobile, value, op_id=op_id)
    done, elapsed = wait_until_done(mobile, op_id=op_id)

    limits = get_limits(op_id, force=False)
    bal = fetch_balance_json(mobile, op_id)
    line = build_line_for_mobile(mobile, limits, bal)

    cfg = load_cfg()
    tzname = cfg.get("timezone", "Australia/Brisbane")
    cache_set(mobile, line, now_ts_str(tzname))

    return {"mobile": mobile, "requested": value, "done": done, "elapsed": elapsed, "line": line}


# ============================================================
# SCHEDULER CONFIG
# ============================================================
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def empty_week():
    return {d: [{"time": "", "value": ""} for _ in range(4)] for d in DAYS}


def empty_default_row():
    return [{"time": "", "value": ""} for _ in range(4)]


def default_config():
    return {
        "timezone": "Australia/Brisbane",
        "mobiles": {m: {"enabled": True, "default": empty_default_row(), "week": empty_week()} for m in MOBILES},
    }


def _ensure_cfg_shape(cfg: dict) -> dict:
    cfg = cfg if isinstance(cfg, dict) else {}
    cfg.setdefault("timezone", "Australia/Brisbane")

    mob = cfg.get("mobiles")
    if not isinstance(mob, dict):
        mob = {}

    out = {}
    for m in MOBILES:
        mcfg = mob.get(m)
        if not isinstance(mcfg, dict):
            mcfg = {}
        mcfg.setdefault("enabled", True)

        drow = mcfg.get("default")
        if not isinstance(drow, list):
            drow = empty_default_row()
        if len(drow) < 4:
            drow.extend([{"time": "", "value": ""} for _ in range(4 - len(drow))])
        mcfg["default"] = drow[:4]

        week = mcfg.get("week")
        if not isinstance(week, dict):
            week = empty_week()

        for d in DAYS:
            slots = week.get(d)
            if not isinstance(slots, list):
                slots = [{"time": "", "value": ""} for _ in range(4)]
            if len(slots) < 4:
                slots.extend([{"time": "", "value": ""} for _ in range(4 - len(slots))])
            week[d] = slots[:4]

        mcfg["week"] = week
        out[m] = mcfg

    cfg["mobiles"] = out
    return cfg


def save_cfg(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def load_cfg():
    if not os.path.exists(CONFIG_PATH):
        cfg = _ensure_cfg_shape(default_config())
        save_cfg(cfg)
        return cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = _ensure_cfg_shape(default_config())
        save_cfg(cfg)
        return cfg
    cfg = _ensure_cfg_shape(cfg)
    try:
        save_cfg(cfg)
    except Exception:
        pass
    return cfg


def format_gb_value(value: str) -> str:
    try:
        return f"{float(str(value).strip()):.2f}"
    except Exception:
        return str(value).strip()


def get_next_scheduled_change(mcfg: dict, tzname: str):
    if not bool((mcfg or {}).get("enabled", False)):
        return None
    try:
        tz = pytz.timezone(tzname)
    except Exception:
        tz = pytz.timezone("Australia/Brisbane")

    now = datetime.now(tz)
    upper = now + timedelta(days=7)
    week = (mcfg or {}).get("week") or {}
    best = None

    for day, slots in week.items():
        day_idx = WEEKDAY_INDEX.get(day)
        if day_idx is None:
            continue
        day_offset = (day_idx - now.weekday()) % 7
        target_date = (now + timedelta(days=day_offset)).date()

        for slot in (slots or []):
            if not isinstance(slot, dict):
                continue
            t = str(slot.get("time") or "").strip()
            v = str(slot.get("value") or "").strip()
            if not t or not v or ":" not in t:
                continue
            hh, mm = t.split(":", 1)
            if not (hh.isdigit() and mm.isdigit()):
                continue

            naive_dt = datetime(target_date.year, target_date.month, target_date.day, int(hh), int(mm))
            dt = tz.localize(naive_dt)
            if dt <= now or dt > upper:
                continue

            if best is None or dt < best["dt"]:
                best = {"dt": dt, "value": v}

    if not best:
        return None

    return {
        "next_change_label": best["dt"].strftime("%a %d %b %H:%M"),
        "next_change_gb": format_gb_value(best["value"]),
    }


def _try_acquire_scheduler_lock() -> bool:
    global _sched_lock_fd
    if _sched_lock_fd is not None:
        return True
    try:
        fd = os.open(SCHED_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _sched_lock_fd = fd
        return True
    except Exception:
        try:
            if "fd" in locals():
                os.close(fd)
        except Exception:
            pass
        return False


def start_scheduler():
    global scheduler
    if scheduler is not None:
        return
    if not _try_acquire_scheduler_lock():
        return

    cfg = load_cfg()
    tz = pytz.timezone(cfg["timezone"])
    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.start()
    reload_jobs()


def reload_jobs():
    global scheduler
    if scheduler is None:
        return

    for job in scheduler.get_jobs():
        scheduler.remove_job(job.id)

    cfg = load_cfg()
    tz = pytz.timezone(cfg["timezone"])

    for mobile, mcfg in cfg["mobiles"].items():
        if not bool(mcfg.get("enabled", False)):
            continue
        week = mcfg.get("week", {})
        for day in DAYS:
            slots = week.get(day, [])
            for i in range(4):
                slot = slots[i] if i < len(slots) else {"time": "", "value": ""}
                t = (slot.get("time") or "").strip()
                v = (slot.get("value") or "").strip()
                if not t or not v or ":" not in t:
                    continue
                hh, mm = t.split(":", 1)
                if not (hh.isdigit() and mm.isdigit()):
                    continue

                job_id = f"{mobile}_{day}_slot{i}"
                trigger = CronTrigger(day_of_week=day, hour=int(hh), minute=int(mm), timezone=tz)
                scheduler.add_job(
                    func=run_scheduled_set,
                    trigger=trigger,
                    id=job_id,
                    replace_existing=True,
                    args=[mobile, v],
                    misfire_grace_time=180,
                )


def run_scheduled_set(mobile: str, value: str):
    try:
        res = set_limit_and_wait(mobile, value, op_id=None)
        print(f"[SCHEDULE] {mobile} -> {value} | done={res['done']} | {res['elapsed']}s")
    except Exception as e:
        print(f"[SCHEDULE] ERROR {mobile} -> {value}: {e}")


# ============================================================
# ROUTES
# ============================================================
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True}), 200


@app.get("/health/upstream")
def health_upstream():
    target = "https://my.aldimobile.com.au"
    try:
        r = _http_head(target, timeout=5)
        if r.status_code >= 400:
            _raise_if_http_error(r, "HEAD", target)
        return jsonify({"ok": True, "stage": "HEAD", "error_code": None}), 200
    except UpstreamError as e:
        return jsonify({"ok": False, "stage": e.stage, "error_code": e.error_code}), 503
    except Exception:
        return jsonify({"ok": False, "stage": "health_upstream", "error_code": "UNKNOWN"}), 503


@app.route("/")
def home():
    cfg = load_cfg()
    tzname = cfg.get("timezone", "Australia/Brisbane")

    items = []
    for m in MOBILES:
        mcfg = cfg["mobiles"].get(m, {})
        enabled = bool(mcfg.get("enabled", False))
        next_change = get_next_scheduled_change(mcfg, tzname)

        cached = cache_get(m)
        if cached:
            line = cached.get("line", "Loading...")
            st = cached.get("status", "Loading...")
            error_code = cached.get("error_code")
            error_ts = cached.get("error_ts")
        else:
            line = "Loading..."
            st = "Loading..."
            error_code = None
            error_ts = None

        item = {
            "mobile": m,
            "display": display_name(m),
            "enabled": enabled,
            "line": line,
            "status": st,
            "status_class": _status_class(st),
            "error_code": error_code,
            "error_ts": error_ts,
        }
        if next_change:
            item.update(next_change)
        items.append(item)

    page = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALDI App</title>
<style>
body{font-family:Arial;background:#fafafa;padding:16px}
.card{background:#fff;border:1px solid #ddd;border-radius:10px;padding:14px;margin-bottom:12px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.pill{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;border:1px solid #ccc}
.pending{background:#fff7d6}
.ok{background:#eaffea}
.err{background:#ffecec}
.small{font-size:12px;color:#666;margin-top:6px}
input,button{width:100%;padding:10px;font-size:14px;border-radius:10px;border:1px solid #ccc;box-sizing:border-box}
button{background:#f3f3f3;cursor:pointer}
a{color:#1a5cff;text-decoration:none}

.spinner-overlay{display:none;position:fixed;inset:0;z-index:99999;background:rgba(255,255,255,0.85);justify-content:center;align-items:center}
.spinner{border:6px solid #f3f3f3;border-top:6px solid #3498db;border-radius:50%;width:50px;height:50px;animation:spin 1s linear infinite}
@keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}
</style>
</head>
<body>

<div id="spinnerOverlay" class="spinner-overlay">
  <div style="display:flex;flex-direction:column;align-items:center;gap:12px;">
    <div class="spinner"></div>
    <div id="progressText" style="font-size:14px;color:#333;text-align:center;max-width:340px;">Loading...</div>
  </div>
</div>

<h2 style="display:flex;align-items:center;gap:10px;margin:0 0 12px 0;">
  <svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M4 19V5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    <path d="M4 19H20" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    <path d="M8 16V10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    <path d="M12 16V7" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    <path d="M16 16V12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
  </svg>
  ALDI App
</h2>

<div class="card">
  <div class="row" style="justify-content:space-between;align-items:center;">
    <div class="row"><a href="/matrix-all">Scheduler</a></div>
    <button onclick="refreshNow()" style="width:auto;padding:8px 12px;border-radius:10px;">Refresh</button>
  </div>
  <div class="small">Cached values show instantly, then latest loads with step-by-step progress.</div>
</div>

{% for it in items %}
<div class="card">
  <div class="row">
    <b>{{it.display}}</b>
    <span class="pill">{{"Scheduler ON" if it.enabled else "Scheduler OFF"}}</span>

    <span id="status-{{it.mobile}}" class="pill {{it.status_class}}">{{it.status}}</span>

    {% if it.next_change_label and it.next_change_gb %}
      <span id="next-{{it.mobile}}" class="pill pending">Next: {{it.next_change_label}} → {{it.next_change_gb}} GB</span>
    {% endif %}
  </div>

  <div style="margin-top:8px;">
    <span id="line-{{it.mobile}}" style="font-weight:600">{{it.line}}</span>
  </div>

  {% if it.error_code %}
  <details id="error-box-{{it.mobile}}" style="margin-top:6px;">
    <summary style="cursor:pointer;color:#666;">Details</summary>
    <div id="details-{{it.mobile}}" class="small">Code: {{it.error_code}}{% if it.error_ts %} · {{it.error_ts}}{% endif %}</div>
  </details>
  {% else %}
  <details id="error-box-{{it.mobile}}" style="margin-top:6px;display:none;">
    <summary style="cursor:pointer;color:#666;">Details</summary>
    <div id="details-{{it.mobile}}" class="small"></div>
  </details>
  {% endif %}

  <form style="margin-top:10px;" onsubmit="return startUpdate(event, '{{it.mobile}}')">
    <input name="value" placeholder="Manual Update (GB) e.g. 0, 20, 999" required>
    <button type="submit" style="margin-top:8px;">Manual Update</button>
  </form>
</div>
{% endfor %}

<script>
function showOverlay(msg){
  const ov = document.getElementById("spinnerOverlay");
  ov.style.display="flex";
  document.getElementById("progressText").innerText = msg || "Working...";
}
function setProgress(msg){
  document.getElementById("progressText").innerText = msg || "Working...";
}
function hideOverlay(){
  document.getElementById("spinnerOverlay").style.display="none";
}

let activeOpId = null;

function statusClassFor(status){
  const s = (status || "").trim();
  if(s === "Error") return "err";
  if(s === "Pending" || s === "Loading...") return "pending";
  return "ok";
}

async function pollProgress(opId, onDone){
  activeOpId = opId;
  let lastSeq = 0;
  let lastMsg = "";
  const poll = async () => {
    try{
      const pr = await fetch(`/api/progress/${opId}`, {cache:"no-store"});
      const pj = await pr.json();
      const seq = Number(pj.seq || 0);
      const msg = pj.msg || "Working...";
      if(seq > lastSeq || msg !== lastMsg){
        setProgress(msg);
        lastSeq = seq;
        lastMsg = msg;
      }
      if(pj.done){
        activeOpId = null;
        if(!pj.ok){
          hideOverlay();
          alert((pj.result && pj.result.error) ? pj.result.error : "Operation failed");
          return;
        }
        if(typeof onDone === "function"){
          onDone(pj.result || {});
        }else{
          window.location.reload();
        }
        return;
      }
    }catch(e){}
    setTimeout(poll, 500);
  };
  poll();
}

function applyItemsToDom(items){
  for(const it of (items || [])){
    const lineEl = document.getElementById(`line-${it.mobile}`);
    if(lineEl) lineEl.innerText = it.line || "—";

    const stEl = document.getElementById(`status-${it.mobile}`);
    if(stEl){
      const st = it.status || "Loading...";
      stEl.textContent = st;
      stEl.classList.remove("pending","ok","err");
      stEl.classList.add(statusClassFor(st));
    }

    const detailsText = document.getElementById(`details-${it.mobile}`);
    const detailsBox = document.getElementById(`error-box-${it.mobile}`);
    if(it.error_code){
      if(detailsText) detailsText.innerText = `Code: ${it.error_code}${it.error_ts ? ` · ${it.error_ts}` : ""}`;
      if(detailsBox) detailsBox.style.display = "block";
    }else if(detailsBox){
      detailsBox.style.display = "none";
    }
  }
}

async function startUpdate(ev, mobile){
  ev.preventDefault();
  const value = (ev.target.querySelector('input[name="value"]').value || "").trim();
  if(!value) return false;

  showOverlay("Starting manual update...");
  const fd = new FormData();
  fd.append("mobile", mobile);
  fd.append("value", value);

  const r = await fetch("/api/set-now-start", {method:"POST", body: fd});
  const j = await r.json();
  if(!j.ok){
    hideOverlay();
    alert(j.error || "Failed to start update");
    return false;
  }
  pollProgress(j.op_id, () => window.location.reload());
  return false;
}

async function refreshNow(){
  showOverlay("Refreshing...");
  const r = await fetch("/api/refresh-start", {method:"POST"});
  const j = await r.json();
  if(!j.ok){
    hideOverlay();
    alert(j.error || "Failed to start refresh");
    return;
  }
  pollProgress(j.op_id, () => window.location.reload());
}

async function loadHomeStatus(){
  showOverlay("Loading latest usage...");
  try{
    const r = await fetch("/api/home-status-start", {method:"POST"});
    const j = await r.json();
    if(!j.ok) throw new Error(j.error || "Failed to load status");

    pollProgress(j.op_id, (result) => {
      applyItemsToDom(result.items || []);
      hideOverlay();
    });
  }catch(e){
    console.error(e);
    hideOverlay();
  }
}

window.addEventListener("load", () => loadHomeStatus());
window.addEventListener("pageshow", (e) => { if (e.persisted && !activeOpId) hideOverlay(); });
</script>

</body>
</html>
"""
    return render_template_string(page, items=items)


@app.route("/api/progress/<op_id>")
def api_progress(op_id):
    d = _progress_read_all()
    st = d.get(op_id)
    if not isinstance(st, dict):
        return jsonify({"ok": False, "done": True, "msg": "Unknown operation", "seq": 0, "result": {"error": "Unknown operation"}})
    return jsonify({"ok": st.get("ok", True), "done": st.get("done", False), "msg": st.get("msg", ""), "seq": st.get("seq", 0), "result": st.get("result")})


@app.post("/api/home-status-start")
def api_home_status_start():
    op_id = progress_init("Preparing...")

    def worker():
        cfg = load_cfg()
        tzname = cfg.get("timezone", "Australia/Brisbane")
        out = []

        try:
            progress_set(op_id, "Loading limits...")
            limits = get_limits(op_id, force=False)

            progress_set(op_id, f"Fetching balances (0/{len(MOBILES)})...")
            results: dict[str, dict] = {}

            with ThreadPoolExecutor(max_workers=BALANCE_WORKERS) as ex:
                futs = {ex.submit(fetch_balance_json, m, op_id): m for m in MOBILES}
                done_count = 0
                for fut in as_completed(futs):
                    m = futs[fut]
                    try:
                        results[m] = fut.result()
                    except Exception as e:
                        results[m] = {"_error": public_error_message(e), "_exc": str(e)}
                    done_count += 1
                    progress_set(op_id, f"Fetching balances ({done_count}/{len(MOBILES)})...")

            for m in MOBILES:
                mcfg = cfg["mobiles"].get(m, {})
                enabled = bool(mcfg.get("enabled", False))
                next_change = get_next_scheduled_change(mcfg, tzname)

                err_code = None
                err_ts = None
                try:
                    js = results.get(m) or {}
                    if "_error" in js:
                        raise Exception(js["_error"])
                    line = build_line_for_mobile(m, limits, js)
                    status = now_ts_str(tzname)
                    cache_set(m, line, status)
                except Exception as e:
                    line = f"Error: {public_error_message(e)}"
                    status = "Error"
                    err_code = "HOME_STATUS_FAIL"
                    err_ts = _error_timestamp()
                    cache_set(m, line, status, error_code=err_code, error_ts=err_ts)

                cached = cache_get(m) or {}
                item = {
                    "mobile": m,
                    "line": cached.get("line", line),
                    "status": cached.get("status", status),
                    "error_code": err_code,
                    "error_ts": err_ts,
                    "enabled": enabled,
                }
                if next_change:
                    item.update(next_change)
                out.append(item)

            progress_complete(op_id, {"items": out})
        except Exception as e:
            progress_done(op_id, False, {"error": public_error_message(e)})

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "op_id": op_id})


@app.post("/api/refresh-start")
def api_refresh_start():
    op_id = progress_init("Preparing...")

    def worker():
        cfg = load_cfg()
        tzname = cfg.get("timezone", "Australia/Brisbane")
        try:
            progress_set(op_id, "Loading limits (fresh)...")
            limits = get_limits(op_id, force=True)

            progress_set(op_id, f"Fetching balances (0/{len(MOBILES)})...")
            results: dict[str, dict] = {}

            with ThreadPoolExecutor(max_workers=BALANCE_WORKERS) as ex:
                futs = {ex.submit(fetch_balance_json, m, op_id): m for m in MOBILES}
                done_count = 0
                for fut in as_completed(futs):
                    m = futs[fut]
                    try:
                        results[m] = fut.result()
                    except Exception as e:
                        results[m] = {"_error": public_error_message(e), "_exc": str(e)}
                    done_count += 1
                    progress_set(op_id, f"Fetching balances ({done_count}/{len(MOBILES)})...")

            for m in MOBILES:
                try:
                    js = results.get(m) or {}
                    if "_error" in js:
                        raise Exception(js["_error"])
                    line = build_line_for_mobile(m, limits, js)
                    cache_set(m, line, now_ts_str(tzname))
                except Exception as e:
                    cache_set(m, f"Error: {public_error_message(e)}", "Error", error_code="REFRESH_FAIL", error_ts=_error_timestamp())

            progress_complete(op_id, {"refreshed": True})
        except Exception as e:
            progress_done(op_id, False, {"error": public_error_message(e)})

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "op_id": op_id})


@app.post("/api/set-now-start")
def api_set_now_start():
    mobile = (request.form.get("mobile") or "").strip()
    value = (request.form.get("value") or "").strip()

    if mobile not in MOBILES:
        return jsonify({"ok": False, "error": "Invalid mobile"}), 400
    if not value:
        return jsonify({"ok": False, "error": "Missing value"}), 400

    op_id = progress_init("Authenticating...")

    def worker():
        try:
            res = set_limit_and_wait(mobile, value, op_id=op_id)
            progress_complete(op_id, res)
        except Exception as e:
            progress_done(op_id, False, {"error": public_error_message(e)})

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "op_id": op_id})


@app.post("/matrix-copy-defaults")
def matrix_copy_defaults():
    data = request.get_json(silent=True) or request.form
    mobile = (data.get("mobile") or "").strip()

    if not mobile or mobile not in MOBILES:
        return jsonify(ok=False, error="invalid mobile"), 400

    cfg = load_cfg()
    cfg.setdefault("mobiles", {})
    cfg["mobiles"].setdefault(mobile, {"enabled": True, "default": empty_default_row(), "week": empty_week()})

    defaults = data.get("defaults")
    if not defaults:
        defaults = cfg["mobiles"][mobile].get("default") or empty_default_row()

    norm = []
    for i in range(4):
        try:
            slot = defaults[i]
        except Exception:
            slot = {"time": "", "value": ""}
        if not isinstance(slot, dict):
            slot = {"time": "", "value": ""}
        norm.append({"time": str(slot.get("time", "") or ""), "value": str(slot.get("value", "") or "")})

    week = cfg["mobiles"][mobile].setdefault("week", empty_week())
    for d in DAYS:
        week[d] = [dict(x) for x in norm]

    save_cfg(cfg)
    reload_jobs()
    return jsonify(ok=True)


@app.route("/matrix-all", methods=["GET", "POST"])
def matrix_all():
    cfg = load_cfg()
    tzname = cfg.get("timezone", "Australia/Brisbane")

    if request.method == "POST":
        cfg["timezone"] = (request.form.get("timezone") or "Australia/Brisbane").strip() or "Australia/Brisbane"

        for m in MOBILES:
            cfg["mobiles"][m]["enabled"] = (request.form.get(f"enabled_{m}") == "on")

            drow = []
            for i in range(4):
                t = (request.form.get(f"default_time_{m}_{i}") or "").strip()
                v = (request.form.get(f"default_value_{m}_{i}") or "").strip()
                drow.append({"time": t, "value": v})
            cfg["mobiles"][m]["default"] = drow

            week = {}
            for d in DAYS:
                slots = []
                for i in range(4):
                    t = (request.form.get(f"time_{m}_{d}_{i}") or "").strip()
                    v = (request.form.get(f"value_{m}_{d}_{i}") or "").strip()
                    slots.append({"time": t, "value": v})
                week[d] = slots
            cfg["mobiles"][m]["week"] = week

        save_cfg(cfg)
        reload_jobs()
        return redirect(url_for("matrix_all"))

    page = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scheduler</title>
<style>
body{font-family:Arial;background:#fafafa;padding:16px}
.card{background:#fff;border:1px solid #ddd;border-radius:10px;padding:14px;margin-bottom:12px}
table{width:100%;border-collapse:collapse;margin-top:8px}
th,td{border:1px solid #ddd;padding:6px;vertical-align:top}
th{background:#f3f3f3}
input[type="text"]{width:100%;padding:8px;border-radius:8px;border:1px solid #ccc;box-sizing:border-box}
.small{font-size:12px;color:#666}
.btn{display:inline-block;padding:10px 14px;border-radius:10px;border:1px solid #ccc;background:#f3f3f3;text-decoration:none;color:#000}
.savebtn{width:100%;padding:12px;border-radius:10px;border:1px solid #ccc;background:#f3f3f3;font-size:16px;cursor:pointer}
.flex{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.slot-table{min-width:560px}
.slot-table th,.slot-table td{min-width:105px}
.slot-table th:first-child,.slot-table td:first-child{min-width:90px}
</style>
</head>
<body>

<a class="btn" href="/">← Back</a>
<h2 style="margin-top:12px;">Scheduler</h2>

<div class="card small">
  <div><b>Server time:</b> {{server_utc}}</div>
  <div><b>App timezone:</b> {{tzname}} &nbsp; <b>Now:</b> {{cfg_now}}</div>
  <div style="margin-top:6px;">Use 24-hour time (HH:MM). Leave blank to disable a slot. Values are GB (e.g. 0, 20, 999).</div>
</div>

<form method="post">
  <div class="card">
    <b>Timezone</b><br>
    <input type="text" name="timezone" value="{{cfg.get('timezone','Australia/Brisbane')}}">
  </div>

  {% for m in mobiles %}
  {% set mcfg = (cfg.get("mobiles", {}) or {}).get(m, {}) %}
  {% set week = mcfg.get("week", {}) %}
  <div class="card">
    <div class="flex">
      <div><b>{{display_name(m)}}</b></div>
      <label>
        <input type="checkbox" name="enabled_{{m}}" {% if mcfg.get("enabled") %}checked{% endif %}>
        Enable schedule
      </label>
    </div>

    <div class="card">
      <b>Default (4 slots)</b>
      <div class="small">Set these 4 slots, then copy them down to every day.</div>

      <div class="table-scroll">
      <table class="slot-table">
        <tr>
          <th>Default</th>
          {% for i in range(4) %}<th>Slot {{i+1}}</th>{% endfor %}
        </tr>
        <tr>
          <td><b>All days</b></td>
          {% for i in range(4) %}
          {% set dslot = (mcfg.get("default", [])[i] if (mcfg.get("default", [])|length) > i else {}) %}
          <td>
            <input type="text" name="default_time_{{m}}_{{i}}" placeholder="HH:MM" value="{{dslot.get('time','')}}" id="default_time_{{m}}_{{i}}">
            <input type="text" name="default_value_{{m}}_{{i}}" placeholder="GB" value="{{dslot.get('value','')}}" id="default_value_{{m}}_{{i}}">
          </td>
          {% endfor %}
        </tr>
      </table>
      </div>

      <div style="margin-top:10px;">
        <button data-mobile="{{m}}" type="button" class="btn" onclick="copyDefaultsAndSave('{{m}}'); return false;">Copy Defaults → All Days</button>
      </div>
    </div>

    <div style="margin-top:12px;">
      <div class="table-scroll">
      <table class="slot-table">
        <tr>
          <th>Day</th>
          {% for i in range(4) %}<th>Slot {{i+1}}</th>{% endfor %}
        </tr>

        {% for d,label in days %}
        {% set slots = week.get(d, []) %}
        <tr>
          <td><b>{{label}}</b></td>
          {% for i in range(4) %}
          {% set slot = (slots[i] if (slots|length) > i else {}) %}
          <td>
            <input type="text" name="time_{{m}}_{{d}}_{{i}}" placeholder="HH:MM" value="{{slot.get('time','')}}" id="time_{{m}}_{{d}}_{{i}}">
            <input type="text" name="value_{{m}}_{{d}}_{{i}}" placeholder="GB" value="{{slot.get('value','')}}" id="value_{{m}}_{{d}}_{{i}}">
          </td>
          {% endfor %}
        </tr>
        {% endfor %}
      </table>
      </div>
    </div>
  </div>
  {% endfor %}

  <button class="savebtn" type="submit">Save schedule</button>
</form>

<script>
window.copyDefaults = function(mobile){
  try{
    const days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
    for(let i = 0; i < 4; i++){
      const dt = document.getElementById(`default_time_${mobile}_${i}`);
      const dv = document.getElementById(`default_value_${mobile}_${i}`);
      if(!dt || !dv){
        console.warn("[copyDefaults] missing default inputs", {mobile, i, dt: !!dt, dv: !!dv});
        return false;
      }
      const t = dt.value || "";
      const v = dv.value || "";
      for(const d of days){
        const ti = document.getElementById(`time_${mobile}_${d}_${i}`);
        const vi = document.getElementById(`value_${mobile}_${d}_${i}`);
        if(ti) ti.value = t;
        if(vi) vi.value = v;
      }
    }
    return false;
  }catch(e){
    console.error("copyDefaults failed", e);
    alert("Copy Defaults failed — open browser console for details.");
    return false;
  }
};

window.copyDefaultsAndSave = async function(mobile){
  try{
    if (typeof window.copyDefaults === "function") {
      window.copyDefaults(mobile);
    }

    const defaults = [];
    for (let i = 0; i < 4; i++) {
      const dt = document.getElementById(`default_time_${mobile}_${i}`);
      const dv = document.getElementById(`default_value_${mobile}_${i}`);
      defaults.push({ time: dt ? (dt.value || "") : "", value: dv ? (dv.value || "") : "" });
    }

    const r = await fetch("/matrix-copy-defaults", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({mobile: mobile, defaults: defaults})
    });

    let j = null;
    try { j = await r.json(); } catch(e) {}
    if(!r.ok || !j || !j.ok){
      console.error("matrix-copy-defaults failed", r.status, j);
      alert("Copy Defaults save failed. Check logs/console.");
      return false;
    }

    window.location.reload();
    return false;
  }catch(e){
    console.error("copyDefaultsAndSave failed", e);
    alert("Copy Defaults save failed — open console for details.");
    return false;
  }
};
</script>
</body>
</html>
"""
    return render_template_string(
        page,
        cfg=cfg,
        mobiles=MOBILES,
        days=list(zip(DAYS, DAY_LABELS)),
        tzname=tzname,
        server_utc=server_utc_str(),
        cfg_now=now_ts_str(tzname),
        display_name=display_name,
    )


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    start_scheduler()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
else:
    start_scheduler()