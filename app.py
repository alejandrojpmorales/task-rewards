import os
import json
import uuid
import base64
import random
import string
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Flask, redirect, request, session, jsonify, render_template, make_response
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Game-day helper — resets at 03:00 CET so late nights count as the same day
# ---------------------------------------------------------------------------
_CET = ZoneInfo("Europe/Amsterdam")

def game_today() -> str:
    """Return the current game-date string (YYYY-MM-DD).
    Before 03:00 CET the day hasn't turned over yet."""
    now = datetime.now(_CET)
    d = now.date() if now.hour >= 3 else now.date() - timedelta(days=1)
    return d.isoformat()

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
    "Class":                 "focus_class",
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
    "habit_important":    1.5,
    "focus_work":                 1.0,
    "focus_homework":             1.2,
    "focus_thesis":               2.0,
    "focus_long_meeting":         0.5,
    "focus_short_meeting":        0.3,
    "focus_digital_housekeeping": 0.5,
    "focus_cleaning":             0.5,
    "focus_morning_routine":      0.3,
    "focus_cooking":              0.3,
    "focus_class":                0.2,
}

DEFAULT_HABIT_LOW_COMPLETION_THRESHOLD = 15.0
DEFAULT_HABIT_LOW_COMPLETION_BONUS = 0.8

# Upstash Redis credentials (set in production env vars; absent = use local files)
UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

_DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))

DEFAULT_REWARDS = [
    {"id": str(uuid.uuid4()), "name": "5 min break",              "cost": 1},
    {"id": str(uuid.uuid4()), "name": "5 min break + snack",      "cost": 2},
    {"id": str(uuid.uuid4()), "name": "30 min walk",              "cost": 8},
    {"id": str(uuid.uuid4()), "name": "30 min gaming",            "cost": 12},
    {"id": str(uuid.uuid4()), "name": "30 min social media",      "cost": 12},
    {"id": str(uuid.uuid4()), "name": "30 min TV / YouTube",      "cost": 12},
    {"id": str(uuid.uuid4()), "name": "Full movie",               "cost": 20},
]

DEFAULT_PUNISHMENTS = [
    {"id": str(uuid.uuid4()), "name": "Used a reward for free",  "cost": 5},
    {"id": str(uuid.uuid4()), "name": "Opened a blocked app",    "cost": 10},
]

DEFAULT_MOOD_MULTIPLIERS = {
    "awesome":  1.0,
    "good":     1.1,
    "fine":     1.25,
    "bad":      1.35,
    "terrible": 1.5,
}

MOOD_ORDER = ["awesome", "good", "fine", "bad", "terrible"]


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


def habit_completion_percent(habit):
    """Return TickTick's habit completion percentage when the API exposes it."""
    for key in ("completionRate", "completionPercentage", "accomplishment", "progress"):
        value = habit.get(key)
        if isinstance(value, dict):
            value = value.get("percentage", value.get("percent", value.get("value")))
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 1:
            value *= 100
        if 0 <= value <= 100:
            return value

    count_pairs = (
        ("completedCount", "totalCount"),
        ("completedCycles", "totalCycles"),
        ("totalCheckIns", "targetDays"),
        ("completedCheckIns", "totalCheckIns"),
    )
    for completed_key, total_key in count_pairs:
        try:
            completed = float(habit.get(completed_key))
            total = float(habit.get(total_key))
        except (TypeError, ValueError):
            continue
        if total > 0 and 0 <= completed <= total:
            return completed / total * 100
    return None


