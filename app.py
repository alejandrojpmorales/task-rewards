import os
import json
import uuid
import base64
from datetime import date
from pathlib import Path
from flask import Flask, redirect, request, session, jsonify, render_template
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")

CLIENT_ID = os.environ.get("TICKTICK_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TICKTICK_CLIENT_SECRET")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:5000/callback")
BASE_URL = "https://api.ticktick.com/open/v1"

FROG_TAG = "🐸"
POMODORO_TAG = "4⏱️"
HIGH_PRIORITY = 5

_DATA_DIR   = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
STATE_FILE  = _DATA_DIR / "state.json"
WALLET_FILE = _DATA_DIR / "wallet.json"

DEFAULT_REWARDS = [
    {"id": str(uuid.uuid4()), "name": "5 mins break",            "cost": 1},
    {"id": str(uuid.uuid4()), "name": "5 mins break + snack",    "cost": 2},
    {"id": str(uuid.uuid4()), "name": "30 mins snack walk",      "cost": 8},
    {"id": str(uuid.uuid4()), "name": "30 mins of Grind",        "cost": 12},
    {"id": str(uuid.uuid4()), "name": "30 mins of Jack",         "cost": 12},
    {"id": str(uuid.uuid4()), "name": "30 mins of TV",           "cost": 12},
    {"id": str(uuid.uuid4()), "name": "30 mins of doomscrolling","cost": 12},
]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def task_score(task):
    score = 1.0
    breakdown = []
    if task.get("priority") == HIGH_PRIORITY:
        score += 0.5
        breakdown.append("high priority +0.5")
    tags = task.get("tags") or []
    if FROG_TAG in tags:
        score += 0.8
        breakdown.append("🐸 +0.8")
    if POMODORO_TAG in tags:
        score += 0.8
        breakdown.append("4⏱️ +0.8")
    return round(score, 1), breakdown


# ---------------------------------------------------------------------------
# Daily state  (resets each day)
# ---------------------------------------------------------------------------

def load_state(today: str) -> dict:
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if s.get("date") == today:
                return s
        except Exception:
            pass
    return {"date": today, "tasks": [], "habits": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Wallet  (persistent balance + rewards list)
# ---------------------------------------------------------------------------

def load_wallet() -> dict:
    if WALLET_FILE.exists():
        try:
            return json.loads(WALLET_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"balance": 0.0, "credited_date": "", "credited_today": 0.0, "rewards": DEFAULT_REWARDS}


def save_wallet(wallet: dict):
    WALLET_FILE.write_text(json.dumps(wallet, ensure_ascii=False, indent=2), encoding="utf-8")


def credit_points(wallet: dict, today: str, today_total: float):
    """Add newly earned points to the persistent balance."""
    if wallet.get("credited_date") != today:
        wallet["credited_date"] = today
        wallet["credited_today"] = 0.0

    prev = wallet.get("credited_today", 0.0)
    if today_total > prev:
        wallet["balance"] = round(wallet.get("balance", 0.0) + (today_total - prev), 1)
        wallet["credited_today"] = today_total


# ---------------------------------------------------------------------------
# TickTick API helpers
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


# ---------------------------------------------------------------------------
# Routes — auth
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
# Routes — score
# ---------------------------------------------------------------------------

@app.route("/api/score")
def get_score():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401

    today = date.today().isoformat()
    state = load_state(today)
    wallet = load_wallet()
    headers = auth_headers()
    errors = []

    # Tasks
    raw_tasks, err = fetch_completed_tasks(headers)
    if err:
        errors.append(err)
        raw_tasks = []

    counted_task_ids = {t["id"] for t in state["tasks"]}
    for task in raw_tasks:
        tid = task.get("id")
        if not tid or tid in counted_task_ids:
            continue
        score, breakdown = task_score(task)
        state["tasks"].append({
            "id": tid, "title": task.get("title", "Untitled"),
            "score": score, "breakdown": breakdown,
            "priority": task.get("priority", 0),
            "tags": task.get("tags") or [], "type": "task",
        })
        counted_task_ids.add(tid)

    # Habits
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
            state["habits"].append({
                "id": hid, "title": habit.get("name", "Habit"),
                "score": 1.0, "breakdown": [], "type": "habit",
            })
            counted_habit_ids.add(hid)

    save_state(state)

    all_items = state["tasks"] + state["habits"]
    today_total = round(sum(i["score"] for i in all_items), 1)

    # Credit earned points to wallet balance
    credit_points(wallet, today, today_total)
    save_wallet(wallet)

    return jsonify({
        "date": today,
        "today_total": today_total,
        "task_count": len(state["tasks"]),
        "habit_count": len(state["habits"]),
        "items": sorted(all_items, key=lambda i: i["score"], reverse=True),
        "balance": wallet["balance"],
        "errors": errors,
    })


# ---------------------------------------------------------------------------
# Routes — wallet
# ---------------------------------------------------------------------------

@app.route("/api/redeem", methods=["POST"])
def redeem():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401

    data = request.get_json() or {}
    reward_id = data.get("reward_id")
    wallet = load_wallet()

    reward = next((r for r in wallet["rewards"] if r["id"] == reward_id), None)
    if not reward:
        return jsonify({"error": "reward not found"}), 404

    cost = reward["cost"]
    if wallet["balance"] < cost:
        return jsonify({"error": "insufficient_balance", "balance": wallet["balance"]}), 400

    wallet["balance"] = round(wallet["balance"] - cost, 1)
    save_wallet(wallet)
    return jsonify({"balance": wallet["balance"], "redeemed": reward["name"]})


@app.route("/api/rewards", methods=["GET"])
def get_rewards():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    wallet = load_wallet()
    return jsonify({"rewards": wallet["rewards"], "balance": wallet["balance"]})


@app.route("/api/rewards", methods=["PUT"])
def update_rewards():
    if "access_token" not in session:
        return jsonify({"error": "not_authenticated"}), 401

    data = request.get_json() or {}
    rewards = data.get("rewards", [])

    # Validate and assign IDs to new items
    clean = []
    for r in rewards:
        name = str(r.get("name", "")).strip()
        try:
            cost = float(r.get("cost", 1))
        except (ValueError, TypeError):
            cost = 1.0
        if name:
            clean.append({"id": r.get("id") or str(uuid.uuid4()), "name": name, "cost": cost})

    wallet = load_wallet()
    wallet["rewards"] = clean
    save_wallet(wallet)
    return jsonify({"rewards": wallet["rewards"]})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
