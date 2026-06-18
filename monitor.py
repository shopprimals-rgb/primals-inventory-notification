"""
Warehance Reorder-Urgency Discord Bot
-------------------------------------
A single-file bot (like the other PRIMALS bots). Pulls inventory + sales
from Warehance, computes reorder urgency using the SAME logic as the boss's
inventory dashboard, and pings Discord — once per product, only when its
urgency level CHANGES (newly Critical / High / Medium).

Urgency logic (matches the dashboard):
    days_of_supply = available_stock / daily_sales_velocity
    CRITICAL = days_of_supply <= LEAD_TIME_DAYS          (stock out before reorder arrives)
    HIGH     = days_of_supply <= REORDER_POINT_DAYS       (inside safety buffer)
    MEDIUM   = days_of_supply <= REORDER_POINT_DAYS + MEDIUM_WINDOW_DAYS
    (above that = healthy, no alert)

No database, no dashboard — just this file. State (last-alerted urgency per
product) is kept in a tiny JSON file so it doesn't re-ping every cycle.
"""

import os
import json
import time
import logging
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------------
# CONFIG (set as environment variables; defaults shown)
# ----------------------------------------------------------------------
WAREHANCE_API_KEY = os.environ["WAREHANCE_API_KEY"]          # required
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]      # required

# One lead time for ALL products (days for a supplier reorder to arrive).
# This is the number that sets the CRITICAL line.
LEAD_TIME_DAYS = int(os.environ.get("LEAD_TIME_DAYS", "120"))

# Reorder point in days. If blank, defaults to lead time + safety stock.
SAFETY_STOCK_DAYS = int(os.environ.get("SAFETY_STOCK_DAYS", "30"))
_rp = os.environ.get("REORDER_POINT_DAYS", "").strip()
REORDER_POINT_DAYS = int(_rp) if _rp else (LEAD_TIME_DAYS + SAFETY_STOCK_DAYS)

# How far above the reorder point still counts as a (medium) heads-up.
MEDIUM_WINDOW_DAYS = int(os.environ.get("MEDIUM_WINDOW_DAYS", "30"))

# Which sales window to use for velocity: 30, 60, 90... (Warehance sales_data)
VELOCITY_WINDOW_DAYS = int(os.environ.get("VELOCITY_WINDOW_DAYS", "30"))

# How often to check, in seconds (default 12 hours)
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "43200"))

STATE_FILE = os.environ.get("STATE_FILE", "alert_state.json")

API_BASE = "https://api.warehance.com/v1"

URGENCY_RANK = {"critical": 3, "high": 2, "medium": 1, "ok": 0}
URGENCY_COLOR = {"critical": 0xE03131, "high": 0xF08C00, "medium": 0xF7B500}
URGENCY_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("warehance-bot")


