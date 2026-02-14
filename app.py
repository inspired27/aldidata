# -*- coding: utf-8 -*-
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import time, json, os
from datetime import datetime
import uuid, threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

ALDI_USERNAME = os.getenv("ALDI_USERNAME", "")
ALDI_PASSWORD = os.getenv("ALDI_PASSWORD", "")

OVERVIEW_URL   = "https://my.aldimobile.com.au/admin/s/5620272/shareddataoverview"
LOGIN_PAGE_URL = "https://my.aldimobile.com.au/login/"
LOGIN_POST_URL = "https://my.aldimobile.com.au/login_check"

MOBILES = ["0494584269"]
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "schedule_matrix.json")

POLL_INTERVAL_SECONDS = 2
POLL_TIMEOUT_SECONDS = 45
CACHE_TTL_SECONDS = 5

app = Flask(__name__)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

cache = {}

def now_ts_str(tzname: str) -> str:
    try:
        tz = pytz.timezone(tzname)
    except Exception:
        tz = pytz.timezone("Australia/Brisbane")
    return datetime.now(tz).strftime("%a %d %b %H:%M")

def cache_get(mobile: str):
    item = cache.get(mobile)
    if not item:
        return None
    if time.time() - item["ts"] > CACHE_TTL_SECONDS:
        return None
    return item

def cache_set(mobile: str, current: str, status: str):
    cache[mobile] = {"ts": time.time(), "current": current, "status": status}

# -----------------------
# Progress
# -----------------------
PROGRESS = {}
PROGRESS_LOCK = threading.Lock()

def progress_init(msg="Starting...") -> str:
    op_id = uuid.uuid4().hex
    with PROGRESS_LOCK:
        PROGRESS[op_id] = {"msg": msg, "done": False, "ok": False, "result": None, "ts": time.time()}
    return op_id

def progress_set(op_id: str | None, msg: str):
    if not op_id:
        return
    with PROGRESS_LOCK:
        if op_id in PROGRESS:
            PROGRESS[op_id]["msg"] = msg
            PROGRESS[op_id]["ts"] = time.time()

def progress_done(op_id: str | None, ok: bool, result=None):
    if not op_id:
        return
    with PROGRESS_LOCK:
        if op_id in PROGRESS:
            PROGRESS[op_id]["done"] = True
            PROGRESS[op_id]["ok"] = ok
            PROGRESS[op_id]["result"] = result
            PROGRESS[op_id]["ts"] = time.time()

# -----------------------
# Login helpers
# -----------------------
def looks_like_login_page(html: str) -> bool:
    return "login_password" in (html or "").lower()

