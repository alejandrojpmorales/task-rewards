import os
import json
import uuid
import base64
import random
import string
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, redirect, request, session, jsonify, render_template
import requests
from dotenv import load_dotenv

load_dotenv()

_ROOT = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_ROOT, "templates"))
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")

CLIENT_ID = os.environ.get("TICKTICK_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TICKTICK_CLIENT_SECRET")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:5000/callback")
BASE_URL = "https://api.ticktick.com/open/v1"

FROG_TAG = "🐸"
POMODORO_TAG_2 = "2⏱️"
POMODORO_TAG_4 = "4⏱️"
POMODORO_TAG_6 = "6⏱️"
POMODORO_TAG_8 = "8⏱️"
HIGH_PRIORITY = 5

# Maps TickTick timer name → scoring key
FOCUS_NAME_MAP = {
    "Work":                  "focus_work",
    "Homework":              "focus_homework",
    "Thesis":                "focus_thesis",
    "LONG meeting":          "focus_long_meeting",
    "SHORT meeting":         "focus_short_meeting",
    "Digital Housekeeping":  "focus_digital_housekeeping",
    "Cleaning and ordering": "focus_cleaning",
    "Morning routine":       "focus_morning_routine",
    "Cooking":               "focus_cooking",
}

DEFAULT_SCORING = {
    "base_task":          1.0,
    "priority_high":      0.5,
    "tag_frog":           0.8,
    "tag_pomo2":          0.3,
    "tag_pomo4":          0.8,
    "tag_pomo6":          1.2,
    "tag_pomo8":          1.6,
    "habit":              1.0,
    "focus_work":                 1.0,
    "focus_homework":             1.2,
    "focus_thesis":               2.0,
    "focus_long_meeting":         0.5,
    "focus_short_meeting":        0.3,
    "focus_digital_housekeeping": 0.5,
    "focus_cleaning":             0.5,
    "focus_morning_routine":      0.3,
    "focus_cooking":              0.3,
}

# Upstash Redis credentials (set in production env vars; absent = use local files)
UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

_DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))

DEFAULT_REWARDS = [
    {"id": str(uuid.uuid4()), "name": "5 mins break",             "cost": 1},
    {"id": str(uuid.uuid4()), "name": "5 mins break + snack",     "cost": 2},
    {"id": str(uuid.uuid4()), "name": "30 mins snack walk",       "cost": 8},
    {"id": str(uuid.uuid4()), "name": "30 mins of Grind",         "cost": 12},
    {"id": str(uuid.uuid4()), "name": "30 mins of Jack",          "cost": 12},
    {"id": str(uuid.uuid4()), "name": "30 mins of TV",            "cost": 12},
    {"id": str(uuid.uuid4()), "name": "30 mins of doomscrolling", "cost": 12},
]

DEFAULT_PUNISHMENTS = [
    {"id": str(uuid.uuid4()), "name": "Used a reward for free",  "cost": 5},
    {"id": str(uuid.uuid4()), "name": "Opened a blocked app",    "cost": 10},
]


# ---------------------------------------------------------------------------
# Storage abstraction — Upstash Redis in production, JSON files locally
# ---------------------------------------------------------------------------

def kv_get(key: str):
    if UPSTASH_URL:
        r = requests.post(
            UPSTASH_URL,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            json=["GET", key],
        )
        if r.ok:
            result = r.json().get("result")
            if result:
                return json.loads(result)
        return None
    else:
        path = _DATA_DIR / f"{key.replace(':', '_')}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None


def kv_set(key: str, value):
    if UPSTASH_URL:
        requests.post(
            UPSTASH_URL,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            json=["SET", key, json.dumps(value, ensure_ascii=False)],
        )
    else:
        path = _DATA_DIR / f"{key.replace(':', '_')}.json"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def task_score(task, scoring=None):
    s = scoring or DEFAULT_SCORING
    score = s.get("base_task", DEFAULT_SCORING["base_task"])
    breakdown = []
    if task.get("priority") == HIGH_PRIORITY:
        b = s.get("priority_high", DEFAULT_SCORING["priority_high"])
        score += b
        breakdown.append(f"high priority +{b}")
    tags = task.get("tags") or []
    if FROG_TAG in tags:
        b = s.get("tag_frog", DEFAULT_SCORING["tag_frog"])
        score += b
        breakdown.append(f"🐸 +{b}")
    for tag, key in [(POMODORO_TAG_2, "tag_pomo2"), (POMODORO_TAG_4, "tag_pomo4"), (POMODORO_TAG_6, "tag_pomo6"), (POMODORO_TAG_8, "tag_pomo8")]:
        if tag in tags:
            b = s.get(key, DEFAULT_SCORING[key])
            score += b
            breakdown.append(f"{tag} +{b}")
    return round(score, 1), breakdown