def habit_score(habit, scoring, wallet):
    is_important = "❗" in (habit.get("name") or "")
    score_key = "habit_important" if is_important else "habit"
    score = scoring.get(score_key, DEFAULT_SCORING[score_key])
    breakdown = []
    if is_important:
        breakdown.append("❗ important habit")
    completion = habit_completion_percent(habit)
    threshold = wallet.get("habit_low_completion_threshold",
                           DEFAULT_HABIT_LOW_COMPLETION_THRESHOLD)
    bonus = wallet.get("habit_low_completion_bonus",
                       DEFAULT_HABIT_LOW_COMPLETION_BONUS)
    if (wallet.get("habit_low_completion_bonus_enabled", False)
            and completion is not None and completion < threshold):
        score += bonus
        breakdown.append(f"low completion ({completion:.0f}%) +{bonus}")
    return round(score, 1), breakdown, completion


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
        w.setdefault("inactivity_punished_at", None)  # kept for migration compat
        w.setdefault("inactivity_days_penalized", 0)
        w.setdefault("inactivity_reference_balance", None)
        w.setdefault("streak", 0)
        w.setdefault("last_active_date", None)
        w.setdefault("daily_goal", 8.0)
        w.setdefault("balance_cap", None)
        w.setdefault("active_multiplier", 1.0)
        w.setdefault("transactions", [])
        w.setdefault("last_break_at", None)
        w.setdefault("daily_break_count", 0)
        w.setdefault("daily_break_date", None)
        w.setdefault("max_breaks_per_day", 3)
        w.setdefault("custom_focus_names", {})
        w.setdefault("today_mood", None)
        w.setdefault("mood_date", None)
        w.setdefault("mood_multipliers", DEFAULT_MOOD_MULTIPLIERS.copy())
        for mood in MOOD_ORDER:
            w["mood_multipliers"].setdefault(mood, DEFAULT_MOOD_MULTIPLIERS[mood])
        w.setdefault("streak_daily_bonus_date", None)
        w.setdefault("habit_low_completion_bonus_enabled", False)
        w.setdefault("habit_low_completion_threshold", DEFAULT_HABIT_LOW_COMPLETION_THRESHOLD)
        w.setdefault("habit_low_completion_bonus", DEFAULT_HABIT_LOW_COMPLETION_BONUS)
        return w
    return {"balance": 0.0, "credited_date": "", "credited_today": 0.0,
            "rewards": DEFAULT_REWARDS, "punishments": DEFAULT_PUNISHMENTS,
            "scoring": DEFAULT_SCORING.copy(),
            "secure_folder": {"password": None, "active_unlock": None},
            "last_activity_at": None, "inactivity_punished_at": None,
            "streak": 0, "last_active_date": None, "daily_goal": 8.0,
            "balance_cap": None, "active_multiplier": 1.0, "transactions": [],
            "last_break_at": None, "daily_break_count": 0,
            "daily_break_date": None, "max_breaks_per_day": 3,
            "custom_focus_names": {}, "today_mood": None, "mood_date": None,
            "mood_multipliers": DEFAULT_MOOD_MULTIPLIERS.copy(),
            "streak_daily_bonus_date": None,
            "habit_low_completion_bonus_enabled": False,
            "habit_low_completion_threshold": DEFAULT_HABIT_LOW_COMPLETION_THRESHOLD,
            "habit_low_completion_bonus": DEFAULT_HABIT_LOW_COMPLETION_BONUS,
            }


def save_wallet(wallet: dict):
    kv_set("wallet", wallet)


def _slug(name: str) -> str:
    """Convert a timer display name to a scoring key, e.g. 'My Class' → 'focus_my_class'."""
    import re
    return "focus_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def resolve_focus_key(name: str, wallet: dict) -> str:
    """Return the scoring key for a focus timer name, auto-creating it if unknown."""
    # Check static map first
    if name in FOCUS_NAME_MAP:
        return FOCUS_NAME_MAP[name]
    # Check custom map
    custom = wallet.setdefault("custom_focus_names", {})
    if name in custom:
        return custom[name]
    # New timer — register it
    key = _slug(name)
    custom[name] = key
    wallet["scoring"].setdefault(key, 0.2)
    return key


def all_focus_names(wallet: dict) -> dict:
    """Return {scoring_key: display_name} for every known focus timer."""
    result = {v: k for k, v in FOCUS_NAME_MAP.items()}
    for display, key in wallet.get("custom_focus_names", {}).items():
        result[key] = display
    return result


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