def fetch(url: str) -> requests.Response:
    r = session.get(url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return r

def get_csrf_from_login_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    csrf = soup.find("input", attrs={"name": "_csrf_token"})
    return csrf.get("value") if csrf else None

def ensure_logged_in(op_id: str | None = None) -> str:
    progress_set(op_id, "Navigating to Shared Data Overview...")
    ov = fetch(OVERVIEW_URL)
    if not looks_like_login_page(ov.text):
        progress_set(op_id, "Already logged in.")
        return ov.text

    progress_set(op_id, "Opening login page...")
    lp = fetch(LOGIN_PAGE_URL)
    csrf = get_csrf_from_login_page(lp.text)
    if not csrf:
        lp2 = fetch(LOGIN_PAGE_URL)
        csrf = get_csrf_from_login_page(lp2.text)
    if not csrf:
        raise Exception("Could not find CSRF token on login page.")

    if not ALDI_USERNAME or not ALDI_PASSWORD:
        raise Exception("Missing ALDI_USERNAME or ALDI_PASSWORD env vars.")

    progress_set(op_id, "Logging into ALDI Mobile...")
    payload = {
        "login_user[login]": ALDI_USERNAME,
        "login_user[password]": ALDI_PASSWORD,
        "_csrf_token": csrf,
    }

    session.post(
        LOGIN_POST_URL,
        data=payload,
        headers={"Referer": LOGIN_PAGE_URL},
        allow_redirects=True,
        timeout=30,
    )

    progress_set(op_id, "Login submitted. Loading overview...")
    ov2 = fetch(OVERVIEW_URL)
    if looks_like_login_page(ov2.text):
        raise Exception("Login failed.")
    return ov2.text

# -----------------------
# Parse overview
# -----------------------
def get_panel_for_mobile(html: str, mobile: str):
    soup = BeautifulSoup(html, "html.parser")
    service_div = soup.find("div", attrs={"data-service_number": mobile})
    if not service_div:
        raise Exception(f"Mobile {mobile} not found on overview page.")
    panel = service_div.find_parent("div", class_="panel")
    if not panel:
        raise Exception("Panel not found.")
    return panel

def status_from_text(text: str, tzname: str) -> str:
    if "pending" in (text or "").lower():
        return "Pending"
    return now_ts_str(tzname)

def get_limit_text_and_status(mobile: str, op_id: str | None = None):
    cached = cache_get(mobile)
    if cached:
        return cached["current"], cached["status"]

    cfg = load_cfg()
    tzname = cfg.get("timezone", "Australia/Brisbane")

    progress_set(op_id, f"Loading usage limit for {mobile}...")
    html = ensure_logged_in(op_id=op_id)

    progress_set(op_id, f"Finding {mobile} in family plan...")
    panel = get_panel_for_mobile(html, mobile)

    div = panel.find("div", id=lambda x: x and x.startswith("usageLimitDivconsumerUsageLimit"))
    current = div.get_text(" ", strip=True) if div else "Unknown"
    status = status_from_text(current, tzname)

    cache_set(mobile, current, status)
    return current, status

# -----------------------
# Update limit
# -----------------------
def submit_limit_form(mobile: str, value: str, op_id: str | None = None):
    progress_set(op_id, "Ensuring session is logged in...")
    html = ensure_logged_in(op_id=op_id)

    progress_set(op_id, f"Finding {mobile} in family plan...")
    panel = get_panel_for_mobile(html, mobile)

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

    progress_set(op_id, f"Submitting usage limit update ({mobile} -> {value}GB)...")
    resp = session.post(
        post_url,
        data=payload,
        headers={"Referer": OVERVIEW_URL},
        allow_redirects=True,
        timeout=30,
    )
    resp.raise_for_status()
    cache.pop(mobile, None)

def wait_until_done(mobile: str, op_id: str | None = None):
    start = time.time()
    while True:
        current, status = get_limit_text_and_status(mobile, op_id=op_id)
        if status != "Pending":
            return True, current, round(time.time() - start, 1)

        progress_set(op_id, "Waiting for ALDI to finish pending update...")
        if time.time() - start > POLL_TIMEOUT_SECONDS:
            return False, current, round(time.time() - start, 1)

        cache.pop(mobile, None)
        time.sleep(POLL_INTERVAL_SECONDS)

def set_limit_and_wait(mobile: str, value: str, op_id: str | None = None):
    submit_limit_form(mobile, value, op_id=op_id)
    done, final_text, elapsed = wait_until_done(mobile, op_id=op_id)
    return {"mobile": mobile, "requested": value, "done": done, "final": final_text, "elapsed": elapsed}

# -----------------------
# Scheduler config
# -----------------------
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def empty_week():
    return {d: [{"time": "", "value": ""} for _ in range(4)] for d in DAYS}

def empty_default_row():
    return [{"time": "", "value": ""} for _ in range(4)]

def default_config():
    return {
        "timezone": "Australia/Brisbane",
        "mobiles": {
            m: {"enabled": True, "default": empty_default_row(), "week": empty_week()}
            for m in MOBILES
        }
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

# -----------------------
# APScheduler
# -----------------------
scheduler = None

def start_scheduler():
    global scheduler
    if scheduler is not None:
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
        print(f"[SCHEDULE] {mobile} -> {value} | done={res['done']} | final={res['final']} | {res['elapsed']}s")
    except Exception as e:
        print(f"[SCHEDULE] ERROR {mobile} -> {value}: {e}")

# -----------------------
# Routes
# -----------------------
@app.route("/")
def home():
    cfg = load_cfg()

    items = []
    for m in MOBILES:
        enabled = bool(cfg["mobiles"].get(m, {}).get("enabled", False))
        try:
            cur, st = get_limit_text_and_status(m, op_id=None)
        except Exception as e:
            cur, st = f"Error: {e}", "Error"
        items.append({"mobile": m, "enabled": enabled, "current": cur, "status": st})

    page = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALDI Data</title>
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

.spinner-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(255,255,255,0.75);display:none;justify-content:center;align-items:center;z-index:9999}
.spinner{border:6px solid #f3f3f3;border-top:6px solid #3498db;border-radius:50%;width:50px;height:50px;animation:spin 1s linear infinite}
@keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}
</style>
</head>
<body>

<div id="spinnerOverlay" class="spinner-overlay">
  <div style="display:flex;flex-direction:column;align-items:center;gap:12px;">
    <div class="spinner"></div>
    <div id="progressText" style="font-size:14px;color:#333;text-align:center;max-width:320px;">Working...</div>
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
  <div class="small">Use Refresh or Manual Update to pull new status. Progress messages appear during operations.</div>
</div>

{% for it in items %}
<div class="card">
  <div class="row">
    <b>{{it.mobile}}</b>
    <span class="pill">{{"Scheduler ON" if it.enabled else "Scheduler OFF"}}</span>
    {% if it.status == "Pending" %}
      <span class="pill pending">Pending</span>
    {% elif it.status == "Error" %}
      <span class="pill err">Error</span>
    {% else %}
      <span class="pill ok">{{it.status}}</span>
    {% endif %}
  </div>
  <div style="margin-top:6px;">Current: <span>{{it.current}}</span></div>

  <form style="margin-top:10px;" onsubmit="return startUpdate(event, '{{it.mobile}}')">
    <input name="value" placeholder="Manual Update (GB) e.g. 0, 20, 999" required>
    <button type="submit" style="margin-top:8px;">Manual Update</button>
  </form>
</div>
{% endfor %}

<script>
function showOverlay(msg){
  document.getElementById("spinnerOverlay").style.display="flex";
  document.getElementById("progressText").innerText = msg || "Working...";
}
function setProgress(msg){
  document.getElementById("progressText").innerText = msg || "Working...";
}
function hideOverlay(){ document.getElementById("spinnerOverlay").style.display="none"; }

async function pollProgress(opId){
  const poll = async () => {
    try{
      const pr = await fetch(`/api/progress/${opId}`, {cache:"no-store"});
      const pj = await pr.json();
      setProgress(pj.msg || "Working...");
      if(pj.done){
        if(!pj.ok){
          hideOverlay();
          alert((pj.result && pj.result.error) ? pj.result.error : "Operation failed");
          return;
        }
        window.location.reload();
        return;
      }
    }catch(e){}
    setTimeout(poll, 500);
  };
  poll();
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
  pollProgress(j.op_id);
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
  pollProgress(j.op_id);
}
</script>


<script>
function copyDefaults(mobile){
  try{
    console.log("[copyDefaults] clicked for", mobile);

    const days = ["mon","tue","wed","thu","fri","sat","sun"];
    for(let i=0;i<4;i++){
      const dt = document.getElementById(`default_time_${mobile}_${i}`);
      const dv = document.getElementById(`default_value_${mobile}_${i}`);

      if(!dt || !dv){
        console.warn("[copyDefaults] missing default inputs", {mobile, i, dt:!!dt, dv:!!dv});
        return;
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

    console.log("[copyDefaults] done for", mobile);
  }catch(e){
    console.error("[copyDefaults] error", e);
    alert("Copy Defaults failed — open browser console for details.");
  }
}_${i}`);
    const dv = document.getElementById(`default_value_${mobile}_${i}`);

    // If default inputs are missing, do nothing (prevents wiping schedules)
    if(!dt || !dv){
      console.warn("Default inputs missing for mobile:", mobile);
      return;
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
}_${i}`);
    const dv = document.getElementById(`default_value_${mobile}_${i}`);
    const t = dt ? dt.value : "";
    const v = dv ? dv.value : "";
    for(const d of days){
      const ti = document.getElementById(`time_${mobile}_${d}_${i}`);
      const vi = document.getElementById(`value_${mobile}_${d}_${i}`);
      if(ti) ti.value = t;
      if(vi) vi.value = v;
    }
  }
}_${i}"]`)?.value || "";
    const v = document.querySelector(`input[name="default_value_${mobile}_${i}"]`)?.value || "";
    // Copy into each day slot
    ["mon","tue","wed","thu","fri","sat","sun"].forEach(d=>{
      const ti = document.querySelector(`input[name="time_${mobile}_${d}_${i}"]`);
      const vi = document.querySelector(`input[name="value_${mobile}_${d}_${i}"]`);
      if(ti) ti.value = t;
      if(vi) vi.value = v;
    });
  }
}
</script>
<script>