# ---------------------------------------------------------------------------
# Daily state
# ---------------------------------------------------------------------------

def load_state(today: str) -> dict:
    s = kv_get(f"state:{today}")
    if isinstance(s, dict) and s.get("date") == today:
        if "focuses" not in s:
            s["focuses"] = []
        return s
    return {"date": today, "tasks": [], "habits": [], "focuses": []}


def save_state(state: dict):
    kv_set(f"state:{state['date']}", state)


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------

def load_wallet() -> dict:
    w = kv_get("wallet")
    if isinstance(w, dict):
        if "scoring" not in w:
            w["scoring"] = DEFAULT_SCORING.copy()
        else:
            if "tag_pomo" in w["scoring"] and "tag_pomo4" not in w["scoring"]:
                w["scoring"]["tag_pomo4"] = w["scoring"].pop("tag_pomo")
            for key, default in DEFAULT_SCORING.items():
                w["scoring"].setdefault(key, default)
        w.setdefault("secure_folder", {"password": None, "active_unlock": None})
        w.setdefault("punishments", DEFAULT_PUNISHMENTS)
        w.setdefault("last_activity_at", None)
        w.setdefault("inactivity_punished_at", None)
        w.setdefault("streak", 0)
        w.setdefault("last_active_date", None)
        w.setdefault("daily_goal", 8.0)
        w.setdefault("balance_cap", None)
        w.setdefault("active_multiplier", 1.0)
        w.setdefault("transactions", [])
        return w
    return {"balance": 0.0, "credited_date": "", "credited_today": 0.0,
            "rewards": DEFAULT_REWARDS, "punishments": DEFAULT_PUNISHMENTS,
            "scoring": DEFAULT_SCORING.copy(),
            "secure_folder": {"password": None, "active_unlock": None},
            "last_activity_at": None, "inactivity_punished_at": None,
            "streak": 0, "last_active_date": None, "daily_goal": 8.0,
            "balance_cap": None, "active_multiplier": 1.0, "transactions": []}


def save_wallet(wallet: dict):
    kv_set("wallet", wallet)


def credit_points(wallet: dict, today: str, today_total: float):
    if wallet.get("credited_date") != today:
        wallet["credited_date"] = today
        wallet["credited_today"] = 0.0
    prev = wallet.get("credited_today", 0.0)
    if today_total > prev:
        wallet["balance"] = round(wallet.get("balance", 0.0) + (today_total - prev), 1)
        wallet["credited_today"] = today_total


# ---------------------------------------------------------------------------
# Streak & transactions
# ---------------------------------------------------------------------------

def update_streak(wallet: dict, today: str, had_activity: bool) -> int:
    """Update streak counter. Returns bonus points awarded (0 normally)."""
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    last = wallet.get("last_active_date")
    bonus = 0
    if had_activity:
        if last == today:
            pass  # already counted this session
        elif last == yesterday:
            wallet["streak"] = wallet.get("streak", 0) + 1
        else:
            wallet["streak"] = 1
        wallet["last_active_date"] = today
        streak = wallet["streak"]
        if streak > 0 and streak % 7 == 0:
            bonus = 5 if streak % 30 == 0 else 2
            wallet["balance"] = round(wallet.get("balance", 0) + bonus, 1)
            add_transaction(wallet, "streak_bonus",
                            f"{'30' if streak % 30 == 0 else '7'}-day streak bonus 🔥", bonus)
    else:
        if last and last < yesterday:
            wallet["streak"] = 0
    return bonus


def add_transaction(wallet: dict, type_: str, description: str, amount: float):
    txns = wallet.setdefault("transactions", [])
    txns.append({
        "ts":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "type":    type_,
        "desc":    description,
        "amount":  round(amount, 1),
        "balance": round(wallet.get("balance", 0), 1),
    })
    wallet["transactions"] = txns[-100:]


# ---------------------------------------------------------------------------
# Inactivity punishment
# ---------------------------------------------------------------------------

