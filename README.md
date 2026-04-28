# Trading Bot — dYdX v4 Automated Order Execution

Automated trading bot that bridges TradingView strategy alerts to dYdX v4 perpetual markets.

Built as a lean MVP  after deliberately discarding a heavier first architecture. Ran continuously in production for over a month with zero crashes, zero memory leaks. The only failure observed was a missed alert on TradingView's side — not the bot.

---

## Architecture

```
TradingView Alert
      │
      │  HTTPS POST (JSON payload)
      ▼
┌─────────────────────┐
│   Nginx + fcgiwrap  │  TLS termination, CGI bridge
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│    webhook.py       │  Payload validation & normalization
│                     │  - strict key check
│                     │  - field type/value validation
│                     │  - symbol normalization (ETHUSDC → ETH-USD)
└─────────┬───────────┘
          │  rpush
          ▼
┌─────────────────────┐
│   Redis Queue       │  Decouples reception from execution
│  "webhook_alerts"   │  Absorbs bursts, survives processor restarts
└─────────┬───────────┘
          │  blpop
          ▼
┌─────────────────────┐
│  dydx_processor.py  │  Async order executor
│                     │  - node connection with fallback (5 endpoints)
│                     │  - oracle price + slippage calculation
│                     │  - SHORT_TERM IOC market order construction
│                     │  - wallet sequence management
└─────────┬───────────┘
          │  gRPC
          ▼
┌─────────────────────┐
│   dYdX v4 Mainnet   │  Perpetual DEX (on-chain order book)
└─────────────────────┘
```

---

## Key design decisions

**Why Redis between webhook and processor?**
Decoupling reception from execution. The webhook needs to respond instantly (Nginx timeout). Order execution is async and can take seconds. Redis absorbs the gap and survives a processor restart without losing alerts.

**Why multiple gRPC node endpoints?**
dYdX is a decentralized chain — any given public node can be temporarily unreachable. The processor tries each endpoint in sequence and logs failures, so the bot stays up even if 4 out of 5 nodes are down.

**Why a strict key check in the webhook?**
TradingView alerts are configured by hand in PineScript. A typo in the payload format should fail loudly and immediately, not silently produce a malformed order.

**Why slippage on market orders?**
dYdX perpetuals use an on-chain order book. A market order at exact oracle price may not fill if the book has moved. The slippage factor (configurable) guarantees fill at the cost of a slightly worse price.

---

## Stack

- **Python 3.11+** — async/await throughout the processor
- **Nginx + fcgiwrap** — CGI bridge for the webhook (no persistent Python process needed)
- **Redis** — lightweight queue, runs locally
- **dydx-v4-client** — official Python client for dYdX v4 gRPC API

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
sudo apt install nginx fcgiwrap redis-server
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your dYdX mnemonic and address
```

### 3. Configure Nginx

Point Nginx to `webhook/scripts/webhook.py` via fcgiwrap.  
See `nginx/webhook.conf` for reference configuration.

### 4. Start the processor

```bash
export $(cat .env | xargs)
python3 processor/dydx_processor.py
```

Or use the restart script:

```bash
bash scripts/restart.sh
```

---

## Security notes

- Secrets (mnemonic, address) are loaded exclusively from environment variables — never hardcoded
- Webhook enforces a 512-byte payload size limit
- SSL/TLS is terminated at Nginx — the CGI script never handles raw sockets
- `.env` and SSL keys are in `.gitignore`

---

## What this does not include

- The PineScript trading strategy
- Risk management logic (handled entirely on the TradingView side)
- Any UI or dashboard , refer it to your broker

---

## Author

Gabriel Raybaud — [linkedin.com/in/gabriel-raybaud-0731a8159](https://www.linkedin.com/in/gabriel-raybaud-0731a8159)