document.addEventListener("click", function(e){
  const btn = e.target.closest("button");
  if(!btn) return;

  const txt = (btn.textContent || "").toLowerCase();
  if(!txt.includes("copy defaults")) return;

  // Never submit forms
  e.preventDefault();

  const mobile = btn.getAttribute("data-mobile") || btn.dataset.mobile;
  if(!mobile){
    console.warn("[copyDefaults] button missing data-mobile");
    return;
  }
  copyDefaults(mobile);
});

</script>

<script>
// Global: Copy Defaults -> All Days
// (fills the form inputs; you still need to click Save to persist)
window.copyDefaults = function(mobile){
  try{
    for(let i=0;i<4;i++){
      const dt = document.getElementById(`default_time_${mobile}_${i}`);
      const dv = document.getElementById(`default_value_${mobile}_${i}`);
      if(!dt || !dv){
        console.warn("Default inputs missing", {mobile, i});
        alert("Default row inputs not found on page.");
        return false;
      }
      const t = dt.value || "";
      const v = dv.value || "";

      // Copy into all day rows for this mobile + slot index
      document.querySelectorAll(`input[id^="time_${mobile}_"][id$="_${i}"]`).forEach(el => el.value = t);
      document.querySelectorAll(`input[id^="value_${mobile}_"][id$="_${i}"]`).forEach(el => el.value = v);
    }
    return false;
  }catch(e){
    console.error("copyDefaults failed", e);
    alert("Copy Defaults failed — open browser console for details.");
    return false;
  }
};
</script>


