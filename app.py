import os, json, re, urllib.parse
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
SNAPSHOTS_FILE = os.path.join(os.path.dirname(__file__), "snapshots.json")


# ── Snapshots (monthly delta tracking) ────────────────────────────────────────

def load_snapshots():
    if os.path.exists(SNAPSHOTS_FILE):
        with open(SNAPSHOTS_FILE) as f:
            return json.load(f)
    return []

def save_snapshot(flat):
    snaps = load_snapshots()
    today = now().strftime("%Y-%m-%d")
    snaps = [s for s in snaps if s["date"] != today]  # replace today's entry
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

def now():
    return datetime.now(timezone.utc)


# ── YouTube ────────────────────────────────────────────────────────────────────

def fetch_youtube():
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "statistics", "id": os.getenv("YOUTUBE_CHANNEL_ID"), "key": os.getenv("YOUTUBE_API_KEY")},
        timeout=10,
    )
    r.raise_for_status()
    s = r.json()["items"][0]["statistics"]
    subs = int(s.get("subscriberCount", 0))
    views = int(s.get("viewCount", 0))
    return {
        "subscribers": subs,
        "total_views": views,
        "videos": int(s.get("videoCount", 0)),
        "delta_subscribers": monthly_delta(subs, "yt_subs"),
        "delta_views": monthly_delta(views, "yt_views"),
    }


# ── Instagram ─────────────────────────────────────────────────────────────────

def fetch_instagram(token, label):
    if not token:
        return None
    pages = requests.get(
        "https://graph.facebook.com/v19.0/me/accounts",
        params={"access_token": token}, timeout=10,
    ).json().get("data", [])
    if not pages:
        return None
    page_id = pages[0]["id"]
    page_token = pages[0]["access_token"]
    ig_id = requests.get(
        f"https://graph.facebook.com/v19.0/{page_id}",
        params={"fields": "instagram_business_account", "access_token": page_token}, timeout=10,
    ).json().get("instagram_business_account", {}).get("id")
    if not ig_id:
        return None

    profile = requests.get(
        f"https://graph.facebook.com/v19.0/{ig_id}",
        params={"fields": "followers_count,media_count,username", "access_token": page_token}, timeout=10,
    ).json()

    # Monthly reach via insights
    since = int((now() - timedelta(days=30)).timestamp())
    until = int(now().timestamp())
    insights = requests.get(
        f"https://graph.facebook.com/v19.0/{ig_id}/insights",
        params={"metric": "reach,impressions", "period": "day", "since": since, "until": until, "access_token": page_token},
        timeout=10,
    ).json().get("data", [])

    monthly_reach = monthly_impressions = 0
    for metric in insights:
        total = sum(v["value"] for v in metric.get("values", []))
        if metric["name"] == "reach":
            monthly_reach = total
        elif metric["name"] == "impressions":
            monthly_impressions = total

    followers = profile.get("followers_count", 0)
    snap_key = f"ig_{label}_followers"
    return {
        "username": profile.get("username", label),
        "followers": followers,
        "posts": profile.get("media_count", 0),
        "monthly_reach": monthly_reach,
        "monthly_impressions": monthly_impressions,
        "delta_followers": monthly_delta(followers, snap_key),
        "_snap_key": snap_key,
    }


# ── Instagram (public scrape fallback) ────────────────────────────────────────

def fetch_instagram_public(username, label):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "x-ig-app-id": "936619743392459",
    }
    r = requests.get(
        f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
        headers=headers, timeout=10,
    )
    if r.status_code != 200:
        return None
    u = r.json().get("data", {}).get("user", {})
    if not u:
        return None
    followers = u.get("edge_followed_by", {}).get("count", 0)
    snap_key = f"ig_{label}_followers"
    return {
        "username": username,
        "followers": followers,
        "posts": u.get("edge_owner_to_timeline_media", {}).get("count", 0),
        "monthly_reach": None,
        "monthly_impressions": None,
        "delta_followers": monthly_delta(followers, snap_key),
        "_snap_key": snap_key,
    }


# ── TikTok (public scrape) ─────────────────────────────────────────────────────

def fetch_tiktok():
    username = os.getenv("TIKTOK_USERNAME", "casprvrbk")
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    r = requests.get(f"https://www.tiktok.com/@{username}", headers=headers, timeout=15)
    match = re.search(r'"stats":\{"followerCount":(\d+),"followingCount":(\d+),"heart":(\d+),"heartCount":(\d+),"videoCount":(\d+)', r.text)
    if not match:
        return {"username": username, "error": "Could not parse public profile"}
    followers = int(match.group(1))
    return {
        "username": username,
        "followers": followers,
        "following": int(match.group(2)),
        "likes": int(match.group(4)),
        "videos": int(match.group(5)),
        "delta_followers": monthly_delta(followers, "tt_followers"),
    }


# ── Cal.com ────────────────────────────────────────────────────────────────────

def fetch_calcom():
    headers = {"Authorization": f"Bearer {os.getenv('CAL_API_KEY')}", "cal-api-version": "2024-08-13"}
    r = requests.get("https://api.cal.com/v2/bookings", headers=headers, params={"take": 250, "sortStart": "desc"}, timeout=10)
    r.raise_for_status()
    bookings = r.json().get("data", [])

    today = now().date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
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
        if d >= month_start:
            counts["this_month"] += 1
    return counts


# ── Whop ───────────────────────────────────────────────────────────────────────

