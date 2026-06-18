# Warehance Reorder-Urgency Discord Bot

Single-file bot. Pulls inventory + sales from Warehance, computes reorder
urgency with the SAME logic as the inventory dashboard, pings Discord once
per product — only when urgency CHANGES (newly Critical / High / Medium).

## Urgency logic
days_of_supply = available stock / daily sales velocity
- CRITICAL: days_of_supply <= LEAD_TIME_DAYS
- HIGH:     days_of_supply <= REORDER_POINT_DAYS
- MEDIUM:   days_of_supply <= REORDER_POINT_DAYS + MEDIUM_WINDOW_DAYS

## Setup
1. Warehance API key: Settings -> API Keys -> Generate (needs read_products).
2. Discord webhook: channel -> Integrations -> Webhooks -> New -> Copy URL.
3. Set env vars (see .env.example).
4. Run:
   pip install -r requirements.txt
   python monitor.py

## Railway (same as your other bots)
- Push to a GitHub repo, deploy from repo, set env vars in the dashboard.
- Runs `python monitor.py` (Procfile). Add a volume if you want
  alert_state.json to persist across restarts (point STATE_FILE at it).