<script>
window.copyDefaultsAndSave = async function(mobile){
  try{
    if (typeof window.copyDefaults === "function") {
      window.copyDefaults(mobile);
    }

    const defaults = [];
    for (let i=0;i<4;i++){
      const dt = document.getElementById("default_time_" + mobile + "_" + i);
      const dv = document.getElementById("default_value_" + mobile + "_" + i);
      defaults.push({
        time: dt ? (dt.value || "") : "",
        value: dv ? (dv.value || "") : ""
      });
    }

    const r = await fetch("/matrix-copy-defaults", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({mobile: mobile, defaults: defaults})
    });

    let j = null;
    try { j = await r.json(); } catch(e) {}

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
    return render_template_string(page, items=items)

@app.route("/api/progress/<op_id>")
def api_progress(op_id):
    with PROGRESS_LOCK:
        st = PROGRESS.get(op_id)
    if not st:
        return jsonify({"ok": False, "done": True, "msg": "Unknown operation", "result": {"error": "Unknown operation"}})
    return jsonify({"ok": st["ok"], "done": st["done"], "msg": st["msg"], "result": st["result"]})

@app.route("/api/set-now-start", methods=["POST"])
def api_set_now_start():
    mobile = (request.form.get("mobile") or "").strip()
    value = (request.form.get("value") or "").strip()

    if mobile not in MOBILES:
        return jsonify({"ok": False, "error": "Invalid mobile"}), 400
    if not value:
        return jsonify({"ok": False, "error": "Missing value"}), 400

    op_id = progress_init("Starting manual update...")

    def worker():
        try:
            res = set_limit_and_wait(mobile, value, op_id=op_id)
            progress_set(op_id, "Done.")
            progress_done(op_id, True, res)
        except Exception as e:
            progress_done(op_id, False, {"error": str(e)})

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "op_id": op_id})

@app.route("/api/refresh-start", methods=["POST"])
def api_refresh_start():
    op_id = progress_init("Refreshing data...")

    def worker():
        try:
            for i, m in enumerate(MOBILES, start=1):
                progress_set(op_id, f"Refreshing {m} ({i}/{len(MOBILES)})...")
                try:
                    get_limit_text_and_status(m, op_id=op_id)
                except Exception as e:
                    cache_set(m, f"Error: {e}", "Error")
            progress_done(op_id, True, {"refreshed": True})
        except Exception as e:
            progress_done(op_id, False, {"error": str(e)})

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
        norm.append({
            "time": str(slot.get("time", "") or ""),
            "value": str(slot.get("value", "") or "")
        })

    week = cfg["mobiles"][mobile].setdefault("week", empty_week())
    for d in DAYS:
        week[d] = [dict(x) for x in norm]

    save_cfg(cfg)
    return jsonify(ok=True)

@app.route("/matrix-all", methods=["GET", "POST"])
def matrix_all():
    # --- FIX: default mobile if none provided so GET /matrix-all never returns JSON ---
    # Some code paths validate 'mobile' and return {"ok":false,"error":"invalid mobile"}.
    # Ensure we always have a valid mobile for GET renders.
    mobile = request.values.get("mobile") or ""
    mobile = mobile.strip()
    if not mobile:
        mobile = MOBILES[0] if MOBILES else ""
    # --- END FIX ---

    cfg = load_cfg()

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
        cache.clear()
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
  Use 24-hour time (HH:MM). Leave blank to disable a slot. Values are GB (e.g. 0, 20, 999).
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
      <div><b>{{m}}</b></div>
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
      defaults.push({
        time: dt ? (dt.value || "") : "",
        value: dv ? (dv.value || "") : ""
      });
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
    return render_template_string(page, cfg=cfg, mobiles=MOBILES, days=list(zip(DAYS, DAY_LABELS)))

if __name__ == "__main__":
    start_scheduler()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
else:
    start_scheduler()
