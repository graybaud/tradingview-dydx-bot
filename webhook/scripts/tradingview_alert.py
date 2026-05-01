# tradingview_alert.py
"""
Pydantic model for TradingView alert validation and normalization.
"""
from pydantic import BaseModel, Field, field_validator


# TradingView symbol format → dYdX format mapping
SYMBOL_MAPPING = {
    "ETHUSDC": "ETH-USD",
    "BTCUSDC": "BTC-USD",
    "PEPEUSDC": "PEPE-USD",
    "SOLUSDC": "SOL-USD",
}


def map_symbol(symbol: str) -> str:
    """Convert TradingView symbol format to dYdX format."""
    if symbol in SYMBOL_MAPPING:
        return SYMBOL_MAPPING[symbol]
    
    if symbol.endswith("USDC"):
        return symbol.replace("USDC", "-USD")
    
    raise ValueError(f"Unsupported symbol: {symbol}")


class TradingViewAlert(BaseModel):
    """
    Validates and normalizes incoming TradingView webhook alerts.
    All fields are stored as strings to maintain compatibility with Redis consumer.
    """
    ticker: str = Field(..., min_length=1, max_length=20)
    side: str = Field(..., pattern="^(BUY|SELL)$")
    type: str = Field(..., pattern="^(long|short)$")
    size: str
    reduce_only: str = Field(..., pattern="^(true|false)$")
    timestamp: str

    class Config:
        extra = 'forbid'

    @field_validator('ticker')
    @classmethod
    def validate_and_normalize_ticker(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ticker must not be empty")
        return map_symbol(v)

    @field_validator('side', mode='before')
    @classmethod
    def normalize_side(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.upper()
            if v not in ["BUY", "SELL"]:
                raise ValueError("side must be 'buy' or 'sell'")
            return v
        raise ValueError("side must be a string")

    @field_validator('type', mode='before')
    @classmethod
    def normalize_type(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.lower()
            if v not in ["long", "short"]:
                raise ValueError("type must be 'long' or 'short'")
            return v
        raise ValueError("type must be a string")

    @field_validator('size')
    @classmethod
    def validate_size(cls, v: str) -> str:
        try:
            if float(v) <= 0:
                raise ValueError("size must be positive")
            return v
        except (ValueError, TypeError):
            raise ValueError("size must be a valid positive number")

    @field_validator('reduce_only', mode='before')
    @classmethod
    def normalize_reduce_only(cls, v) -> str:
        if isinstance(v, str):
            v = v.lower()
            if v not in ["true", "false"]:
                raise ValueError("reduce_only must be 'true' or 'false'")
            return v
        raise ValueError("reduce_only must be a string")

    @field_validator('timestamp')
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        try:
            ts = int(v)
            if not (1577836800 <= ts <= 4102444800):
                raise ValueError("timestamp out of valid range (2020-2100)")
            return v
        except (ValueError, TypeError):
            raise ValueError("timestamp must be a valid Unix timestamp")