def check_inactivity_punishment(wallet: dict, state: dict) -> bool:
    """Wipe balance if no activity recorded in the last 24 h. Returns True if applied."""
    last_activity = wallet.get("last_activity_at")
    if not last_activity:
        return False
    all_items = state["tasks"] + state["habits"] + state.get("focuses", [])
    if all_items:
        return False
    now = datetime.now(timezone.utc)
    last_dt = datetime.fromisoformat(last_activity)
    if (now - last_dt).total_seconds() < 86400:
        return False
    punished_at = wallet.get("inactivity_punished_at")
    if punished_at and datetime.fromisoformat(punished_at) > last_dt:
        return False  # already wiped for this inactivity gap
    wallet["balance"] = 0.0
    wallet["credited_today"] = 0.0
    wallet["streak"] = 0
    wallet["inactivity_punished_at"] = now.isoformat()
    add_transaction(wallet, "inactivity", "24h inactivity — balance wiped 💀", 0)
    return True


# ---------------------------------------------------------------------------
# Secure Folder helpers
# ---------------------------------------------------------------------------

def get_secure_folder_status(wallet: dict) -> dict:
    sf = wallet.get("secure_folder") or {}
    unlock = sf.get("active_unlock")
    if not unlock:
        return {"unlocked": False, "password": None, "expires_at": None, "seconds_left": 0, "reward_name": None}
    expires_at = datetime.fromisoformat(unlock["expires_at"])
    now = datetime.now(timezone.utc)
    seconds_left = max(0, int((expires_at - now).total_seconds()))
    if seconds_left == 0:
        wallet["secure_folder"]["active_unlock"] = None
        return {"unlocked": False, "password": None, "expires_at": None, "seconds_left": 0, "reward_name": None}
    return {
        "unlocked": True,
        "password": sf.get("password"),
        "expires_at": unlock["expires_at"],
        "seconds_left": seconds_left,
        "reward_name": unlock.get("reward_name"),
    }


# ---------------------------------------------------------------------------
# TickTick helpers
# ---------------------------------------------------------------------------

def auth_headers():
    return {"Authorization": f"Bearer {session.get('access_token')}"}


def get_basic_auth():
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    return f"Basic {creds}"


def fetch_completed_tasks(headers):
    today = date.today()
    start = f"{today.isoformat()}T00:00:00.000+0000"
    end   = f"{today.isoformat()}T23:59:59.000+0000"
    resp = requests.post(
        f"{BASE_URL}/task/completed",
        headers=headers,
        json={"startDate": start, "endDate": end},
    )
    if not resp.ok:
        return None, f"task/completed failed {resp.status_code}: {resp.text}"
    data = resp.json()
    return data if isinstance(data, list) else [], None


def fetch_habits(headers):
    resp = requests.get(f"{BASE_URL}/habit", headers=headers)
    if not resp.ok:
        return None, f"habit list failed {resp.status_code}: {resp.text}"
    data = resp.json()
    return data if isinstance(data, list) else [], None


def fetch_pomodoros(headers):
    """GET /open/v1/focus — fetch completed pomodoros for today (type=0)."""
    today = date.today()
    resp = requests.get(
        f"{BASE_URL}/focus",
        headers=headers,
        params={
            "from": f"{today.isoformat()}T00:00:00+0000",
            "to":   f"{today.isoformat()}T23:59:59+0000",
            "type": 0,
        },
    )
    if not resp.ok:
        return None, f"focus failed {resp.status_code}: {resp.text}"
    data = resp.json()
    return data if isinstance(data, list) else [], None


def focus_name(focus: dict) -> str:
    """Extract the timer name from a focus record."""
    tasks = focus.get("tasks") or []
    if tasks and tasks[0].get("timerName"):
        return tasks[0]["timerName"]
    return focus.get("note") or ""


# ---------------------------------------------------------------------------
# Pending tasks
# ---------------------------------------------------------------------------