def check_inactivity_punishment(wallet: dict, state: dict) -> dict:
    """Progressive inactivity penalty: −25% of balance per day without activity.
    Day 1 = 25%, day 2 = 50%, day 3 = 75%, day 4+ = 100%.
    Habits, tasks, and focus sessions all count as activity.
    Returns a dict: {applied, pct, amount, days}"""
    empty = {"applied": False, "pct": 0, "amount": 0.0, "days": 0}

    last_activity = wallet.get("last_activity_at")
    if not last_activity:
        return empty

    # Any items in today's stored state = user was active today (habits count!)
    all_items = state["tasks"] + state["habits"] + state.get("focuses", [])
    if all_items:
        # Reset inactivity tracking if there is already stored activity
        wallet["inactivity_days_penalized"] = 0
        wallet["inactivity_reference_balance"] = None
        return empty

    now = datetime.now(timezone.utc)
    last_dt = datetime.fromisoformat(last_activity)
    hours_inactive = (now - last_dt).total_seconds() / 3600

    if hours_inactive < 24:
        return empty  # Still within the 24 h grace period

    # How many complete 24 h periods have elapsed (cap at 4 = full wipe)
    days_inactive = min(int(hours_inactive / 24), 4)
    days_penalized = wallet.get("inactivity_days_penalized", 0)

    if days_inactive <= days_penalized:
        return empty  # Already applied for this many days

    # On the first penalty day, snapshot the reference balance
    if days_penalized == 0:
        wallet["inactivity_reference_balance"] = wallet.get("balance", 0.0)
        wallet["streak"] = 0  # break streak on day 1

    ref = wallet.get("inactivity_reference_balance") or wallet.get("balance", 0.0)

    # Cumulative penalty vs the reference balance, then subtract what's already been taken
    old_pct = min(days_penalized * 0.25, 1.0)
    new_pct = min(days_inactive  * 0.25, 1.0)
    penalty_amount = round((new_pct - old_pct) * ref, 1)

    wallet["balance"] = max(0.0, round(wallet.get("balance", 0.0) - penalty_amount, 1))
    wallet["inactivity_days_penalized"] = days_inactive
    wallet["inactivity_punished_at"] = now.isoformat()

    pct_label = int(new_pct * 100)
    desc = f"Inactivity day {days_inactive}: −{pct_label}% of balance 💀"
    add_transaction(wallet, "inactivity", desc, -penalty_amount)

    return {"applied": True, "pct": pct_label, "amount": penalty_amount, "days": days_inactive}


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
    today = game_today()
    start = f"{today}T00:00:00.000+0000"
    end   = f"{today}T23:59:59.000+0000"
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
    today = game_today()
    resp = requests.get(
        f"{BASE_URL}/focus",
        headers=headers,
        params={
            "from": f"{today}T00:00:00+0000",
            "to":   f"{today}T23:59:59+0000",
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

def estimate_task_hours(task: dict) -> tuple:
    """Return (min_hours, max_hours) estimate for a task.
    Calendar blocks (startDate != dueDate) return exact duration.
    Otherwise uses ⏱️ tag ranges."""
    start = task.get("startDate") or ""
    due   = task.get("dueDate")   or ""

    # Calendar block: both dates set, different times
    if start and due and start[:10] == due[:10] and start != due:
        try:
            from datetime import timezone as _tz
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            d = datetime.fromisoformat(due.replace("Z", "+00:00"))
            if d > s:
                h = round((d - s).total_seconds() / 3600, 2)
                return h, h
        except Exception:
            pass

    tags = task.get("tags") or []
    if "8⏱️" in tags: return 6.0, 8.0
    if "6⏱️" in tags: return 4.0, 6.0
    if "4⏱️" in tags: return 2.0, 4.0
    if "2⏱️" in tags: return 1.0, 2.0
    return 0.0, 1.0   # no tag → ≤ 1 h


def fetch_pending_tasks(headers, scoring=None):
    """Return all active tasks across all projects with estimated scores."""
    projects_resp = requests.get(f"{BASE_URL}/project", headers=headers)
    if not projects_resp.ok:
        return None, f"projects failed {projects_resp.status_code}"
    projects = projects_resp.json()
    if not isinstance(projects, list):
        return [], None

    today_str = game_today()
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
            due = task.get("dueDate") or ""
            start = task.get("startDate") or ""
            is_today = due[:10] == today_str if due else False
            is_calendar = bool(start and due and start != due and start[:10] == today_str)
            h_min, h_max = estimate_task_hours(task)
            results.append({
                "id": task.get("id"),
                "title": task.get("title", "Untitled"),
                "project": pname,
                "priority": task.get("priority", 0),
                "tags": task.get("tags") or [],
                "score": score,
                "breakdown": breakdown,
                "dueDate": due,
                "startDate": start,
                "is_today": is_today or is_calendar,
                "is_calendar": is_calendar,
                "hours_min": h_min,
                "hours_max": h_max,
            })

    results.sort(key=lambda t: t["score"], reverse=True)
    return results, None


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    resp = make_response(render_template("index.html", logged_in="access_token" in session))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/sw.js")
def service_worker():
    sw_code = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({type:'window'}).then(list => {
    if (list.length) return list[0].focus();
    return clients.openWindow('/');
  }));
});
"""
    from flask import Response
    return Response(sw_code, mimetype="application/javascript",
                    headers={"Service-Worker-Allowed": "/",
                             "Cache-Control": "no-cache"})


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

    today = game_today()
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
            score, breakdown, completion = habit_score(habit, scoring, wallet)
            state["habits"].append({
                "id": hid, "title": habit.get("name", "Habit"),
                "score": score, "breakdown": breakdown, "type": "habit",
                "completion": completion,
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
        key = resolve_focus_key(name, wallet)
        score = scoring.get(key, wallet["scoring"].get(key, 0.2))
        state["focuses"].append({
            "id": fid, "title": name,
            "score": score, "breakdown": [], "type": "focus",
        })
        counted_focus_ids.add(fid)

    save_state(state)

    # Reset mood if it's a new day
    if wallet.get("mood_date") != today:
        wallet["today_mood"] = None
        wallet["mood_date"] = today
        # Reset multiplier to 1.0 when no mood is set yet
        if wallet.get("active_multiplier", 1.0) != 1.0:
            wallet["active_multiplier"] = 1.0

    all_items = state["tasks"] + state["habits"] + state["focuses"]
    multiplier = wallet.get("active_multiplier", 1.0)
    raw_total = round(sum(i["score"] for i in all_items), 1)
    today_total = round(raw_total * multiplier, 1)

    credit_points(wallet, today, today_total)

    # Activity = any completed task, habit, or focus (not just scored points)
    had_activity = bool(state["tasks"] or state["habits"] or state["focuses"])
    streak_bonus = update_streak(wallet, today, had_activity)

    if had_activity:
        wallet["last_activity_at"] = datetime.now(timezone.utc).isoformat()
        # Coming back after inactivity: reset penalty tracking so fresh points are safe
        wallet["inactivity_days_penalized"] = 0
        wallet["inactivity_reference_balance"] = None

    # Streak daily bonus: 1 pt × streak length per day when streak >= 7
    streak_daily_bonus = 0
    streak_val = wallet.get("streak", 0)
    if streak_val >= 7 and wallet.get("streak_daily_bonus_date") != today:
        streak_daily_bonus = streak_val
        wallet["balance"] = round(wallet.get("balance", 0) + streak_daily_bonus, 1)
        wallet["streak_daily_bonus_date"] = today
        add_transaction(wallet, "streak_bonus", f"Daily streak bonus ({streak_val}-day streak) 🔥", streak_daily_bonus)

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
        "streak_daily_bonus": streak_daily_bonus,
        "daily_goal": wallet.get("daily_goal", 8.0),
        "active_multiplier": multiplier,
        "today_mood": wallet.get("today_mood"),
        "mood_multipliers": wallet.get("mood_multipliers", DEFAULT_MOOD_MULTIPLIERS),
        "habit_low_completion_bonus_enabled": wallet.get("habit_low_completion_bonus_enabled", False),
        "habit_low_completion_threshold": wallet.get("habit_low_completion_threshold", DEFAULT_HABIT_LOW_COMPLETION_THRESHOLD),
        "habit_low_completion_bonus": wallet.get("habit_low_completion_bonus", DEFAULT_HABIT_LOW_COMPLETION_BONUS),
        "hours_since_activity": hours_since,
        "inactivity_punished": inactivity_punished.get("applied", False),
        "inactivity_info": inactivity_punished,
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

    today_tasks = [t for t in tasks if t.get("is_today")]
    total_min = round(sum(t["hours_min"] for t in today_tasks), 1)
    total_max = round(sum(t["hours_max"] for t in today_tasks), 1)
    calendar_count = sum(1 for t in today_tasks if t.get("is_calendar"))

    return jsonify({
        "tasks": tasks,
        "count": len(tasks),
        "today_count": len(today_tasks),
        "hours_min": total_min,
        "hours_max": total_max,
        "calendar_count": calendar_count,
    })


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
        "max_breaks_per_day": wallet.get("max_breaks_per_day", 3),
        "daily_break_count": wallet.get("daily_break_count", 0),
        "focus_names": all_focus_names(wallet),
        "mood_multipliers": wallet.get("mood_multipliers", DEFAULT_MOOD_MULTIPLIERS),
    })


@app.route("/api/config", methods=["PUT"])
def update_config():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    data = request.get_json() or {}
    incoming = data.get("scoring", {})
    wallet = load_wallet()
    # Build clean scoring: start with all DEFAULT_SCORING keys, then add any
    # custom focus keys already known in the wallet
    clean = {}
    all_keys = dict(DEFAULT_SCORING)
    for key in wallet.get("scoring", {}):
        if key.startswith("focus_") and key not in all_keys:
            all_keys[key] = 0.2  # default for unknown custom timers
    for key, default in all_keys.items():
        try:
            clean[key] = round(max(0.0, float(incoming.get(key, default))), 2)
        except (ValueError, TypeError):
            clean[key] = default
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
    try:
        wallet["max_breaks_per_day"] = max(1, int(data.get("max_breaks_per_day", wallet.get("max_breaks_per_day", 3))))
    except (ValueError, TypeError):
        pass
    incoming_mood = data.get("mood_multipliers", {})
    mood_mults = wallet.setdefault("mood_multipliers", DEFAULT_MOOD_MULTIPLIERS.copy())
    for mood, default in DEFAULT_MOOD_MULTIPLIERS.items():
        try:
            mood_mults[mood] = round(max(1.0, float(incoming_mood.get(mood, default))), 2)
        except (ValueError, TypeError):
            pass
    wallet["habit_low_completion_bonus_enabled"] = bool(
        data.get("habit_low_completion_bonus_enabled",
                 wallet.get("habit_low_completion_bonus_enabled", False))
    )
    try:
        wallet["habit_low_completion_threshold"] = min(
            100.0, max(0.0, float(data.get(
                "habit_low_completion_threshold",
                wallet.get("habit_low_completion_threshold",
                           DEFAULT_HABIT_LOW_COMPLETION_THRESHOLD)))))
    except (ValueError, TypeError):
        pass
    try:
        wallet["habit_low_completion_bonus"] = max(
            0.0, float(data.get(
                "habit_low_completion_bonus",
                wallet.get("habit_low_completion_bonus",
                           DEFAULT_HABIT_LOW_COMPLETION_BONUS))))
    except (ValueError, TypeError):
        pass
    save_wallet(wallet)
    return jsonify({
        "scoring": clean,
        "daily_goal": wallet["daily_goal"],
        "balance_cap": wallet["balance_cap"],
        "active_multiplier": wallet["active_multiplier"],
        "max_breaks_per_day": wallet["max_breaks_per_day"],
        "mood_multipliers": wallet["mood_multipliers"],
        "habit_low_completion_bonus_enabled": wallet["habit_low_completion_bonus_enabled"],
        "habit_low_completion_threshold": wallet["habit_low_completion_threshold"],
        "habit_low_completion_bonus": wallet["habit_low_completion_bonus"],
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

    # Break cooldown + daily budget enforcement
    is_break = int(reward.get("unlock_minutes") or 0) > 0
    if is_break:
        now = datetime.now(timezone.utc)
        today_str = now.date().isoformat()

        # Reset daily count if it's a new day
        if wallet.get("daily_break_date") != today_str:
            wallet["daily_break_count"] = 0
            wallet["daily_break_date"] = today_str

        # Check daily budget
        max_breaks = int(wallet.get("max_breaks_per_day") or 3)
        if wallet["daily_break_count"] >= max_breaks:
            return jsonify({
                "error": "daily_break_limit",
                "breaks_today": wallet["daily_break_count"],
                "max_breaks": max_breaks,
            }), 400

        # Check 25-min cooldown
        last_break_at = wallet.get("last_break_at")
        if last_break_at:
            elapsed = (now - datetime.fromisoformat(last_break_at)).total_seconds()
            cooldown = 25 * 60
            if elapsed < cooldown:
                minutes_left = int((cooldown - elapsed) // 60) + 1
                return jsonify({
                    "error": "break_cooldown",
                    "minutes_left": minutes_left,
                }), 400

        wallet["last_break_at"] = now.isoformat()
        wallet["daily_break_count"] = wallet.get("daily_break_count", 0) + 1

    wallet["balance"] = round(wallet["balance"] - reward["cost"], 1)
    add_transaction(wallet, "redeem", f"Redeemed: {reward['name']}", -reward["cost"])

    sf_status = None
    if reward.get("show_password") and wallet["secure_folder"].get("password"):
        unlock_minutes = int(reward.get("unlock_minutes") or 0) or 5
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=unlock_minutes)
        wallet["secure_folder"]["active_unlock"] = {
            "expires_at": expires_at.isoformat(),
            "reward_name": reward["name"],
        }
        sf_status = get_secure_folder_status(wallet)

    save_wallet(wallet)
    unlock_minutes_out = int(reward.get("unlock_minutes") or 0)
    return jsonify({
        "balance": wallet["balance"],
        "redeemed": reward["name"],
        "unlock_minutes": unlock_minutes_out,
        "secure_folder": sf_status,
        "breaks_today": wallet.get("daily_break_count", 0),
        "max_breaks_per_day": wallet.get("max_breaks_per_day", 3),
        "notif_enabled": reward.get("notif_enabled", True),
        "notif_title": reward.get("notif_title", ""),
        "notif_body": reward.get("notif_body", ""),
    })


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
            clean.append({
                "id": r.get("id") or str(uuid.uuid4()),
                "name": name, "cost": cost, "unlock_minutes": unlock_minutes,
                "show_password": bool(r.get("show_password", False)),
                "notif_enabled": bool(r.get("notif_enabled", True)),
                "notif_title": str(r.get("notif_title") or "").strip(),
                "notif_body":  str(r.get("notif_body")  or "").strip(),
            })
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

@app.route("/api/mood", methods=["POST"])
def set_mood():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    data = request.get_json() or {}
    mood = data.get("mood")
    if mood not in MOOD_ORDER:
        return jsonify({"error": "invalid mood"}), 400
    wallet = load_wallet()
    today = game_today()
    wallet["today_mood"] = mood
    wallet["mood_date"] = today
    multiplier = wallet["mood_multipliers"].get(mood, DEFAULT_MOOD_MULTIPLIERS[mood])
    wallet["active_multiplier"] = multiplier
    save_wallet(wallet)
    return jsonify({"mood": mood, "active_multiplier": multiplier})


@app.route("/api/history")
def get_history():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    today = date.fromisoformat(game_today())
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
        "daily_goal": wallet.get("daily_goal", 8.0),
        "transactions": list(reversed(wallet.get("transactions", [])[:30])),
    })


@app.route("/api/habits/map")
def habits_map():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401

    headers = {"Authorization": f"Bearer {session['access_token']}"}
    habits_list, err = fetch_habits(headers)
    if err or not habits_list:
        habits_list = []

    # Master list of habits (current ones in TickTick)
    all_habits = [
        {"id": h["id"], "name": h.get("name", "Habit")}
        for h in habits_list if h.get("id")
    ]
    habit_ids = {h["id"] for h in all_habits}

    today = date.fromisoformat(game_today())
    num_days = 21  # 3 weeks

    # Build day list oldest → newest
    days = [(today - timedelta(days=num_days - 1 - i)).isoformat() for i in range(num_days)]

    # checkins[habit_id] = list of booleans (one per day, oldest first)
    checkins = {h["id"]: [] for h in all_habits}

    for d in days:
        state = kv_get(f"state:{d}")
        completed_ids = set()
        if isinstance(state, dict) and state.get("date") == d:
            for item in state.get("habits", []):
                completed_ids.add(item.get("id", ""))
        for hid in habit_ids:
            checkins[hid].append(hid in completed_ids)

    return jsonify({
        "habits": all_habits,
        "days": days,
        "checkins": checkins,
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
