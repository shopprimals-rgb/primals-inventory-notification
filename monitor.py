"""
Warehance Reorder-Urgency Discord Bot (real bot account)
--------------------------------------------------------
Logs into Discord as a real bot (green dot + APP badge in the member list,
like the other PRIMALS bots), pulls inventory + sales from Warehance,
computes reorder urgency with the SAME logic as the inventory dashboard,
and posts to a channel — once per product, only when urgency CHANGES
(newly Critical / High / Medium).

Urgency logic (matches the dashboard):
    days_of_supply = available_stock / daily_sales_velocity
    CRITICAL = days_of_supply <= LEAD_TIME_DAYS
    HIGH     = days_of_supply <= REORDER_POINT_DAYS
    MEDIUM   = days_of_supply <= REORDER_POINT_DAYS + MEDIUM_WINDOW_DAYS
    (above that = healthy, no alert)

State (last-alerted urgency per product) is kept in a small JSON file so it
doesn't re-ping every cycle.
"""

import os
import json
import logging
from datetime import datetime, timezone

import requests
import discord
from discord.ext import tasks

# ----------------------------------------------------------------------
# CONFIG (environment variables; defaults shown)
# ----------------------------------------------------------------------
WAREHANCE_API_KEY = os.environ["WAREHANCE_API_KEY"]          # required
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]          # required
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])   # required (channel to post in)

# One lead time for ALL products (days for a supplier reorder to arrive).
# This is the number that sets the CRITICAL line.
LEAD_TIME_DAYS = int(os.environ.get("LEAD_TIME_DAYS", "120"))

# Reorder point in days. If blank, defaults to lead time + safety stock.
SAFETY_STOCK_DAYS = int(os.environ.get("SAFETY_STOCK_DAYS", "30"))
_rp = os.environ.get("REORDER_POINT_DAYS", "").strip()
REORDER_POINT_DAYS = int(_rp) if _rp else (LEAD_TIME_DAYS + SAFETY_STOCK_DAYS)

# How far above the reorder point still counts as a (medium) heads-up.
MEDIUM_WINDOW_DAYS = int(os.environ.get("MEDIUM_WINDOW_DAYS", "30"))

# Which sales window to use for velocity: 30, 60, 90 ... (Warehance sales_data)
VELOCITY_WINDOW_DAYS = int(os.environ.get("VELOCITY_WINDOW_DAYS", "30"))

# How often to check, in seconds (default 12 hours)
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "43200"))

STATE_FILE = os.environ.get("STATE_FILE", "alert_state.json")

# Non-product line items to skip entirely (matched in name OR sku,
# case-insensitive substring). Comma-separated; override via env if you want.
EXCLUDE_KEYWORDS = [
    w.strip().lower()
    for w in os.environ.get("EXCLUDE_KEYWORDS", "shipping fee,fee,guide").split(",")
    if w.strip()
]


def is_excluded(product):
    """True if this product looks like a non-physical line item to skip."""
    name = (product.get("name") or "").lower()
    sku = (product.get("sku") or "").lower()
    return any(kw in name or kw in sku for kw in EXCLUDE_KEYWORDS)

API_BASE = "https://api.warehance.com/v1"

