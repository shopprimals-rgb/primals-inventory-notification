"""
Warehance Low-Stock Discord Bot (real bot account)
--------------------------------------------------
Logs into Discord as a real bot (green dot + APP badge), pulls product
inventory from Warehance, and posts a tiered low-stock alert per product -
based on ON-HAND stock level - only when a product's tier CHANGES.

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
PAGE_LIMIT = 100  # max allowed by Warehance

# Warehouse to report on (matches the Inventory Value Report). Default = 1251 E Walnut St.
WAREHOUSE_ID = os.environ.get("WAREHOUSE_ID", "231185184003").strip()

# How often to poll inbound shipments, in seconds (default 15 min) for prompt
# "new shipment" alerts. Separate from the 4-hour stock roundup.
INBOUND_POLL_SECONDS = int(os.environ.get("INBOUND_POLL_SECONDS", "900"))
INBOUND_STATE_FILE = os.environ.get("INBOUND_STATE_FILE", "inbound_state.json")

URGENCY_COLOR = {"critical": 0xE03131, "high": 0xF08C00, "medium": 0xF7B500}
URGENCY_ICON = {"critical": "\U0001F534", "high": "\U0001F7E0", "medium": "\U0001F7E1"}



# ----------------------------------------------------------------------
# Warehance: pull every product (offset pagination, with clear errors)
# ----------------------------------------------------------------------
def fetch_all_products():
    """
    Pull per-location inventory from Warehance's /inventory endpoint, filtered
    to the configured warehouse, and SUM all location-bins per SKU. This matches
    the Warehance Inventory Value Report exactly (a product split across bins is
    totalled). Returns a list of {sku, name, on_hand, available} dicts.
    """
    headers = {"X-API-KEY": WAREHANCE_API_KEY, "accept": "application/json"}
    cursor = None
    # Accumulate per SKU
    by_sku = {}

    while True:
        params = {"limit": PAGE_LIMIT}
        if WAREHOUSE_ID:
            params["warehouse_id"] = WAREHOUSE_ID
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(f"{API_BASE}/inventory-locations", headers=headers, params=params, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Warehance {resp.status_code} on /inventory-locations: {resp.text[:300]}"
            )

        data = resp.json().get("data", {}) or {}
        locs = data.get("inventory_locations", []) or []

        for row in locs:
            prod = row.get("product") or {}
            sku = prod.get("sku")
            if not sku:
                continue
            # Safety: if warehouse filter isn't honored, skip other warehouses
            wh = ((row.get("location") or {}).get("warehouse") or {}).get("id")
            if WAREHOUSE_ID and wh and str(wh) != str(WAREHOUSE_ID):
                continue

            entry = by_sku.setdefault(sku, {"sku": sku, "name": prod.get("name", "Unknown"),
                                            "on_hand": 0, "available": 0})
            entry["on_hand"] += row.get("quantity") or 0
            entry["available"] += row.get("available") or 0

        if data.get("has_next_page") and data.get("next_cursor"):
            cursor = data["next_cursor"]
        else:
            break

    return list(by_sku.values())


# ----------------------------------------------------------------------
# Inbound shipments: fetch, state, and alert formatting
# ----------------------------------------------------------------------
def fetch_inbound_shipments():
    """
    Pull all inbound shipments for the configured warehouse. Returns a list of
    shipment dicts (raw from Warehance), each with id, reference_number, items[],
    closed, etc.
    """
    headers = {"X-API-KEY": WAREHANCE_API_KEY, "accept": "application/json"}
    cursor = None
    shipments = []

    while True:
        params = {"limit": PAGE_LIMIT}
        if WAREHOUSE_ID:
            params["warehouse_id"] = WAREHOUSE_ID
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(f"{API_BASE}/inbound-shipments", headers=headers, params=params, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Warehance {resp.status_code} on /inbound-shipments: {resp.text[:300]}"
            )

        data = resp.json().get("data", {}) or {}
        batch = data.get("inbound_shipments", []) or []

        for s in batch:
            wh = (s.get("warehouse") or {}).get("id")
            if WAREHOUSE_ID and wh and str(wh) != str(WAREHOUSE_ID):
                continue
            shipments.append(s)

        if data.get("has_next_page") and data.get("next_cursor"):
            cursor = data["next_cursor"]
        else:
            break

    return shipments


def load_inbound_state():
    """State: {'seen': [ids], 'received': [ids]} -- shipments already announced."""
    try:
        with open(INBOUND_STATE_FILE) as f:
            data = json.load(f)
            return {"seen": set(data.get("seen", [])),
                    "received": set(data.get("received", []))}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": set(), "received": set()}


def save_inbound_state(state):
    with open(INBOUND_STATE_FILE, "w") as f:
        json.dump({"seen": sorted(state["seen"]),
                   "received": sorted(state["received"])}, f)


def is_received(shipment):
    """A shipment counts as received if it's closed OR every item's received >= ordered."""
    if shipment.get("closed"):
        return True
    items = shipment.get("items") or []
    if not items:
        return False
    return all((it.get("received") or 0) >= (it.get("ordered") or 0) for it in items)


def _inbound_lines(shipment, kind):
    """
    Build per-item lines + totals for an inbound alert.
    - created: 'Item   Ordered'
    - received: 'Item   Recv / Ord'  (side by side so over/under deliveries show)
    """
    items = shipment.get("items") or []
    lines = []
    total_main = 0   # ordered (created) or received (received)
    total_ord = 0
    for it in items:
        prod = it.get("product") or {}
        name = short_name(prod.get("name"))
        ordered = it.get("ordered") or 0
        received = it.get("received") or 0
        total_ord += ordered
        if kind == "created":
            total_main += ordered
            lines.append(f"{name:<22} {ordered:>8,}")
        else:
            total_main += received
            lines.append(f"{name:<22} {received:>7,} / {ordered:<7,}")
    return lines, total_main, total_ord


def build_inbound_embed(shipment, kind):
    """kind = 'created' or 'received'."""
    now_dt = datetime.now(timezone.utc)
    ref = shipment.get("reference_number") or f"Shipment {shipment.get('id')}"
    lines, total_main, total_ord = _inbound_lines(shipment, kind)

    if kind == "created":
        title = "\U0001F4E6 New Inbound Shipment"
        color = 0x3D8B37
        header = f"{'Item':<22} {'Ordered':>8}"
    else:
        title = "\u2705 Inbound Shipment Received"
        color = 0x1F8B4C
        header = f"{'Item':<22} {'Recv':>7} / {'Ord':<7}"

    table = header + "\n" + "\n".join(lines)
    embed = discord.Embed(
        title=title,
        description=f"**{ref}**\n```\n{table}\n```",
        color=color,
        timestamp=now_dt,
    )
    if kind == "received":
        embed.add_field(name="Total received", value=f"{total_main:,}", inline=True)
        embed.add_field(name="Total ordered", value=f"{total_ord:,}", inline=True)
    else:
        embed.add_field(name="Total units", value=f"{total_main:,}", inline=True)
    embed.add_field(name="Products", value=f"{len(lines)}", inline=True)
    embed.set_footer(text="Warehance inbound monitor")
    return embed


def _fmt_date(iso):
    """Format an ISO timestamp as 'Jun 17, 2026'. Returns '-' for missing/placeholder."""
    if not iso:
        return "-"
    # Warehance uses '0001-01-01T...' as a placeholder for 'no date'
    if iso.startswith("0001"):
        return "-"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except (ValueError, AttributeError):
        return "-"


def build_inbound_detail_block(shipment):
    """
    One shipment as a full-detail text block for /inbound:
    name, added/received dates, status, every product with Recv/Ord, totals.
    Returns a string (may be combined with others, then split into messages).
    """
    ref = shipment.get("reference_number") or f"Shipment {shipment.get('id')}"
    added = _fmt_date(shipment.get("created_at"))
    received_date = _fmt_date(shipment.get("closed_date"))
    closed = bool(shipment.get("closed"))
    status_icon = "\u2705" if closed else "\U0001F69A"  # check if closed, truck if in transit
    status_txt = "Received" if closed else "In transit"

    items = shipment.get("items") or []
    lines = []
    total_recv = 0
    total_ord = 0
    for it in items:
        prod = it.get("product") or {}
        name = short_name(prod.get("name"))
        ordered = it.get("ordered") or 0
        received = it.get("received") or 0
        total_recv += received
        total_ord += ordered
        lines.append(f"  {name:<22} {received:>7,} / {ordered:<7,}")

    head = (
        f"{status_icon} {ref}\n"
        f"   Status: {status_txt}  |  Added: {added}  |  Received: {received_date}\n"
        f"   {'Item':<22} {'Recv':>7} / {'Ord':<7}"
    )
    foot = f"   Total: {total_recv:,} received / {total_ord:,} ordered  ({len(lines)} product(s))"
    return head + "\n" + "\n".join(lines) + "\n" + foot


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
# Columns: Item | OnHand | Avail
# ----------------------------------------------------------------------

# Column widths
_W_NAME = 22
_W_NUM = 8


def _table_header():
    return f"{'Item':<{_W_NAME}} {'OnHand':>{_W_NUM}} {'Avail':>{_W_NUM}}"


def _table_row(product):
    on_hand = product.get("on_hand") or 0
    available = product.get("available") or 0
    urgency = compute_urgency(on_hand)
    icon = URGENCY_ICON.get(urgency, "\u2705")
    name = short_name(product.get("name"))
    return f"{name:<{_W_NAME}} {on_hand:>{_W_NUM},} {available:>{_W_NUM},} {icon}"


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
        log.info("Slash commands synced (/inventory, /lowstock, /inbound, /total, /commands)")
    except Exception as exc:
        log.warning(f"Failed to sync slash commands: {exc}")
    if not check_inventory.is_running():
        check_inventory.start()
    if not check_inbound.is_running():
        check_inbound.start()


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


@tree.command(name="inventory", description="Full inventory: on-hand and available, per product")
async def inventory_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        products = fetch_all_products()
    except Exception as exc:
        await interaction.followup.send(f"Couldn't reach Warehance: {exc}")
        return

    rows = allowed_products(products)
    low = sum(1 for p in rows if compute_urgency(p.get("on_hand") or 0) != "ok")
    header = f"\U0001F4CA Current Inventory - {len(rows)} products, {low} running low"

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
        await interaction.followup.send("\u2705 **All good** - no products are running low right now.")
        return

    header = f"\u26a0\ufe0f Running Low - {len(rows)} product(s)"
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
        value="Full inventory list (on-hand + available) for 1251 E Walnut St.",
        inline=False,
    )
    embed.add_field(
        name="/lowstock",
        value="Only the products currently running low (Critical / High / Medium).",
        inline=False,
    )
    embed.add_field(
        name="/inbound",
        value="All inbound shipments with products, quantities (received/ordered), and dates.",
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


@tree.command(name="inbound", description="List all inbound shipments with products, quantities, and dates")
async def inbound_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        shipments = fetch_inbound_shipments()
    except Exception as exc:
        await interaction.followup.send(f"Couldn't reach Warehance: {exc}")
        return

    if not shipments:
        await interaction.followup.send("No inbound shipments found.")
        return

    # Newest first by created_at
    shipments.sort(key=lambda s: s.get("created_at") or "", reverse=True)

    open_count = sum(1 for s in shipments if not s.get("closed"))
    header = (f"\U0001F69A Inbound Shipments - {len(shipments)} total, "
              f"{open_count} in transit\n"
              f"\U0001F69A in transit  \u2705 received")

    blocks = [build_inbound_detail_block(s) for s in shipments]

    # Pack blocks into Discord messages under 2000 chars, each wrapped in a code fence
    messages = []
    chunk = []
    chunk_len = 0
    for b in blocks:
        # +8 for code fences and newlines
        if chunk_len + len(b) + 8 > 1850 and chunk:
            messages.append("```\n" + "\n\n".join(chunk) + "\n```")
            chunk, chunk_len = [], 0
        chunk.append(b)
        chunk_len += len(b) + 2
    if chunk:
        messages.append("```\n" + "\n\n".join(chunk) + "\n```")

    # First message carries the header
    await interaction.followup.send(header + "\n" + messages[0])
    for extra in messages[1:]:
        await interaction.followup.send(extra)


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
        f"\U0001F4CA Inventory Count \u00b7 {now_dt.strftime('%b %d, %Y %I:%M %p UTC')}"
        f"  -  {len(rows)} products, {low} running low"
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


@tasks.loop(seconds=INBOUND_POLL_SECONDS)
async def check_inbound():
    channel = client.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        log.error("Channel %s not found for inbound alerts.", DISCORD_CHANNEL_ID)
        return

    try:
        shipments = fetch_inbound_shipments()
    except Exception as exc:
        log.error(f"Inbound fetch failed: {exc}")
        return

    state = load_inbound_state()
    first_run = not state["seen"] and not state["received"]

    if first_run:
        # Baseline: record all existing shipments WITHOUT alerting, so we don't
        # dump dozens of "new shipment" messages on first boot. Mark already-
        # received ones too.
        for s in shipments:
            sid = s.get("id")
            if sid is None:
                continue
            state["seen"].add(sid)
            if is_received(s):
                state["received"].add(sid)
        save_inbound_state(state)
        log.info("Inbound baseline recorded: %d existing shipments (no alerts).", len(shipments))
        return

    new_created = 0
    new_received = 0

    for s in shipments:
        sid = s.get("id")
        if sid is None:
            continue

        # NEW shipment -> created alert
        if sid not in state["seen"]:
            state["seen"].add(sid)
            try:
                await channel.send(embed=build_inbound_embed(s, "created"))
                new_created += 1
            except Exception as exc:
                log.warning(f"Failed to send created alert for {sid}: {exc}")

        # RECEIVED -> received alert (once)
        if sid not in state["received"] and is_received(s):
            state["received"].add(sid)
            try:
                await channel.send(embed=build_inbound_embed(s, "received"))
                new_received += 1
            except Exception as exc:
                log.warning(f"Failed to send received alert for {sid}: {exc}")

    save_inbound_state(state)
    if new_created or new_received:
        log.info("Inbound alerts posted: %d new, %d received", new_created, new_received)


@check_inbound.before_loop
async def before_inbound():
    await client.wait_until_ready()


if __name__ == "__main__":
    log.info(
        "Starting Warehance inventory bot | tiers: crit<=%d high<=%d med<=%d | roundup every %ds | inbound poll %ds",
        CRITICAL_AT, HIGH_AT, MEDIUM_AT, CHECK_INTERVAL_SECONDS, INBOUND_POLL_SECONDS,
    )
    client.run(DISCORD_BOT_TOKEN)
