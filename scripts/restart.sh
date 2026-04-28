#!/bin/bash
# restart.sh — restart the full bot stack

set -e

echo "Stopping Nginx and fcgiwrap..."
sudo systemctl stop nginx fcgiwrap

echo "Stopping processor if running..."
pkill -f dydx_processor.py || true

echo "Restarting fcgiwrap and Nginx..."
sudo systemctl start fcgiwrap nginx

echo "Checking service status..."
sudo systemctl status fcgiwrap nginx --no-pager

echo "Starting dYdX processor..."
cd "$(dirname "$0")/../processor"
nohup python3 dydx_processor.py >> ../logs/dydx_processor.log 2>&1 &

echo "Bot stack restarted. Logs: logs/dydx_processor.log"