URGENCY_RANK = {"critical": 3, "high": 2, "medium": 1, "ok": 0}
URGENCY_COLOR = {"critical": 0xE03131, "high": 0xF08C00, "medium": 0xF7B500}
URGENCY_ICON = {"critical": "\U0001F534", "high": "\U0001F7E0", "medium": "\U0001F7E1"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("warehance-bot")


# ----------------------------------------------------------------------
# State (only ping on CHANGE)
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
    return sd.get(f"sales_last_{VELOCITY_WINDOW_DAYS}_days") or 0


# ----------------------------------------------------------------------
# Discord embed (one per product)
# ----------------------------------------------------------------------
def build_embed(product, urgency, days_of_supply, now_dt):
    name = product.get("name", "Unknown")
    sku = product.get("sku") or "\u2014"
    available = product.get("available") or 0
    on_hand = product.get("on_hand") or 0
    units = _sales_for_window(product)
    velocity = units / VELOCITY_WINDOW_DAYS if VELOCITY_WINDOW_DAYS else 0
    dos_txt = "\u2014" if days_of_supply is None else f"{days_of_supply:.0f} days"

    embed = discord.Embed(
        title=f"{URGENCY_ICON.get(urgency,'\U0001F7E1')} {urgency.upper()} \u00b7 {name}",
        description=f"**Notification:** {now_dt.strftime('%b %d, %Y at %I:%M %p UTC')}",
        color=URGENCY_COLOR.get(urgency, 0xF7B500),
        timestamp=now_dt,
    )
    embed.add_field(name="SKU", value=f"`{sku}`", inline=True)
    embed.add_field(name="Days of supply", value=dos_txt, inline=True)
    embed.add_field(name="Urgency", value=urgency.upper(), inline=True)
    embed.add_field(name="Available", value=f"{available:,}", inline=True)
    embed.add_field(name="On hand", value=f"{on_hand:,}", inline=True)
    embed.add_field(name="Velocity / day", value=f"{velocity:.1f}", inline=True)
    embed.add_field(name="Lead time", value=f"{LEAD_TIME_DAYS}d", inline=True)
    embed.add_field(name="Reorder point", value=f"{REORDER_POINT_DAYS}d", inline=True)
    embed.set_footer(text="Warehance reorder monitor")
    return embed


# ----------------------------------------------------------------------
# Discord bot
# ----------------------------------------------------------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)


@client.event
async def on_ready():
    log.info(f"Logged in as {client.user} (bot is now ONLINE in your server)")
    try:
        await tree.sync()
        log.info("Slash commands synced (/total available)")
    except Exception as exc:
        log.warning(f"Failed to sync slash commands: {exc}")
    if not check_inventory.is_running():
        check_inventory.start()


@tree.command(name="total", description="Total quantity across all products (excludes shipping/fees/guides)")
async def total_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        products = fetch_all_products()
    except Exception as exc:
        await interaction.followup.send(f"Couldn't reach Warehance: {exc}")
        return

    total_available = 0
    total_on_hand = 0
    counted = 0
    for p in products:
        if is_excluded(p):
            continue
        total_available += p.get("available") or 0
        total_on_hand += p.get("on_hand") or 0
        counted += 1

    now_dt = datetime.now(timezone.utc)
    embed = discord.Embed(
        title="\U0001F4E6 Total Inventory",
        description=f"Across **{counted:,}** products (shipping/fees/guides excluded)",
        color=0x3D8B37,
        timestamp=now_dt,
    )
    embed.add_field(name="Total available", value=f"{total_available:,}", inline=True)
    embed.add_field(name="Total on hand", value=f"{total_on_hand:,}", inline=True)
    embed.set_footer(text="Warehance reorder monitor")
    await interaction.followup.send(embed=embed)


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_inventory():
    channel = client.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        log.error(
            "Channel %s not found - check DISCORD_CHANNEL_ID and that the bot "
            "was invited to the server with access to that channel.",
            DISCORD_CHANNEL_ID,
        )
        return

    try:
        products = fetch_all_products()
    except Exception as exc:
        log.exception(f"Warehance fetch failed: {exc}")
        return

    log.info("Fetched %d products from Warehance", len(products))

    prev = load_state()
    new_state = {}
    now_dt = datetime.now(timezone.utc)
    to_send = []

    for p in products:
        sku = p.get("sku")
        if not sku:
            continue
        if is_excluded(p):
            continue  # non-product line item (shipping fee, guide, etc.)
        available = p.get("available") or 0
        units = _sales_for_window(p)
        velocity = units / VELOCITY_WINDOW_DAYS if VELOCITY_WINDOW_DAYS else 0

        urgency, dos = compute_urgency(available, velocity)
        if urgency == "ok":
            continue

        new_state[sku] = urgency
        if prev.get(sku) != urgency:  # only on CHANGE
            to_send.append((urgency, p, dos))

    to_send.sort(key=lambda x: -URGENCY_RANK.get(x[0], 0))

    for urgency, p, dos in to_send:
        try:
            await channel.send(embed=build_embed(p, urgency, dos, now_dt))
        except Exception as exc:
            log.warning(f"Failed to send alert for {p.get('sku')}: {exc}")

    save_state(new_state)
    log.info("Posted %d changed alert(s) to Discord", len(to_send))


@check_inventory.before_loop
async def before_check():
    await client.wait_until_ready()


if __name__ == "__main__":
    log.info(
        "Starting Warehance reorder bot | lead_time=%dd | reorder_point=%dd | interval=%ds",
        LEAD_TIME_DAYS, REORDER_POINT_DAYS, CHECK_INTERVAL_SECONDS,
    )
    client.run(DISCORD_BOT_TOKEN)