def fetch_pending_tasks(headers, scoring=None):
    """Return all active tasks across all projects with estimated scores."""
    projects_resp = requests.get(f"{BASE_URL}/project", headers=headers)
    if not projects_resp.ok:
        return None, f"projects failed {projects_resp.status_code}"
    projects = projects_resp.json()
    if not isinstance(projects, list):
        return [], None

    results = []
    for project in projects:
        pid = project.get("id")
        pname = project.get("name", "")
        resp = requests.get(f"{BASE_URL}/project/{pid}/data", headers=headers)
        if not resp.ok:
            continue
        data = resp.json()
        tasks = data.get("tasks", []) if isinstance(data, dict) else []
        for task in tasks:
            if task.get("status", 0) != 0:
                continue
            score, breakdown = task_score(task, scoring)
            results.append({
                "id": task.get("id"),
                "title": task.get("title", "Untitled"),
                "project": pname,
                "priority": task.get("priority", 0),
                "tags": task.get("tags") or [],
                "score": score,
                "breakdown": breakdown,
                "dueDate": task.get("dueDate", ""),
            })

    results.sort(key=lambda t: t["score"], reverse=True)
    return results, None


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", logged_in="access_token" in session)


@app.route("/login")
def login():
    auth_url = (
        "https://ticktick.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        "&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        "&scope=tasks:read"
    )
    return redirect(auth_url)


@app.route("/callback")
def callback():
    code = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        return f"Auth failed: {error or 'no code received'}", 400
    resp = requests.post(
        "https://ticktick.com/oauth/token",
        headers={"Authorization": get_basic_auth(), "Content-Type": "application/x-www-form-urlencoded"},
        data={"code": code, "grant_type": "authorization_code", "redirect_uri": REDIRECT_URI},
    )
    if not resp.ok:
        return f"Token exchange failed: {resp.text}", 400
    data = resp.json()
    session["access_token"] = data["access_token"]
    if "refresh_token" in data:
        session["refresh_token"] = data["refresh_token"]
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

@app.route("/api/score")
def get_score():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401

    today = date.today().isoformat()
    state = load_state(today)
    wallet = load_wallet()
    inactivity_punished = check_inactivity_punishment(wallet, state)
    headers = auth_headers()
    errors = []

    raw_tasks, err = fetch_completed_tasks(headers)
    if err:
        errors.append(err)
        raw_tasks = []

    scoring = wallet.get("scoring", DEFAULT_SCORING)
    counted_task_ids = {t["id"] for t in state["tasks"]}
    for task in raw_tasks:
        tid = task.get("id")
        if not tid or tid in counted_task_ids:
            continue
        score, breakdown = task_score(task, scoring)
        state["tasks"].append({
            "id": tid, "title": task.get("title", "Untitled"),
            "score": score, "breakdown": breakdown,
            "priority": task.get("priority", 0),
            "tags": task.get("tags") or [], "type": "task",
        })
        counted_task_ids.add(tid)

    habits, err = fetch_habits(headers)
    if err:
        errors.append(err)
        habits = []

    counted_habit_ids = {h["id"] for h in state["habits"]}
    for habit in habits:
        hid = habit.get("id")
        if not hid or hid in counted_habit_ids:
            continue
        modified_today = (habit.get("modifiedTime") or "")[:10] == today
        if modified_today and habit.get("totalCheckIns", 0) > 0:
            habit_score = scoring.get("habit", DEFAULT_SCORING["habit"])
            state["habits"].append({
                "id": hid, "title": habit.get("name", "Habit"),
                "score": habit_score, "breakdown": [], "type": "habit",
            })
            counted_habit_ids.add(hid)

    # Pomodoros
    pomodoros, err = fetch_pomodoros(headers)
    if err:
        errors.append(err)
        pomodoros = []

    counted_focus_ids = {f["id"] for f in state["focuses"]}
    for focus in pomodoros:
        fid = focus.get("id")
        if not fid or fid in counted_focus_ids:
            continue
        name = focus_name(focus)
        key = FOCUS_NAME_MAP.get(name)
        if key is None:
            continue
        score = scoring.get(key, DEFAULT_SCORING.get(key, 0))
        state["focuses"].append({
            "id": fid, "title": name,
            "score": score, "breakdown": [], "type": "focus",
        })
        counted_focus_ids.add(fid)

    save_state(state)

    all_items = state["tasks"] + state["habits"] + state["focuses"]
    multiplier = wallet.get("active_multiplier", 1.0)
    raw_total = round(sum(i["score"] for i in all_items), 1)
    today_total = round(raw_total * multiplier, 1)

    credit_points(wallet, today, today_total)

    streak_bonus = update_streak(wallet, today, today_total > 0)

    if today_total > 0:
        wallet["last_activity_at"] = datetime.now(timezone.utc).isoformat()

    cap = wallet.get("balance_cap")
    if cap and wallet["balance"] > cap:
        wallet["balance"] = float(cap)

    save_wallet(wallet)

    hours_since = None
    if wallet.get("last_activity_at"):
        diff = (datetime.now(timezone.utc) - datetime.fromisoformat(wallet["last_activity_at"])).total_seconds()
        hours_since = round(diff / 3600, 1)

    return jsonify({
        "date": today,
        "today_total": today_total,
        "task_count": len(state["tasks"]),
        "habit_count": len(state["habits"]),
        "focus_count": len(state["focuses"]),
        "items": sorted(all_items, key=lambda i: i["score"], reverse=True),
        "balance": wallet["balance"],
        "streak": wallet.get("streak", 0),
        "streak_bonus": streak_bonus,
        "daily_goal": wallet.get("daily_goal", 8.0),
        "active_multiplier": multiplier,
        "hours_since_activity": hours_since,
        "inactivity_punished": inactivity_punished,
        "errors": errors,
    })