# ----------------------------------------------------------------------
# State (so we only ping on CHANGE)
# ----------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)  # {sku: "critical"|"high"|"medium"}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ----------------------------------------------------------------------
# Warehance: pull every product (cursor pagination)
# ----------------------------------------------------------------------
def fetch_all_products():
    products = []
    cursor = None
    headers = {"X-API-KEY": WAREHANCE_API_KEY}
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(f"{API_BASE}/products", headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data", {}) or {}
        products.extend(data.get("products", []) or [])
        if data.get("has_next_page") and data.get("next_cursor"):
            cursor = data["next_cursor"]
        else:
            break
    return products


# ----------------------------------------------------------------------
# The boss's urgency formula
# ----------------------------------------------------------------------
def compute_urgency(available, daily_velocity):
    """Return (urgency, days_of_supply). urgency is critical/high/medium/ok."""
    if daily_velocity <= 0:
        # No sales = not going to stock out from sales; treat as healthy.
        return "ok", None
    days_of_supply = available / daily_velocity
    if days_of_supply <= LEAD_TIME_DAYS:
        return "critical", days_of_supply
    if days_of_supply <= REORDER_POINT_DAYS:
        return "high", days_of_supply
    if days_of_supply <= REORDER_POINT_DAYS + MEDIUM_WINDOW_DAYS:
        return "medium", days_of_supply
    return "ok", days_of_supply


def _sales_for_window(wh_product):
    sd = wh_product.get("sales_data") or {}
    key = f"sales_last_{VELOCITY_WINDOW_DAYS}_days"
    return sd.get(key) or 0


# ----------------------------------------------------------------------
# Discord embed (one per product)
# ----------------------------------------------------------------------
def build_embed(product, urgency, days_of_supply, now_dt):
    name = product.get("name", "Unknown")
    sku = product.get("sku") or "—"
    available = product.get("available")
    available = 0 if available is None else available
    on_hand = product.get("on_hand")
    on_hand = 0 if on_hand is None else on_hand
    units = _sales_for_window(product)
    velocity = units / VELOCITY_WINDOW_DAYS if VELOCITY_WINDOW_DAYS else 0

    dos_txt = "—" if days_of_supply is None else f"{days_of_supply:.0f} days"

    fields = [
        {"name": "SKU", "value": f"`{sku}`", "inline": True},
        {"name": "Days of supply", "value": dos_txt, "inline": True},
        {"name": "Urgency", "value": urgency.upper(), "inline": True},
        {"name": "Available", "value": f"{available:,}", "inline": True},
        {"name": "On hand", "value": f"{on_hand:,}", "inline": True},
        {"name": "Velocity / day", "value": f"{velocity:.1f}", "inline": True},
        {"name": "Lead time", "value": f"{LEAD_TIME_DAYS}d", "inline": True},
        {"name": "Reorder point", "value": f"{REORDER_POINT_DAYS}d", "inline": True},
    ]
    return {
        "title": f"{URGENCY_ICON.get(urgency,'🟡')} {urgency.upper()} · {name}",
        "description": f"**Notification:** {now_dt.strftime('%b %d, %Y at %I:%M %p UTC')}",
        "color": URGENCY_COLOR.get(urgency, 0xF7B500),
        "fields": fields,
        "footer": {"text": "Warehance reorder monitor"},
        "timestamp": now_dt.isoformat(),
    }


def post_embeds(embeds):
    # Discord allows 10 embeds per message
    for i in range(0, len(embeds), 10):
        chunk = embeds[i : i + 10]
        r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": chunk}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.5)
            requests.post(DISCORD_WEBHOOK_URL, json={"embeds": chunk}, timeout=30)
        elif r.status_code not in (200, 204):
            log.warning(f"Discord returned {r.status_code}: {r.text}")


# ----------------------------------------------------------------------
# One cycle
# ----------------------------------------------------------------------
def run_cycle():
    products = fetch_all_products()
    log.info("Fetched %d products from Warehance", len(products))

    prev = load_state()
    new_state = {}
    now_dt = datetime.now(timezone.utc)
    embeds = []

    for p in products:
        sku = p.get("sku")
        if not sku:
            continue
        available = p.get("available")
        available = 0 if available is None else available
        units = _sales_for_window(p)
        velocity = units / VELOCITY_WINDOW_DAYS if VELOCITY_WINDOW_DAYS else 0

        urgency, dos = compute_urgency(available, velocity)
        if urgency == "ok":
            continue  # healthy — drops out of state, can re-alert later

        new_state[sku] = urgency
        # Only ping if urgency level CHANGED since last time
        if prev.get(sku) != urgency:
            embeds.append(build_embed(p, urgency, dos, now_dt))

    # Sort critical first
    embeds.sort(key=lambda e: -URGENCY_RANK.get(e["title"].split("·")[0].strip().split()[-1].lower(), 0))

    if embeds:
        post_embeds(embeds)
        log.info("Posted %d changed alert(s) to Discord", len(embeds))
    else:
        log.info("No urgency changes this cycle")

    save_state(new_state)


def main():
    log.info(
        "Starting Warehance reorder bot | lead_time=%dd | reorder_point=%dd | interval=%ds",
        LEAD_TIME_DAYS, REORDER_POINT_DAYS, CHECK_INTERVAL_SECONDS,
    )
    while True:
        try:
            run_cycle()
        except requests.HTTPError as e:
            log.error("HTTP error: %s | %s", e, getattr(e.response, "text", ""))
        except Exception as e:
            log.exception("Unexpected error: %s", e)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