def fetch_whop():
    headers = {"Authorization": f"Bearer {os.getenv('WHOP_API_KEY')}"}
    biz = os.getenv("WHOP_BUSINESS_ID")

    r = requests.get("https://api.whop.com/api/v2/memberships", headers=headers,
                     params={"business_id": biz, "status": "active", "per": 50}, timeout=10)
    r.raise_for_status()
    data = r.json()
    active = data.get("pagination", {}).get("total_count", len(data.get("data", [])))

    thirty_ago = int((now() - timedelta(days=30)).timestamp())
    r2 = requests.get("https://api.whop.com/api/v2/payments", headers=headers,
                      params={"business_id": biz, "after": thirty_ago, "per": 100}, timeout=10)
    r2.raise_for_status()
    payments = r2.json().get("data", [])

    today_ts = int(now().replace(hour=0, minute=0, second=0).timestamp())
    month_ts = int(now().replace(day=1, hour=0, minute=0, second=0).timestamp())

    return {
        "active_members": active,
        "revenue_today": sum(p.get("final_amount", 0) for p in payments if p.get("created_at", 0) >= today_ts) / 100,
        "revenue_month": sum(p.get("final_amount", 0) for p in payments if p.get("created_at", 0) >= month_ts) / 100,
        "revenue_30d": sum(p.get("final_amount", 0) for p in payments) / 100,
    }


# ── Aggregate ──────────────────────────────────────────────────────────────────

def fetch_all():
    result = {"updated_at": now().isoformat()}
    snap = {}

    try:
        yt = fetch_youtube()
        snap["yt_subs"] = yt["subscribers"]
        snap["yt_views"] = yt["total_views"]
        result["youtube"] = yt
    except Exception as e:
        result["youtube"] = {"error": str(e)}

    try:
        token_main = os.getenv("INSTAGRAM_ACCESS_TOKEN_MAIN")
        ig_main = fetch_instagram(token_main, "main") if token_main else fetch_instagram_public("casperrverbeek", "main")
        if ig_main:
            snap[ig_main["_snap_key"]] = ig_main["followers"]
        result["instagram_main"] = ig_main
    except Exception as e:
        result["instagram_main"] = {"error": str(e)}

    try:
        token_second = os.getenv("INSTAGRAM_ACCESS_TOKEN_SECOND")
        ig_second = fetch_instagram(token_second, "second") if token_second else fetch_instagram_public("casprvrbk", "second")
        if ig_second:
            snap[ig_second["_snap_key"]] = ig_second["followers"]
        result["instagram_second"] = ig_second
    except Exception as e:
        result["instagram_second"] = {"error": str(e)}

    try:
        tt = fetch_tiktok()
        if "error" not in tt:
            snap["tt_followers"] = tt["followers"]
        result["tiktok"] = tt
    except Exception as e:
        result["tiktok"] = {"error": str(e)}

    try:
        result["calcom"] = fetch_calcom()
    except Exception as e:
        result["calcom"] = {"error": str(e)}

    try:
        result["whop"] = fetch_whop()
    except Exception as e:
        result["whop"] = {"error": str(e)}

    if snap:
        save_snapshot(snap)

    return result


# ── WhatsApp ───────────────────────────────────────────────────────────────────

def send_whatsapp(message):
    phone = os.getenv("WHATSAPP_PHONE", "").replace("+", "")
    api_key = os.getenv("CALLMEBOT_API_KEY")
    if not api_key:
        print("No CallMeBot API key — skipping WhatsApp")
        return
    url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={urllib.parse.quote(message)}&apikey={api_key}"
    r = requests.get(url, timeout=15)
    print(f"WhatsApp sent: {r.status_code}")


def build_whatsapp_message(d):
    date_str = now().strftime("%a %d %b")
    dashboard_url = os.getenv("DASHBOARD_URL", "")
    lines = [f"Daily Digest · {date_str}", dashboard_url, ""]

    yt = d.get("youtube", {})
    if "error" not in yt:
        ds = f" (+{yt['delta_subscribers']:,})" if yt.get("delta_subscribers") else ""
        lines.append(f"YT: {yt['subscribers']:,} subs{ds}")

    for key, label in [("instagram_main", "@casperrverbeek"), ("instagram_second", "@casprvrbk")]:
        ig = d.get(key)
        if ig and "error" not in ig:
            df = f" (+{ig['delta_followers']:,})" if ig.get("delta_followers") else ""
            lines.append(f"IG {label}: {ig['followers']:,}{df} · {ig['monthly_reach']:,} reach")

    tt = d.get("tiktok", {})
    if tt and "error" not in tt:
        dt = f" (+{tt['delta_followers']:,})" if tt.get("delta_followers") else ""
        lines.append(f"TT @{tt['username']}: {tt['followers']:,}{dt}")

    cal = d.get("calcom", {})
    if "error" not in cal:
        lines.append(f"Calls: {cal['today']} today · {cal['this_month']} this month")

    whop = d.get("whop", {})
    if "error" not in whop:
        lines.append(f"Whop: ${whop['revenue_month']:,.2f}/mo · {whop['active_members']} members")

    return "\n".join(lines)


def daily_job():
    print(f"Running daily digest at {now()}")
    d = fetch_all()
    send_whatsapp(build_whatsapp_message(d))


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("index.html")

@app.route("/api/data")
def api_data():
    return jsonify(fetch_all())

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
