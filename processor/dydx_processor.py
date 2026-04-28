"""
dYdX order processor — consumes validated alerts from Redis queue
and executes market orders on dYdX v4 mainnet via gRPC.

Architecture:
  webhook.py -> Redis queue ("webhook_alerts") -> this script -> dYdX API
"""

import json
import os
import logging
import asyncio
import random
from decimal import Decimal, getcontext
from collections import defaultdict
getcontext().prec = 50

from dydx_v4_client import MAX_CLIENT_ID, OrderFlags
from dydx_v4_client.node.client import NodeClient
from dydx_v4_client.node.market import Market
from dydx_v4_client.wallet import Wallet
from dydx_v4_client.indexer.rest.indexer_client import MarketsClient, IndexerClient
from dydx_v4_client.indexer.rest.constants import OrderType
from v4_proto.dydxprotocol.clob.order_pb2 import Order
from dydx_v4_client.network import make_mainnet
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG = {
    "mainnet_node_endpoints": [
        "dydx-dao-grpc-1.polkachu.com:443",
        "dydx-ops-grpc.kingnodes.com:443",
        "dydx-dao-grpc.enigma-validator.com:443",
        "dydx.lavenderfive.com:443",
        "dydx-grpc.publicnode.com:443"
    ],
    "mainnet_indexer_endpoints": [
        "https://indexer.dydx.trade/v4"
    ],
    "mainnet_websocket_indexer": "wss://indexer.dydx.trade/v4/ws",
    "slippage": 0.05,
    "subaccount_number": 0
}

to_order_side = {"BUY": Order.Side.SIDE_BUY, "SELL": Order.Side.SIDE_SELL}

async def get_market(rest_indexer, ticker: str) -> Market | None:
    try:
        markets_client = MarketsClient(rest_indexer)
        result = await markets_client.get_perpetual_markets(market=ticker)
        market_data = result.get("markets", {}).get(ticker)
        if not market_data:
            logger.error(f"get_market FAILED: Ticker '{ticker}' not found")
            return None
        return Market(market_data)
    except Exception as e:
        logger.error(f"get_market FAILED for '{ticker}': {e}")
        return None

async def generate_order_id(market: Market, address: str) -> str | None:
    try:
        client_id = random.randint(0, MAX_CLIENT_ID)
        return market.order_id(address, CONFIG["subaccount_number"], client_id, OrderFlags.SHORT_TERM)
    except Exception as e:
        logger.error(f"generate_order_id FAILED: {e}")
        return None

async def get_adjusted_price(oraclePrice, side: str) -> float | None:
    try:
        oracle_price = float(oraclePrice)
        slippage_factor = 1.0 + CONFIG["slippage"] if side == "BUY" else 1.0 - CONFIG["slippage"]
        return oracle_price * slippage_factor
    except Exception as e:
        logger.error(f"get_adjusted_price FAILED: {e}")
        return None

async def create_market_order(market: Market, order_id: str, side: str, size: str, adjusted_price: float, reduce_only: bool, node: NodeClient) -> Order | None:
    try:
        current_block = await node.latest_block_height()
        return market.order(
            order_id=order_id,
            order_type=OrderType.MARKET,
            side=to_order_side[side],
            size=float(size),
            price=adjusted_price,
            time_in_force=Order.TimeInForce.TIME_IN_FORCE_IOC,
            reduce_only=reduce_only,
            good_til_block=current_block + 10,
        )
    except Exception as e:
        logger.error(f"create_market_order FAILED (id={order_id}, {side} {size} @ {adjusted_price:.2f}): {e}")
        return None

async def execute_order(node: NodeClient, wallet: Wallet, order: Order) -> dict | None:
    try:
        tx = await node.place_order(wallet, order)
        wallet.sequence += 1
        await asyncio.sleep(1)
        return tx
    except Exception as e:
        logger.error(f"execute_order FAILED (id={order.order_id}): {e}")
        return None