# ---------------------------------------------------------------------------
# Wallet / Rewards
# ---------------------------------------------------------------------------

@app.route("/api/pending")
def get_pending():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    wallet = load_wallet()
    tasks, err = fetch_pending_tasks(auth_headers(), wallet.get("scoring", DEFAULT_SCORING))
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"tasks": tasks, "count": len(tasks)})


@app.route("/api/config", methods=["GET"])
def get_config():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    wallet = load_wallet()
    return jsonify({
        "scoring": wallet.get("scoring", DEFAULT_SCORING),
        "daily_goal": wallet.get("daily_goal", 8.0),
        "balance_cap": wallet.get("balance_cap"),
        "active_multiplier": wallet.get("active_multiplier", 1.0),
    })


@app.route("/api/config", methods=["PUT"])
def update_config():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    data = request.get_json() or {}
    incoming = data.get("scoring", {})
    clean = {}
    for key, default in DEFAULT_SCORING.items():
        try:
            clean[key] = round(max(0.0, float(incoming.get(key, default))), 2)
        except (ValueError, TypeError):
            clean[key] = default
    wallet = load_wallet()
    wallet["scoring"] = clean
    try:
        wallet["daily_goal"] = max(0.0, float(data.get("daily_goal", wallet.get("daily_goal", 8.0))))
    except (ValueError, TypeError):
        pass
    try:
        cap = data.get("balance_cap")
        wallet["balance_cap"] = max(0.0, float(cap)) if cap not in (None, "", 0) else None
    except (ValueError, TypeError):
        pass
    try:
        wallet["active_multiplier"] = max(1.0, float(data.get("active_multiplier", 1.0)))
    except (ValueError, TypeError):
        pass
    save_wallet(wallet)
    return jsonify({
        "scoring": clean,
        "daily_goal": wallet["daily_goal"],
        "balance_cap": wallet["balance_cap"],
        "active_multiplier": wallet["active_multiplier"],
    })


@app.route("/api/redeem", methods=["POST"])
def redeem():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    data = request.get_json() or {}
    wallet = load_wallet()
    reward = next((r for r in wallet["rewards"] if r["id"] == data.get("reward_id")), None)
    if not reward:
        return jsonify({"error": "reward not found"}), 404
    if wallet["balance"] < reward["cost"]:
        return jsonify({"error": "insufficient_balance", "balance": wallet["balance"]}), 400
    wallet["balance"] = round(wallet["balance"] - reward["cost"], 1)
    add_transaction(wallet, "redeem", f"Redeemed: {reward['name']}", -reward["cost"])

    sf_status = None
    if wallet["secure_folder"].get("password"):
        unlock_minutes = int(reward.get("unlock_minutes") or 0) or 30
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=unlock_minutes)
        wallet["secure_folder"]["active_unlock"] = {
            "expires_at": expires_at.isoformat(),
            "reward_name": reward["name"],
        }
        sf_status = get_secure_folder_status(wallet)

    save_wallet(wallet)
    return jsonify({"balance": wallet["balance"], "redeemed": reward["name"], "secure_folder": sf_status})


@app.route("/api/rewards", methods=["GET"])
def get_rewards():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    wallet = load_wallet()
    return jsonify({"rewards": wallet["rewards"], "balance": wallet["balance"]})


