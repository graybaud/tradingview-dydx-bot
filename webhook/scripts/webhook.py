# webhook.py
#!/usr/bin/env python3
"""
Webhook CGI script for TradingView alerts.
Receives alerts via Nginx/fcgiwrap, validates payload, and pushes to Redis queue.
"""
import sys
import json
import logging
import redis
import os
from tradingview_alert import TradingViewAlert


logging.basicConfig(
    filename='/var/log/webhook/webhook_error.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_DB = int(os.environ.get("REDIS_DB", 0))

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)

def check_rate_limit() -> bool:
    """
    Uses a key that expires after 1 minute.
    """
    current_time = int(time.time())
    window_key = f"rate_limit:{current_time // 60}"
    
    redis_client.set(window_key, 1, nx=True, ex=120)
    
    current_count = redis_client.incr(window_key)
    
    return current_count <= MAX_ALERTS_PER_MINUTE

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

        raw_json = json.loads(raw_data)
        
        if not isinstance(raw_json, dict):
            raise ValueError("JSON payload must be a dictionary")

        # Pydantic validation (includes key checking via Config.extra='forbid')
        alert = TradingViewAlert.model_validate(raw_json)

        # --- Rate limiting ---
        if not check_rate_limit():
            raise ValueError("Rate limit exceeded. Try again later.")

        # Push to Redis queue
        redis_client.rpush("webhook_alerts", alert.model_dump_json())
        logging.info(f"Alert queued: {alert.model_dump()}")

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