async def place_market_order(node: NodeClient, wallet: Wallet, indexer: IndexerClient, market: Market, alert: dict) -> dict:
    result = {"success": False, "order_id": None}
    try:
        ticker = alert.get("ticker")
        side = alert.get("side")
        size = alert.get("size")
        reduce_only = alert.get("reduce_only") == "true"
        order_id = await generate_order_id(market, wallet.address)
        if not order_id:
            return result
        adjusted_price = await get_adjusted_price(market.market.get("oraclePrice"), side)
        if not adjusted_price:
            return result
        order = await create_market_order(market, order_id, side, size, adjusted_price, reduce_only, node)
        if not order:
            return result
        tx = await execute_order(node, wallet, order)
        if not tx:
            return result
        result["success"] = True
        result["order_id"] = order_id
        return result
    except Exception as e:
        logger.error(f"place_market_order FAILED ({ticker} {side} {size}): {e}")
        return result

async def process_alert(alert: dict, node: NodeClient, indexer: IndexerClient, wallet: Wallet, rest_indexer) -> bool:
    try:
        ticker = alert["ticker"]
        market = await get_market(rest_indexer, ticker)
        if not market:
            return False
        result = await place_market_order(node, wallet, indexer, market, alert)
        return result["success"]
    except Exception as e:
        logger.error(f"process_alert FAILED (ticker={alert.get('ticker', 'UNKNOWN')}): {e}")
        return False

async def monitor_redis(node, wallet, indexer, rest_indexer):
    r = await aioredis.from_url('redis://localhost:6379', decode_responses=True)
    logger.info("Bot ready - Waiting for alerts on webhook_alerts")
    alert_count = 0
    success_count = 0
    stats_by_ticker = defaultdict(lambda: {"success": 0, "failed": 0})
    reconnect_attempts = 0
    max_reconnect_attempts = 10

    try:
        while True:
            try:
                result = await r.blpop("webhook_alerts", timeout=0)
                reconnect_attempts = 0
                if result:
                    alert_count += 1
                    _, alert_json = result
                    try:
                        alert = json.loads(alert_json)
                        ticker = alert.get('ticker', 'UNKNOWN')
                        side = alert.get('side', '?')
                        size = alert.get('size', '?')
                        logger.info(f"Alert #{alert_count} received: {ticker} {side} {size}")
                        success = await process_alert(alert, node, indexer, wallet, rest_indexer)
                        if success:
                            success_count += 1
                            stats_by_ticker[ticker]["success"] += 1
                            logger.info(f"OK Alert #{alert_count} ({ticker}) processed successfully")
                        else:
                            stats_by_ticker[ticker]["failed"] += 1
                            logger.error(f"FAIL Alert #{alert_count} ({ticker}) - Processing failed")
                        if alert_count % 10 == 0:
                            success_rate = (success_count * 100) // alert_count
                            logger.info(f"Stats: {success_count}/{alert_count} success ({success_rate}%)")
                            logger.info(f"Breakdown: {dict(stats_by_ticker)}")
                    except json.JSONDecodeError as e:
                        logger.error(f"Alert #{alert_count} - Invalid JSON: {e}")
                        logger.error(f"Content: {alert_json[:200]}")
                    except Exception as e:
                        logger.error(f"Alert #{alert_count} - Error: {e}")
            except aioredis.RedisError as e:
                reconnect_attempts += 1
                logger.error(f"Redis error (attempt {reconnect_attempts}/{max_reconnect_attempts}): {e}")
                if reconnect_attempts >= max_reconnect_attempts:
                    logger.error("Max reconnection attempts reached - Stopping bot")
                    raise
                backoff_time = 5 * reconnect_attempts
                logger.info(f"Reconnecting in {backoff_time}s...")
                await asyncio.sleep(backoff_time)
                try:
                    await r.close()
                    r = await aioredis.from_url('redis://localhost:6379', decode_responses=True)
                    logger.info(f"Reconnected successfully")
                except Exception as reconnect_error:
                    logger.error(f"Reconnection failed: {reconnect_error}")
    except KeyboardInterrupt:
        logger.info("Shutdown requested (Ctrl+C)")
    except Exception as e:
        logger.error(f"Critical error: {e}")
        raise
    finally:
        await r.close()
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info(f"Bot stopped - {success_count}/{alert_count} success")
        if stats_by_ticker:
            logger.info(f"By ticker: {dict(stats_by_ticker)}")
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

