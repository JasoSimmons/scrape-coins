# scrape-coins

Find Solana coins that hit **$300k–$5M market cap** then died — and surface the ones that died because the team gave up, not because the concept failed. Sortable dashboard ranks them by a configurable **idea score** so you can pick redeploy targets.

## What it does

Every hour it:

1. **Discovers** SOL tokens from DexScreener feeds (newest profiles, recent updates, boosted, plus broad search)
2. **Snapshots** price / MC / liquidity / volume / swaps for every tracked token via DexScreener `/tokens/v1` (batched 30 at a time)
3. **Enriches** in-band tokens via Helius DAS — counts holders, top-10 concentration, dev wallet activity
4. **Classifies** each token using thresholds in `config.yaml` and writes a verdict + idea score
5. **Serves** a local dashboard at `http://127.0.0.1:8000`

## How "good idea, bad team" gets identified

The classifier requires **both** organic-traction signals at ATH **and** team-abandonment signals now. All thresholds live in `config.yaml`.

**At peak (must show real traction):**
- MC between `$300k` and `$5M`
- ≥ 300 unique holders
- Top-10 concentration ≤ 35% (excluding burn / programs)
- ≥ 500 swaps in the 24h around ATH
- Sustained ≥ 50% of peak MC for ≥ 24h
- LP burned or locked

**Now (must show abandonment):**
- Down ≥ 90% from ATH
- 24h volume < $2k
- ATH was ≥ 72h ago
- Dev wallet inactive ≥ 7 days

**Hard excludes:**
- Dev dumped ≥ 50% within 1h of peak (classic rug)
- Peak holders < 100 (bot pump)
- Peak top-10 > 50% (insider job)

**Idea score** is a weighted composite (0–100) of: peak holders, holder diversity at peak, volume intensity (vol/mc) at peak, hours sustained ≥ 50% of ATH, current holder retention vs peak, time-to-peak, social presence, ticker quality. All weights live in `config.yaml`.

## Quick start

### 1. Install Python deps

You need Python ≥ 3.11. Easiest path uses [`uv`](https://docs.astral.sh/uv/):

```bash
brew install uv          # if you don't have it
uv sync
```

Or with plain `venv`/`pip`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Get a free Helius API key

1. Go to <https://helius.dev> → click **Sign Up** (top right). Sign up with email or Google.
2. Once in, go to **Dashboard → Endpoints** (or just the home of the dashboard — there's a default project created for you).
3. Find your **RPC URL**. It looks like `https://mainnet.helius-rpc.com/?api-key=abcd1234-...`
4. Copy just the key (the part after `api-key=`).
5. Free tier is **100k credits/month** — plenty for this project at the default hourly schedule.

### 3. Configure

```bash
cp .env.example .env
# Open .env and paste your Helius key into HELIUS_API_KEY
```

### 4. Run

Initialize the DB (one-time, optional — it auto-runs on first launch):

```bash
uv run scrape-coins init-db
```

Start the dashboard + scheduler:

```bash
uv run scrape-coins serve
```

Then open <http://127.0.0.1:8000>.

The scheduler runs all four jobs once at startup-ish; you can also trigger any of them manually from the buttons in the dashboard topbar, or from the CLI:

```bash
uv run scrape-coins discover     # pull new candidates
uv run scrape-coins snapshot     # update price/MC/vol for tracked tokens
uv run scrape-coins enrich       # Helius enrichment for in-band tokens
uv run scrape-coins classify     # rerun classifier + idea score
uv run scrape-coins cycle        # run all four in order
```

### 5. Tuning

Open `config.yaml` and edit. The big knobs:

- `classifier.peak_mc_min_usd` / `peak_mc_max_usd` — target MC band
- `classifier.peak_holders_min` — strictness of "real traction" filter
- `classifier.current_drawdown_from_ath_min` — how dead is dead
- `classifier.dev_wallet_inactive_days_min` — how abandoned is abandoned
- `idea_score.weights.*` — how to rank surviving candidates
- `discovery.search_terms` — extra search terms to widen candidate net
- `scheduler.*_minutes` — how often each job runs

After editing, click **run classify** in the dashboard (or `uv run scrape-coins classify`) to re-score everything without restarting the server. The scheduler picks up most changes on its next tick.

## Project layout

```
src/scrape_coins/
  config.py            # config.yaml + .env loader
  db.py                # SQLAlchemy models + async session
  classifier.py        # apply thresholds → mark redeploy candidates
  scoring.py           # idea-score formula
  scheduler.py         # APScheduler job wiring
  cli.py               # typer CLI entry
  clients/
    dexscreener.py     # DexScreener REST client
    helius.py          # Helius JSON-RPC client
  workers/
    discovery.py       # seed tokens table
    snapshot.py        # hourly price/MC time series
    enrich.py          # Helius holder/dev-wallet enrichment
  web/
    app.py             # FastAPI dashboard
    templates/         # Jinja2 templates
    static/            # CSS
config.yaml            # all thresholds + weights
.env.example           # HELIUS_API_KEY etc.
```

## Notes & limits

- Holder counting via Helius `getTokenAccounts` paginates 1k accounts at a time. The default cap of 50 pages = up to 50k holders per token; tokens above that are flagged `holders_count_capped=true` and the count is treated as a floor.
- `dev_wallet_inactive_days` currently uses the most recent signature on the **mint** itself + on the mint authority's wallet, as a conservative proxy. In v1 we don't try to identify the *original deployer* (that needs an on-chain backfill of the mint's first signer); if you want that level of precision, ping me and we can add it.
- Free DexScreener limits: 60 req/min for profiles/search, 300 req/min for `/tokens`. Defaults respect both.
- LP burn/lock detection is a placeholder in v1 (`null` until enriched). The classifier soft-fails when unknown, so you'll see it in `failed/unknown` reasons rather than as a hard reject. Easy to add.
- All times in the DB are UTC.

## Roadmap (easy follow-ups)

- LP burn/lock heuristic (check largest holder of LP token = burn address or known locker)
- Dev-dump detection in the 1h around ATH (Helius enhanced txns + balance diff)
- Pull GeckoTerminal OHLC to backfill ATH for tokens we discovered late
- Telegram bot that pings new candidates as they appear
- CSV export from the dashboard
