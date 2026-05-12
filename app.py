import os, json, re, urllib.parse
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

SNAPSHOTS_FILE  = os.path.join(os.path.dirname(__file__), "snapshots.json")
IG_INSIGHTS_FILE = os.path.join(os.path.dirname(__file__), "ig_insights.json")
STUDIO_FILE     = os.path.join(os.path.dirname(__file__), "studio_data.json")

# ── Goals ──────────────────────────────────────────────────────────────────────

GOALS = {
    "yt_personal_subs":         10000,
    "yt_makeugc_subs":           5000,
    "yt_makeugc_views_month":   10000,
    "yt_pagepilot_subs":         5000,
    "yt_pagepilot_views_month": 20000,
    "ig_main_followers":        10000,
    "ig_main_impressions_month":200000,
    "ig_second_followers":      10000,
    "ig_second_views_month":    350000,
    "tt_followers":             10000,
    "whop_revenue_month":       10000,
    "personal_cut_pct":          0.60,
}

def pct(current, goal_key):
    g = GOALS.get(goal_key, 1)
    return round(min((current or 0) / g * 100, 100), 1)


# ── Helpers ────────────────────────────────────────────────────────────────────

def now():
    return datetime.now(timezone.utc)

def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ── Snapshots (monthly delta tracking) ────────────────────────────────────────

def load_snapshots():
    if os.path.exists(SNAPSHOTS_FILE):
        with open(SNAPSHOTS_FILE) as f:
            return json.load(f)
    return []

def save_snapshot(flat):
    snaps = load_snapshots()
    today = now().strftime("%Y-%m-%d")
    snaps = [s for s in snaps if s["date"] != today]
    snaps.append({"date": today, "data": flat})
    snaps = snaps[-60:]
    with open(SNAPSHOTS_FILE, "w") as f:
        json.dump(snaps, f)

def monthly_delta(current, key):
    snaps = load_snapshots()
    cutoff = (now() - timedelta(days=30)).strftime("%Y-%m-%d")
    past = next((s for s in reversed(snaps) if s["date"] <= cutoff), None)
    if past is None and snaps:
        past = snaps[0]
    if past is None:
        return None
    old = past["data"].get(key)
    return (current - old) if old is not None else None


# ── YouTube ────────────────────────────────────────────────────────────────────

def fetch_youtube_channel(channel_id, snap_prefix):
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "statistics", "id": channel_id, "key": os.getenv("YOUTUBE_API_KEY")},
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        return None
    s = items[0]["statistics"]
    subs  = int(s.get("subscriberCount", 0))
    views = int(s.get("viewCount", 0))
    return {
        "subscribers":      subs,
        "total_views":      views,
        "videos":           int(s.get("videoCount", 0)),
        "delta_subscribers": monthly_delta(subs,  f"{snap_prefix}_subs"),
        "delta_views":       monthly_delta(views, f"{snap_prefix}_views"),
        "_snap_subs":        f"{snap_prefix}_subs",
        "_snap_views":       f"{snap_prefix}_views",
    }


# ── Instagram (public scrape) ─────────────────────────────────────────────────