async def try_connect_node():
    for endpoint in CONFIG["mainnet_node_endpoints"]:
        try:
            config = make_mainnet(
                node_url=endpoint,
                rest_indexer=CONFIG["mainnet_indexer_endpoints"][0],
                websocket_indexer=CONFIG["mainnet_websocket_indexer"]
            )
            node = await NodeClient.connect(config.node)
            logger.info(f"Node connected: {endpoint}")
            return node, config.rest_indexer
        except Exception as e:
            logger.error(f"Node connection failed '{endpoint}': {e}")
    logger.error("CRITICAL: No node reachable")
    raise Exception("All node endpoints failed")

async def get_user_fee_tier(node: NodeClient, address: str) -> dict | None:
    try:
        return await node.get_user_fee_tier(address)
    except Exception as e:
        logger.error(f"get_user_fee_tier FAILED: {e}")
        return None

async def get_subaccount(indexer: IndexerClient, address: str) -> dict | None:
    try:
        return await indexer.account.get_subaccount(address, CONFIG["subaccount_number"])
    except Exception as e:
        logger.error(f"get_subaccount FAILED (sub #{CONFIG['subaccount_number']}): {e}")
        return None

async def get_subaccounts(indexer: IndexerClient, address: str) -> dict | None:
    try:
        return await indexer.account.get_subaccounts(address)
    except Exception as e:
        logger.error(f"get_subaccounts FAILED: {e}")
        return None

async def verify_redis_connection() -> bool:
    try:
        r = await aioredis.from_url('redis://localhost:6379', decode_responses=True)
        await r.ping()
        await r.close()
        logger.info("Redis reachable")
        return True
    except Exception as e:
        logger.error(f"Redis unreachable: {e}")
        return False

async def main():
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("   DYDX TRADING BOT - INITIALIZATION")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Environment variables
    mnemonic = os.getenv("DYDX_MNEMONIC_MAINNET")
    address = os.getenv("DYDX_ADDRESS_MAINNET")
    if not mnemonic:
        logger.error("DYDX_MNEMONIC_MAINNET missing")
        raise Exception("DYDX_MNEMONIC_MAINNET not set")
    if not address:
        logger.error("DYDX_ADDRESS_MAINNET missing")
        raise Exception("DYDX_ADDRESS_MAINNET not set")
    logger.info(f"Environment variables OK - Address: {address}")

    # Node connection
    node, rest_indexer = await try_connect_node()

    # Indexer
    indexer = IndexerClient(rest_indexer)
    logger.info(f"Indexer initialized")

    # Wallet
    wallet = await Wallet.from_mnemonic(node, mnemonic, address)
    logger.info(f"Wallet OK (sequence: {wallet.sequence})")

    # Account
    account = await node.get_account(address)
    if not account:
        raise Exception("Account unreachable")
    logger.info(f"Account verified")

    # Fee tier
    fee_tier = await get_user_fee_tier(node, address)
    if not fee_tier:
        raise Exception("Fee tier unreachable")
    logger.info(f"Fee tier: {fee_tier}")

    # Subaccounts
    subaccounts = await get_subaccounts(indexer, address)
    if not subaccounts:
        raise Exception("Subaccounts unreachable")
    nb_subs = len(subaccounts.get('subaccounts', []))
    logger.info(f"{nb_subs} subaccount(s) available")

    # Primary subaccount
    subaccount = await get_subaccount(indexer, address)
    if not subaccount:
        raise Exception("Primary subaccount unreachable")
    equity = subaccount.get('equity', 'N/A')
    logger.info(f"Subaccount #{CONFIG['subaccount_number']} OK (equity: {equity})")

    # Redis
    redis_ok = await verify_redis_connection()
    if not redis_ok:
        raise Exception("Redis unreachable")

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("   ALL CHECKS PASSED - STARTING")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    try:
        await monitor_redis(node, wallet, indexer, rest_indexer)
    except KeyboardInterrupt:
        logger.info("Shutdown (Ctrl+C)")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
    finally:
        logger.info("Bot stopped")

if __name__ == "__main__":
    asyncio.run(main())