@app.route("/api/punish", methods=["POST"])
def punish():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    data = request.get_json() or {}
    wallet = load_wallet()
    punishment = next((p for p in wallet.get("punishments", []) if p["id"] == data.get("punishment_id")), None)
    if not punishment:
        return jsonify({"error": "punishment not found"}), 404
    wallet["balance"] = max(0.0, round(wallet["balance"] - punishment["cost"], 1))
    add_transaction(wallet, "punish", f"Punishment: {punishment['name']}", -punishment["cost"])
    save_wallet(wallet)
    return jsonify({"balance": wallet["balance"], "applied": punishment["name"]})


@app.route("/api/punishments", methods=["GET"])
def get_punishments():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    wallet = load_wallet()
    return jsonify({"punishments": wallet.get("punishments", []), "balance": wallet["balance"]})


@app.route("/api/punishments", methods=["PUT"])
def update_punishments():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    data = request.get_json() or {}
    clean = []
    for p in data.get("punishments", []):
        name = str(p.get("name", "")).strip()
        try:
            cost = float(p.get("cost", 1))
        except (ValueError, TypeError):
            cost = 1.0
        if name:
            clean.append({"id": p.get("id") or str(uuid.uuid4()), "name": name, "cost": cost})
    wallet = load_wallet()
    wallet["punishments"] = clean
    save_wallet(wallet)
    return jsonify({"punishments": wallet["punishments"]})


@app.route("/api/rewards", methods=["PUT"])
def update_rewards():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    data = request.get_json() or {}
    clean = []
    for r in data.get("rewards", []):
        name = str(r.get("name", "")).strip()
        try:
            cost = float(r.get("cost", 1))
        except (ValueError, TypeError):
            cost = 1.0
        try:
            unlock_minutes = max(0, int(r.get("unlock_minutes") or 0))
        except (ValueError, TypeError):
            unlock_minutes = 0
        if name:
            clean.append({"id": r.get("id") or str(uuid.uuid4()), "name": name, "cost": cost, "unlock_minutes": unlock_minutes})
    wallet = load_wallet()
    wallet["rewards"] = clean
    save_wallet(wallet)
    return jsonify({"rewards": wallet["rewards"]})


# ---------------------------------------------------------------------------
# Secure Folder routes
# ---------------------------------------------------------------------------

@app.route("/api/secure-folder", methods=["GET"])
def secure_folder_status():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    wallet = load_wallet()
    status = get_secure_folder_status(wallet)
    if not status["unlocked"] and wallet["secure_folder"].get("active_unlock") is None:
        pass
    else:
        save_wallet(wallet)
    has_password = bool(wallet["secure_folder"].get("password"))
    return jsonify({**status, "has_password": has_password})


@app.route("/api/secure-folder/password", methods=["PUT"])
def set_sf_password():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    data = request.get_json() or {}
    password = str(data.get("password", "")).strip()
    wallet = load_wallet()
    wallet["secure_folder"]["password"] = password if password else None
    save_wallet(wallet)
    return jsonify({"ok": True, "has_password": bool(password)})


@app.route("/api/secure-folder/generate", methods=["POST"])
def generate_sf_password():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    alphabet = string.ascii_letters  # a-z + A-Z
    password = "".join(random.choices(alphabet, k=10))
    wallet = load_wallet()
    wallet["secure_folder"]["password"] = password
    save_wallet(wallet)
    return jsonify({"ok": True, "password": password})


@app.route("/api/secure-folder/lock", methods=["POST"])
def lock_sf():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    wallet = load_wallet()
    wallet["secure_folder"]["active_unlock"] = None
    save_wallet(wallet)
    return jsonify({"ok": True, "unlocked": False})


# ---------------------------------------------------------------------------
# History & transactions
# ---------------------------------------------------------------------------

@app.route("/api/history")
def get_history():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    today = date.today()
    days = []
    for i in range(14):
        d = (today - timedelta(days=i)).isoformat()
        state = kv_get(f"state:{d}")
        if isinstance(state, dict) and state.get("date") == d:
            all_items = state.get("tasks", []) + state.get("habits", []) + state.get("focuses", [])
            days.append({
                "date": d,
                "total": round(sum(x["score"] for x in all_items), 1),
                "task_count":  len(state.get("tasks", [])),
                "habit_count": len(state.get("habits", [])),
                "focus_count": len(state.get("focuses", [])),
            })
        else:
            days.append({"date": d, "total": 0, "task_count": 0, "habit_count": 0, "focus_count": 0})
    wallet = load_wallet()
    return jsonify({
        "days": days,
        "streak": wallet.get("streak", 0),
        "transactions": list(reversed(wallet.get("transactions", [])[:30])),
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