def fetch_instagram_public(username, label):
    r = requests.get(
        f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
        headers={"User-Agent": "Mozilla/5.0", "x-ig-app-id": "936619743392459"},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    u = r.json().get("data", {}).get("user", {})
    if not u:
        return None
    followers = u.get("edge_followed_by", {}).get("count", 0)
    snap_key  = f"ig_{label}_followers"
    return {
        "username":          username,
        "followers":         followers,
        "posts":             u.get("edge_owner_to_timeline_media", {}).get("count", 0),
        "delta_followers":   monthly_delta(followers, snap_key),
        "_snap_key":         snap_key,
    }


# ── TikTok (public scrape) ─────────────────────────────────────────────────────

def fetch_tiktok():
    username = os.getenv("TIKTOK_USERNAME", "casprvrbk")
    r = requests.get(
        f"https://www.tiktok.com/@{username}",
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
        timeout=15,
    )
    m = re.search(r'"stats":\{"followerCount":(\d+),"followingCount":(\d+),"heart":(\d+),"heartCount":(\d+),"videoCount":(\d+)', r.text)
    if not m:
        return {"username": username, "error": "parse failed"}
    followers = int(m.group(1))
    return {
        "username":        username,
        "followers":       followers,
        "likes":           int(m.group(4)),
        "videos":          int(m.group(5)),
        "delta_followers": monthly_delta(followers, "tt_followers"),
    }


# ── Cal.com ────────────────────────────────────────────────────────────────────

def fetch_calcom():
    headers = {"Authorization": f"Bearer {os.getenv('CAL_API_KEY')}", "cal-api-version": "2024-08-13"}
    r = requests.get("https://api.cal.com/v2/bookings", headers=headers,
                     params={"take": 250, "sortStart": "desc"}, timeout=10)
    r.raise_for_status()
    bookings = r.json().get("data", [])

    today      = now().date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    # Build 7-day calendar
    week_days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        week_days.append({"day": d.strftime("%a"), "date": d.strftime("%-d"), "count": 0, "is_today": d == today})

    counts = {"today": 0, "this_week": 0, "this_month": 0}
    for b in bookings:
        if b.get("status") != "accepted":
            continue
        start = b.get("start", "")
        if not start:
            continue
        d = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
        if d == today:
            counts["today"] += 1
        if d >= week_start:
            counts["this_week"] += 1
            idx = (d - week_start).days
            if 0 <= idx < 7:
                week_days[idx]["count"] += 1
        if d >= month_start:
            counts["this_month"] += 1

    return {**counts, "week_days": week_days}


# ── Whop ───────────────────────────────────────────────────────────────────────

def fetch_whop():
    headers = {"Authorization": f"Bearer {os.getenv('WHOP_API_KEY')}"}
    biz = os.getenv("WHOP_BUSINESS_ID")

    r = requests.get("https://api.whop.com/api/v2/memberships", headers=headers,
                     params={"business_id": biz, "status": "active", "per": 50}, timeout=10)
    r.raise_for_status()
    data   = r.json()
    active = data.get("pagination", {}).get("total_count", len(data.get("data", [])))

    thirty_ago = int((now() - timedelta(days=30)).timestamp())
    r2 = requests.get("https://api.whop.com/api/v2/payments", headers=headers,
                      params={"business_id": biz, "after": thirty_ago, "per": 100}, timeout=10)
    r2.raise_for_status()
    payments = r2.json().get("data", [])

    month_ts = int(now().replace(day=1, hour=0, minute=0, second=0).timestamp())
    today_ts = int(now().replace(hour=0, minute=0, second=0).timestamp())

    rev_month = sum(p.get("final_amount", 0) for p in payments if p.get("created_at", 0) >= month_ts) / 100
    rev_today = sum(p.get("final_amount", 0) for p in payments if p.get("created_at", 0) >= today_ts) / 100

    return {
        "active_members":  active,
        "revenue_today":   rev_today,
        "revenue_month":   rev_month,
        "personal_cut":    round(rev_month * GOALS["personal_cut_pct"], 2),
    }


# ── Aggregate ──────────────────────────────────────────────────────────────────

def fetch_all():
    result = {"updated_at": now().isoformat(), "goals": GOALS}
    snap   = {}

    # Personal YouTube
    try:
        yt = fetch_youtube_channel(os.getenv("YOUTUBE_CHANNEL_ID"), "yt_personal")
        if yt:
            snap[yt["_snap_subs"]]  = yt["subscribers"]
            snap[yt["_snap_views"]] = yt["total_views"]
        result["youtube_personal"] = yt
    except Exception as e:
        result["youtube_personal"] = {"error": str(e)}

    # MakeUGC YouTube
    makeugc_id = os.getenv("YOUTUBE_MAKEUGC_CHANNEL_ID")
    if makeugc_id:
        try:
            yt2 = fetch_youtube_channel(makeugc_id, "yt_makeugc")
            if yt2:
                snap[yt2["_snap_subs"]]  = yt2["subscribers"]
                snap[yt2["_snap_views"]] = yt2["total_views"]
            # Merge studio data (monthly views from Chrome scrape)
            studio = load_json(STUDIO_FILE)
            if studio.get("makeugc_views_month") and yt2:
                yt2["monthly_views"] = studio["makeugc_views_month"]
            result["youtube_makeugc"] = yt2
        except Exception as e:
            result["youtube_makeugc"] = {"error": str(e)}
    else:
        result["youtube_makeugc"] = None

    # Pagepilot YouTube
    pagepilot_id = os.getenv("YOUTUBE_PAGEPILOT_CHANNEL_ID")
    if pagepilot_id:
        try:
            yt3 = fetch_youtube_channel(pagepilot_id, "yt_pagepilot")
            if yt3:
                snap[yt3["_snap_subs"]]  = yt3["subscribers"]
                snap[yt3["_snap_views"]] = yt3["total_views"]
            studio = load_json(STUDIO_FILE)
            if studio.get("pagepilot_views_month") and yt3:
                yt3["monthly_views"] = studio["pagepilot_views_month"]
            result["youtube_pagepilot"] = yt3
        except Exception as e:
            result["youtube_pagepilot"] = {"error": str(e)}
    else:
        result["youtube_pagepilot"] = None

    # Instagram
    ig_insights = load_json(IG_INSIGHTS_FILE)
    for key, username, label in [
        ("instagram_main",   "casperrverbeek", "main"),
        ("instagram_second", "casprvrbk",      "second"),
    ]:
        try:
            token = os.getenv(f"INSTAGRAM_ACCESS_TOKEN_{label.upper()}")
            ig = fetch_instagram_public(username, label)
            if ig:
                snap[ig["_snap_key"]] = ig["followers"]
                ig["monthly_views"]      = ig_insights.get(f"{label}_views")
                ig["monthly_reach"]      = ig_insights.get(f"{label}_reach")
                ig["monthly_impressions"]= ig_insights.get(f"{label}_impressions")
                ig["interactions"]       = ig_insights.get(f"{label}_interactions")
                ig["profile_visits"]     = ig_insights.get(f"{label}_profile_visits")
            result[key] = ig
        except Exception as e:
            result[key] = {"error": str(e)}

    # TikTok
    try:
        tt = fetch_tiktok()
        if "error" not in tt:
            snap["tt_followers"] = tt["followers"]
        result["tiktok"] = tt
    except Exception as e:
        result["tiktok"] = {"error": str(e)}

    # Cal.com
    try:
        result["calcom"] = fetch_calcom()
    except Exception as e:
        result["calcom"] = {"error": str(e)}

    # Whop
    try:
        result["whop"] = fetch_whop()
    except Exception as e:
        result["whop"] = {"error": str(e)}

    if snap:
        save_snapshot(snap)

    result["studio"] = load_json(STUDIO_FILE)
    return result


# ── WhatsApp ───────────────────────────────────────────────────────────────────

def send_whatsapp(message):
    phone   = os.getenv("WHATSAPP_PHONE", "").replace("+", "")
    api_key = os.getenv("CALLMEBOT_API_KEY")
    if not api_key:
        print("No CallMeBot key")
        return
    r = requests.get(
        f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={urllib.parse.quote(message)}&apikey={api_key}",
        timeout=15,
    )
    print(f"WhatsApp: {r.status_code}")


def motivational_line(d):
    yt    = d.get("youtube_personal") or {}
    whop  = d.get("whop") or {}
    subs  = yt.get("subscribers", 0)
    rev   = whop.get("revenue_month", 0)
    sub_pct = pct(subs, "yt_personal_subs")
    rev_pct = pct(rev,  "whop_revenue_month")

    if sub_pct >= 90:
        return "You're almost at 10K — one push and it's done 🏁"
    elif sub_pct >= 75:
        return "YouTube is looking strong, keep the uploads coming 🔥"
    elif sub_pct >= 50:
        return "Halfway on YouTube — consistency is everything right now 📈"
    else:
        if rev_pct >= 50:
            return "Revenue is solid — now double down on content 🎬"
        return "New week, new chance. Post today, results follow 💪"


def build_whatsapp_message(d):
    date_str      = now().strftime("%a %d %b")
    dashboard_url = os.getenv("DASHBOARD_URL", "")
    yt   = d.get("youtube_personal") or {}
    whop = d.get("whop") or {}
    cal  = d.get("calcom") or {}
    igm  = d.get("instagram_main") or {}
    tt   = d.get("tiktok") or {}

    subs     = yt.get("subscribers", 0)
    sub_pct  = pct(subs, "yt_personal_subs")
    rev      = whop.get("revenue_month", 0)
    rev_pct  = pct(rev, "whop_revenue_month")
    cut      = whop.get("personal_cut", 0)

    lines = [
        f"📊 {date_str}",
        dashboard_url,
        "",
        f"🎯 YT {subs:,} subs — {sub_pct}% to 10K",
        f"💰 Revenue ${rev:,.0f} — {rev_pct}% to $10K (your cut ${cut:,.0f})",
        f"📱 IG {igm.get('followers', 0):,} · TT {tt.get('followers', 0):,}",
        f"📅 {cal.get('today', 0)} call{'s' if cal.get('today', 0) != 1 else ''} today · {cal.get('this_month', 0)} this month",
        "",
        motivational_line(d),
    ]
    return "\n".join(lines)


def daily_job():
    print(f"Daily digest at {now()}")
    d = fetch_all()
    send_whatsapp(build_whatsapp_message(d))


# ── Studio data endpoints ──────────────────────────────────────────────────────

def load_ig_insights():
    return load_json(IG_INSIGHTS_FILE)

def save_ig_insights(data):
    existing = load_ig_insights()
    existing.update(data)
    existing["updated_at"] = now().isoformat()
    save_json(IG_INSIGHTS_FILE, existing)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("index.html")

@app.route("/api/data")
def api_data():
    return jsonify(fetch_all())

@app.route("/api/ig-insights", methods=["POST"])
def update_ig_insights():
    save_ig_insights(request.json)
    return jsonify({"status": "saved"})

@app.route("/api/studio", methods=["POST"])
def update_studio():
    existing = load_json(STUDIO_FILE)
    existing.update(request.json)
    existing["updated_at"] = now().isoformat()
    save_json(STUDIO_FILE, existing)
    return jsonify({"status": "saved"})

@app.route("/api/trigger")
def trigger():
    daily_job()
    return jsonify({"status": "sent"})


# ── Scheduler ──────────────────────────────────────────────────────────────────

def start_scheduler():
    scheduler = BackgroundScheduler(timezone="Europe/Madrid")
    scheduler.add_job(daily_job, "cron", hour=8, minute=0)
    scheduler.start()


if __name__ == "__main__":
    start_scheduler()
    app.run(debug=False, port=3000)
else:
    start_scheduler()
