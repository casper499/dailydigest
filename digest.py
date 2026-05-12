import os
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

TODAY = datetime.now(timezone.utc)
YESTERDAY = TODAY - timedelta(days=1)


# ── YouTube ────────────────────────────────────────────────────────────────────

def get_youtube_stats():
    api_key = os.getenv("YOUTUBE_API_KEY")
    channel_id = os.getenv("YOUTUBE_CHANNEL_ID")
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {
        "part": "statistics",
        "id": channel_id,
        "key": api_key,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    stats = r.json()["items"][0]["statistics"]
    return {
        "subscribers": int(stats.get("subscriberCount", 0)),
        "total_views": int(stats.get("viewCount", 0)),
        "videos": int(stats.get("videoCount", 0)),
    }


def get_youtube_recent_views():
    """Get views from the last 28 days via YouTube Analytics API."""
    # Requires OAuth — returning placeholder until OAuth is set up
    return None


# ── Cal.com ────────────────────────────────────────────────────────────────────

def get_calcom_bookings():
    api_key = os.getenv("CAL_API_KEY")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "cal-api-version": "2024-08-13",
    }
    r = requests.get(
        "https://api.cal.com/v2/bookings",
        headers=headers,
        params={"take": 250, "sortStart": "desc"},
        timeout=10,
    )
    r.raise_for_status()
    all_bookings = r.json().get("data", [])

    today_date = TODAY.date()
    week_start = today_date - timedelta(days=today_date.weekday())

    bookings_today, bookings_week = 0, 0
    for b in all_bookings:
        if b.get("status") != "accepted":
            continue
        start_str = b.get("start", "")
        if not start_str:
            continue
        booking_date = datetime.fromisoformat(start_str.replace("Z", "+00:00")).date()
        if booking_date == today_date:
            bookings_today += 1
        if booking_date >= week_start:
            bookings_week += 1

    return {"today": bookings_today, "this_week": bookings_week}


# ── Whop ───────────────────────────────────────────────────────────────────────

def get_whop_stats():
    api_key = os.getenv("WHOP_API_KEY")
    business_id = os.getenv("WHOP_BUSINESS_ID")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Active memberships
    r = requests.get(
        "https://api.whop.com/api/v2/memberships",
        headers=headers,
        params={"business_id": business_id, "status": "active", "per": 50},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    active_members = data.get("pagination", {}).get("total_count", len(data.get("data", [])))

    # Revenue — payments from last 30 days
    thirty_days_ago = int((TODAY - timedelta(days=30)).timestamp())
    r2 = requests.get(
        "https://api.whop.com/api/v2/payments",
        headers=headers,
        params={"business_id": business_id, "after": thirty_days_ago, "per": 100},
        timeout=10,
    )
    r2.raise_for_status()
    payments = r2.json().get("data", [])
    revenue_30d = sum(p.get("final_amount", 0) for p in payments) / 100  # cents → dollars

    # Revenue today
    today_ts = int(TODAY.replace(hour=0, minute=0, second=0).timestamp())
    revenue_today = sum(
        p.get("final_amount", 0) for p in payments if p.get("created_at", 0) >= today_ts
    ) / 100

    return {
        "active_members": active_members,
        "revenue_today": revenue_today,
        "revenue_30d": revenue_30d,
    }


# ── Instagram ─────────────────────────────────────────────────────────────────

def get_instagram_stats(access_token, label="Main"):
    if not access_token:
        return None
    # Get IG user ID
    r = requests.get(
        "https://graph.facebook.com/v19.0/me/accounts",
        params={"access_token": access_token},
        timeout=10,
    )
    r.raise_for_status()
    pages = r.json().get("data", [])
    if not pages:
        return None
    page_id = pages[0]["id"]
    page_token = pages[0]["access_token"]

    r2 = requests.get(
        f"https://graph.facebook.com/v19.0/{page_id}",
        params={
            "fields": "instagram_business_account",
            "access_token": page_token,
        },
        timeout=10,
    )
    r2.raise_for_status()
    ig_id = r2.json().get("instagram_business_account", {}).get("id")
    if not ig_id:
        return None

    # Get follower count + media count
    r3 = requests.get(
        f"https://graph.facebook.com/v19.0/{ig_id}",
        params={
            "fields": "followers_count,media_count,username",
            "access_token": page_token,
        },
        timeout=10,
    )
    r3.raise_for_status()
    data = r3.json()
    return {
        "username": data.get("username", label),
        "followers": data.get("followers_count", 0),
        "posts": data.get("media_count", 0),
    }


# ── Slack ──────────────────────────────────────────────────────────────────────

def send_slack(message: str):
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook:
        print("No Slack webhook set — printing digest instead:\n")
        print(message)
        return
    r = requests.post(webhook, json={"text": message}, timeout=10)
    r.raise_for_status()
    print("Digest sent to Slack.")


# ── Build digest ───────────────────────────────────────────────────────────────

def build_digest():
    date_str = TODAY.strftime("%A, %d %b %Y")
    lines = [f"*Daily Digest — {date_str}*", ""]

    # YouTube
    try:
        yt = get_youtube_stats()
        lines += [
            "*YouTube*",
            f"  Subscribers: {yt['subscribers']:,}",
            f"  Total views: {yt['total_views']:,}",
            f"  Videos uploaded: {yt['videos']:,}",
            "",
        ]
    except Exception as e:
        lines += [f"*YouTube* — error: {e}", ""]

    # Cal.com
    try:
        cal = get_calcom_bookings()
        lines += [
            "*Cal.com — Booked Calls*",
            f"  Today: {cal['today']}",
            f"  This week: {cal['this_week']}",
            "",
        ]
    except Exception as e:
        lines += [f"*Cal.com* — error: {e}", ""]

    # Whop
    try:
        whop = get_whop_stats()
        lines += [
            "*Whop Revenue*",
            f"  Today: ${whop['revenue_today']:,.2f}",
            f"  Last 30 days: ${whop['revenue_30d']:,.2f}",
            f"  Active members: {whop['active_members']:,}",
            "",
        ]
    except Exception as e:
        lines += [f"*Whop* — error: {e}", ""]

    # Instagram (main)
    token_main = os.getenv("INSTAGRAM_ACCESS_TOKEN_MAIN")
    if token_main:
        try:
            ig = get_instagram_stats(token_main, "Main IG")
            lines += [
                f"*Instagram — @{ig['username']}*",
                f"  Followers: {ig['followers']:,}",
                f"  Posts: {ig['posts']:,}",
                "",
            ]
        except Exception as e:
            lines += [f"*Instagram (main)* — error: {e}", ""]
    else:
        lines += ["*Instagram (main)* — access token not set yet", ""]

    # Instagram (second)
    token_second = os.getenv("INSTAGRAM_ACCESS_TOKEN_SECOND")
    if token_second:
        try:
            ig2 = get_instagram_stats(token_second, "Second IG")
            lines += [
                f"*Instagram — @{ig2['username']}*",
                f"  Followers: {ig2['followers']:,}",
                f"  Posts: {ig2['posts']:,}",
                "",
            ]
        except Exception as e:
            lines += [f"*Instagram (second)* — error: {e}", ""]
    else:
        lines += ["*Instagram (second)* — access token not set yet", ""]

    # TikTok
    tiktok_username = os.getenv("TIKTOK_USERNAME")
    if tiktok_username:
        lines += [f"*TikTok* — @{tiktok_username} (API pending)", ""]
    else:
        lines += ["*TikTok* — username not set yet", ""]

    return "\n".join(lines)


if __name__ == "__main__":
    digest = build_digest()
    send_slack(digest)
