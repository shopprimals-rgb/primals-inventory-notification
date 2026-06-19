"""
Warehance Low-Stock Discord Bot (real bot account)
--------------------------------------------------
Logs into Discord as a real bot (green dot + APP badge), pulls product
inventory from Warehance, and posts a tiered low-stock alert per product —
based on ON-HAND stock level — only when a product's tier CHANGES.

Tiers (by on_hand quantity, configurable via env):
    CRITICAL : on_hand <= CRITICAL_AT   (default 50)
    HIGH     : on_hand <= HIGH_AT       (default 100)
    MEDIUM   : on_hand <= MEDIUM_AT     (default 200)
    (above MEDIUM_AT = healthy, no alert)

Slash command:  /total  -> total on-hand + available across all real products

Non-product line items (shipping fee, fee, guide) are skipped.
State is stored in a small JSON file so it only pings on tier CHANGE.
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

# Stock tiers, checked against ON_HAND quantity.
CRITICAL_AT = int(os.environ.get("CRITICAL_AT", "50"))
HIGH_AT = int(os.environ.get("HIGH_AT", "100"))
MEDIUM_AT = int(os.environ.get("MEDIUM_AT", "200"))

# How often to check, in seconds (default 12 hours)
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "43200"))

STATE_FILE = os.environ.get("STATE_FILE", "alert_state.json")

# Non-product line items to skip (substring match on name OR sku, case-insensitive)
EXCLUDE_KEYWORDS = [
    w.strip().lower()
    for w in os.environ.get("EXCLUDE_KEYWORDS", "shipping fee,fee,guide").split(",")
    if w.strip()
]

API_BASE = "https://api.warehance.com/v1"
PAGE_LIMIT = 100  # max allowed by Warehance /products

URGENCY_RANK = {"critical": 3, "high": 2, "medium": 1, "ok": 0}
URGENCY_COLOR = {"critical": 0xE03131, "high": 0xF08C00, "medium": 0xF7B500}
URGENCY_ICON = {"critical": "\U0001F534", "high": "\U0001F7E0", "medium": "\U0001F7E1"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("warehance-bot")


# ----------------------------------------------------------------------
# State (only ping on tier CHANGE)
# ----------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ----------------------------------------------------------------------
# Exclude non-product line items
# ----------------------------------------------------------------------
def is_excluded(product):
    name = (product.get("name") or "").lower()
    sku = (product.get("sku") or "").lower()
    return any(kw in name or kw in sku for kw in EXCLUDE_KEYWORDS)


# ----------------------------------------------------------------------
# Warehance: pull every product (offset pagination, with clear errors)
# ----------------------------------------------------------------------
def fetch_all_products():
    products = []
    offset = 0
    headers = {"X-API-KEY": WAREHANCE_API_KEY, "accept": "application/json"}

    while True:
        params = {"limit": PAGE_LIMIT, "offset": offset}
        resp = requests.get(f"{API_BASE}/products", headers=headers, params=params, timeout=30)

        # Surface the real Warehance error instead of a bare 400
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Warehance {resp.status_code} on /products?limit={PAGE_LIMIT}&offset={offset}: {resp.text[:300]}"
            )

        data = resp.json().get("data", {}) or {}
        batch = data.get("products", []) or []
        products.extend(batch)

        # Offset pagination: stop when we got less than a full page
        if len(batch) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT

    return products


# ----------------------------------------------------------------------
# Stock-tier urgency
# ----------------------------------------------------------------------
def compute_urgency(on_hand):
    if on_hand <= CRITICAL_AT:
        return "critical"
    if on_hand <= HIGH_AT:
        return "high"
    if on_hand <= MEDIUM_AT:
        return "medium"
    return "ok"


# ----------------------------------------------------------------------
# Discord embed (one per product)
# ----------------------------------------------------------------------
def build_embed(product, urgency, now_dt):
    name = product.get("name", "Unknown")
    sku = product.get("sku") or "\u2014"
    on_hand = product.get("on_hand") or 0
    available = product.get("available") or 0
    allocated = product.get("allocated") or 0

    embed = discord.Embed(
        title=f"{URGENCY_ICON.get(urgency,'\U0001F7E1')} {urgency.upper()} \u00b7 {name}",
        description=f"**Notification:** {now_dt.strftime('%b %d, %Y at %I:%M %p UTC')}",
        color=URGENCY_COLOR.get(urgency, 0xF7B500),
        timestamp=now_dt,
    )
    embed.add_field(name="SKU", value=f"`{sku}`", inline=True)
    embed.add_field(name="On hand", value=f"{on_hand:,}", inline=True)
    embed.add_field(name="Urgency", value=urgency.upper(), inline=True)
    embed.add_field(name="Available", value=f"{available:,}", inline=True)
    embed.add_field(name="Allocated", value=f"{allocated:,}", inline=True)
    embed.add_field(
        name="Tiers",
        value=f"Crit \u2264{CRITICAL_AT} \u00b7 High \u2264{HIGH_AT} \u00b7 Med \u2264{MEDIUM_AT}",
        inline=True,
    )
    embed.set_footer(text="Warehance low-stock monitor")
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


@tree.command(name="total", description="Total on-hand + available across all products (excludes shipping/fees/guides)")
async def total_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        products = fetch_all_products()
    except Exception as exc:
        await interaction.followup.send(f"Couldn't reach Warehance: {exc}")
        return

    total_on_hand = 0
    total_available = 0
    counted = 0
    for p in products:
        if is_excluded(p):
            continue
        total_on_hand += p.get("on_hand") or 0
        total_available += p.get("available") or 0
        counted += 1

    now_dt = datetime.now(timezone.utc)
    embed = discord.Embed(
        title="\U0001F4E6 Total Inventory",
        description=f"Across **{counted:,}** products (shipping/fees/guides excluded)",
        color=0x3D8B37,
        timestamp=now_dt,
    )
    embed.add_field(name="Total on hand", value=f"{total_on_hand:,}", inline=True)
    embed.add_field(name="Total available", value=f"{total_available:,}", inline=True)
    embed.set_footer(text="Warehance low-stock monitor")
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
        log.error(f"Warehance fetch failed: {exc}")
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
            continue
        on_hand = p.get("on_hand") or 0
        urgency = compute_urgency(on_hand)
        if urgency == "ok":
            continue

        new_state[sku] = urgency
        if prev.get(sku) != urgency:  # only on tier CHANGE
            to_send.append((urgency, p))

    to_send.sort(key=lambda x: -URGENCY_RANK.get(x[0], 0))

    for urgency, p in to_send:
        try:
            await channel.send(embed=build_embed(p, urgency, now_dt))
        except Exception as exc:
            log.warning(f"Failed to send alert for {p.get('sku')}: {exc}")

    save_state(new_state)
    log.info("Posted %d changed alert(s) to Discord", len(to_send))


@check_inventory.before_loop
async def before_check():
    await client.wait_until_ready()


if __name__ == "__main__":
    log.info(
        "Starting Warehance low-stock bot | tiers: crit<=%d high<=%d med<=%d | interval=%ds",
        CRITICAL_AT, HIGH_AT, MEDIUM_AT, CHECK_INTERVAL_SECONDS,
    )
    client.run(DISCORD_BOT_TOKEN)
