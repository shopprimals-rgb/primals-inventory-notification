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
import logging
from datetime import datetime, timezone

import requests
import discord
from discord.ext import tasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("warehance-bot")

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
# How often to post the full inventory roundup, in seconds (default 4 hours)
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "14400"))


# SKU allowlist: only these products get alerts. Loaded from skus.txt
# (one SKU per line; blank lines and #comments ignored). Stored lowercase
# for case-insensitive matching.
ALLOWLIST_FILE = os.environ.get("ALLOWLIST_FILE", "skus.txt")


def load_allowlist():
    allowed = set()
    try:
        with open(ALLOWLIST_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                allowed.add(line.lower())
    except FileNotFoundError:
        log.warning("Allowlist file '%s' not found - no products will alert.", ALLOWLIST_FILE)
    return allowed


ALLOWED_SKUS = load_allowlist()


def is_allowed(product):
    """True only if this product's SKU is on the allowlist."""
    sku = (product.get("sku") or "").strip().lower()
    return sku in ALLOWED_SKUS

API_BASE = "https://api.warehance.com/v1"
PAGE_LIMIT = 100  # max allowed by Warehance /products

URGENCY_COLOR = {"critical": 0xE03131, "high": 0xF08C00, "medium": 0xF7B500}
URGENCY_ICON = {"critical": "\U0001F534", "high": "\U0001F7E0", "medium": "\U0001F7E1"}



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
# Compact inventory table (shared by roundup + /inventory + /lowstock)
# ----------------------------------------------------------------------
def allowed_products(products):
    """Filter to allowlisted products, sorted lowest on-hand first."""
    rows = [p for p in products if is_allowed(p)]
    rows.sort(key=lambda p: p.get("on_hand") or 0)
    return rows


SIZE_TOKENS = ["XXL", "XL", "Small", "Medium", "Large", "S", "M", "L"]
SIZE_ABBR = {"small": "S", "medium": "M", "large": "L", "xl": "XL", "xxl": "XXL",
             "s": "S", "m": "M", "l": "L"}


def _find_size(name):
    """Return the size token present in the name, if any (e.g. 'Small', 'XL')."""
    import re
    for tok in SIZE_TOKENS:
        if re.search(rf"(?<![A-Za-z]){re.escape(tok)}(?![A-Za-z])", name, re.IGNORECASE):
            return tok
    return None


def _drop_vowels(word):
    """Remove vowels from a word (keep first letter), for last-resort shortening."""
    if len(word) <= 3:
        return word
    return word[0] + "".join(c for c in word[1:] if c.lower() not in "aeiou")


def short_name(raw, width=20):
    """
    Make a SHORT readable label (default <=20 chars) for table rows.
    - strips 'PRIMALS'
    - keeps the first distinctive words + size, e.g. 'Hair-Strengthening'
    - appends size abbreviation if present: 'Organic Cotton (S)'
    - drops vowels as a last resort if still too long
    """
    import re
    name = (raw or "Unknown").strip()
    if name.upper().startswith("PRIMALS"):
        name = name[len("PRIMALS"):].strip()

    size = _find_size(name)
    size_abbr = SIZE_ABBR.get(size.lower(), size) if size else None

    # Drop colour/variant noise after a dash/slash and parentheticals
    core = re.split(r"[/(]", name)[0].strip()
    # keep hyphenated words together (Hair-Strengthening, Fluoride-Free)
    words = core.replace(" - ", " ").split()

    suffix = f" ({size_abbr})" if size_abbr else ""
    avail = width - len(suffix)

    # Add words until we'd exceed the available width
    label = ""
    for w in words:
        candidate = (label + " " + w).strip()
        if len(candidate) > avail:
            break
        label = candidate
    if not label:  # first word alone too long
        label = words[0] if words else core

    label = (label + suffix).strip()

    if len(label) > width:
        base = " ".join(_drop_vowels(w) for w in label.replace(suffix, "").split())
        label = (base + suffix).strip()

    return label[:width]


# ----------------------------------------------------------------------
# 5-column spreadsheet-style table (shared by roundup + commands)
# Columns: Item | OnHand | Alloc | Avail | Back | Resv
# ----------------------------------------------------------------------
def _num_field(product, *keys):
    """Return the first present numeric field among `keys` (handles API naming variants)."""
    for k in keys:
        v = product.get(k)
        if v is not None:
            return v
    return 0


def _fields(product):
    return {
        "on_hand": _num_field(product, "on_hand", "onhand", "quantity_on_hand"),
        "allocated": _num_field(product, "allocated", "allocated_quantity"),
        "available": _num_field(product, "available", "available_quantity"),
        "backordered": _num_field(product, "backordered", "backorder", "backordered_quantity"),
        "reserved": _num_field(product, "reserved", "reserved_quantity"),
    }


# Column widths
_W_NAME = 20
_W_NUM = 7


def _table_header():
    return (
        f"{'Item':<{_W_NAME}} {'OnHand':>{_W_NUM}} {'Alloc':>{_W_NUM}} "
        f"{'Avail':>{_W_NUM}} {'Back':>{_W_NUM}} {'Resv':>{_W_NUM}}"
    )


def _table_row(product):
    f = _fields(product)
    urgency = compute_urgency(f["on_hand"])
    icon = URGENCY_ICON.get(urgency, "\u2705")
    name = short_name(product.get("name"))
    return (
        f"{name:<{_W_NAME}} {f['on_hand']:>{_W_NUM},} {f['allocated']:>{_W_NUM},} "
        f"{f['available']:>{_W_NUM},} {f['backordered']:>{_W_NUM},} {f['reserved']:>{_W_NUM},} {icon}"
    )


def build_table_blocks(rows, header):
    """
    Build one or more Discord messages (each <2000 chars) containing a
    monospace 5-column spreadsheet-style table. Returns a list of strings.
    """
    legend = f"\U0001F534\u2264{CRITICAL_AT}  \U0001F7E0\u2264{HIGH_AT}  \U0001F7E1\u2264{MEDIUM_AT}  \u2705 ok"
    col_head = _table_header()
    body_lines = [_table_row(p) for p in rows]

    messages = []
    chunk = [col_head]
    chunk_len = len(col_head) + 1
    for line in body_lines:
        if chunk_len + len(line) + 1 > 1800:
            messages.append("```\n" + "\n".join(chunk) + "\n```")
            chunk = [col_head]
            chunk_len = len(col_head) + 1
        chunk.append(line)
        chunk_len += len(line) + 1
    if len(chunk) > 1:
        messages.append("```\n" + "\n".join(chunk) + "\n```")

    if not messages:
        messages = ["```\n(no products)\n```"]

    messages[0] = f"**{header}**\n{legend}\n" + messages[0]
    return messages


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
        log.info("Slash commands synced (/inventory, /lowstock, /total, /commands)")
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
        if not is_allowed(p):
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


@tree.command(name="inventory", description="Full inventory: on-hand, allocated, available, backordered, reserved")
async def inventory_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        products = fetch_all_products()
    except Exception as exc:
        await interaction.followup.send(f"Couldn't reach Warehance: {exc}")
        return

    rows = allowed_products(products)
    low = sum(1 for p in rows if compute_urgency(p.get("on_hand") or 0) != "ok")
    header = f"\U0001F4E6 Current Inventory \u2014 {len(rows)} products, {low} running low"

    msgs = build_table_blocks(rows, header)
    await interaction.followup.send(msgs[0])
    for extra in msgs[1:]:
        await interaction.followup.send(extra)


@tree.command(name="lowstock", description="Show only the products currently running low (Critical/High/Medium)")
async def lowstock_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        products = fetch_all_products()
    except Exception as exc:
        await interaction.followup.send(f"Couldn't reach Warehance: {exc}")
        return

    rows = [
        p for p in allowed_products(products)
        if compute_urgency(p.get("on_hand") or 0) != "ok"
    ]
    if not rows:
        await interaction.followup.send("\u2705 **All good** — no products are running low right now.")
        return

    header = f"\u26a0\ufe0f Running Low — {len(rows)} product(s)"
    msgs = build_table_blocks(rows, header)
    await interaction.followup.send(msgs[0])
    for extra in msgs[1:]:
        await interaction.followup.send(extra)


@tree.command(name="commands", description="List all available commands")
async def commands_command(interaction: discord.Interaction):
    now_dt = datetime.now(timezone.utc)
    embed = discord.Embed(
        title="\U0001F4CB Available Commands",
        color=0x3D8B37,
        timestamp=now_dt,
    )
    embed.add_field(
        name="/inventory",
        value="Full 5-column inventory (on-hand, allocated, available, backordered, reserved).",
        inline=False,
    )
    embed.add_field(
        name="/lowstock",
        value="Only the products currently running low (Critical / High / Medium).",
        inline=False,
    )
    embed.add_field(
        name="/total",
        value="Total on-hand + available across all tracked products.",
        inline=False,
    )
    embed.add_field(
        name="/commands",
        value="Show this list of commands.",
        inline=False,
    )
    embed.add_field(
        name="Automatic roundup",
        value=f"A full inventory roundup posts automatically every "
              f"{CHECK_INTERVAL_SECONDS // 3600} hours.",
        inline=False,
    )
    embed.add_field(
        name="Stock tiers",
        value=f"\U0001F534 Critical \u2264{CRITICAL_AT}  \u00b7  "
              f"\U0001F7E0 High \u2264{HIGH_AT}  \u00b7  "
              f"\U0001F7E1 Medium \u2264{MEDIUM_AT}",
        inline=False,
    )
    embed.set_footer(text="Warehance inventory monitor")
    await interaction.response.send_message(embed=embed)


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

    rows = allowed_products(products)
    now_dt = datetime.now(timezone.utc)
    low = sum(1 for p in rows if compute_urgency(p.get("on_hand") or 0) != "ok")
    header = (
        f"\U0001F4E6 Inventory Roundup \u00b7 {now_dt.strftime('%b %d, %Y %I:%M %p UTC')}"
        f"  \u2014  {len(rows)} products, {low} running low"
    )

    for msg in build_table_blocks(rows, header):
        try:
            await channel.send(msg)
        except Exception as exc:
            log.warning(f"Failed to send roundup message: {exc}")

    log.info("Posted inventory roundup (%d products, %d low)", len(rows), low)


@check_inventory.before_loop
async def before_check():
    await client.wait_until_ready()


if __name__ == "__main__":
    log.info(
        "Starting Warehance inventory bot | tiers: crit<=%d high<=%d med<=%d | roundup every %ds",
        CRITICAL_AT, HIGH_AT, MEDIUM_AT, CHECK_INTERVAL_SECONDS,
    )
    client.run(DISCORD_BOT_TOKEN)
