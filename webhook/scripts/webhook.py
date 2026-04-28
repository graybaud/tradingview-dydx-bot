#!/usr/bin/env python3
"""
Webhook CGI script — receives TradingView alerts via Nginx/fcgiwrap,
validates payload, and pushes to Redis queue for async processing.
"""

import sys
import json
import logging
import redis
import os

logging.basicConfig(
    filename='/var/log/webhook/webhook_error.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Redis connection
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_DB   = int(os.environ.get("REDIS_DB", 0))
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)

# Symbol normalization: TradingView format -> broker format
SYMBOL_MAPPING = {
    "ETHUSDC":  "ETH-USD",
    "BTCUSDC":  "BTC-USD",
}

def map_symbol(symbol: str) -> str:
    """Normalize TradingView symbol to broker format."""
    logging.info(f"Symbol received: '{symbol}'")
    if symbol in SYMBOL_MAPPING:
        return SYMBOL_MAPPING[symbol]
    if symbol.endswith("USDC"):
        return symbol.replace("USDC", "USD")
    raise ValueError(f"Unsupported symbol: {symbol}")

def is_positive_number(s: str) -> bool:
    """Validate that a string represents a strictly positive number."""
    try:
        return float(s) > 0
    except (ValueError, TypeError):
        return False

def main():
    try:
        MAX_CONTENT_LENGTH = 512
        content_length = int(os.environ.get("CONTENT_LENGTH", 0))
        if content_length > MAX_CONTENT_LENGTH:
            raise ValueError(f"Payload too large: {content_length} bytes (max {MAX_CONTENT_LENGTH})")

        raw_data = sys.stdin.read(content_length) if content_length else ""
        logging.info(f"Raw data received: {raw_data}")

        if not raw_data.strip():
            raise ValueError("Empty payload")

        data = json.loads(raw_data)
        if not isinstance(data, dict):
            raise ValueError("JSON payload must be a dictionary")

        # Strict key validation — no extra or missing keys accepted
        expected_keys = {"ticker", "side", "type", "size", "reduce_only", "timestamp"}
        if set(data.keys()) != expected_keys:
            raise ValueError(f"Invalid keys. Expected: {expected_keys}, got: {set(data.keys())}")

        # Field validation and normalization
        if data["side"].lower() not in ["buy", "sell"]:
            raise ValueError("'side' must be 'buy' or 'sell'")
        data["side"] = data["side"].upper()

        if data["type"].lower() not in ["long", "short"]:
            raise ValueError("'type' must be 'long' or 'short'")
        data["type"] = data["type"].lower()

        if not data["ticker"].strip():
            raise ValueError("'ticker' is empty")
        data["ticker"] = map_symbol(data["ticker"])

        if not is_positive_number(data["size"]):
            raise ValueError("'size' must be a positive number")

        if data["reduce_only"].lower() not in ["true", "false"]:
            raise ValueError("'reduce_only' must be 'true' or 'false'")
        data["reduce_only"] = data["reduce_only"].lower()

        # Push validated alert to Redis queue
        redis_client.rpush("webhook_alerts", json.dumps(data))
        logging.info(f"Alert queued: {data}")

        print("Content-Type: text/plain\n")
        print("OK")

    except ValueError as e:
        logging.error(f"Validation error: {str(e)}")
        print("Content-Type: text/plain\n")
        print(f"Error: {str(e)}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}")
        print("Content-Type: text/plain\n")
        print(f"Error: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
