#!/usr/bin/env python3
"""
SAFE STOP for the legacy options alert.

The previous script analysed Binance spot candles and labelled the result as
an options signal without an option contract, expiry, strike, or executable
option premium. That is not a valid options alert. Do not send Telegram signals
until a verified option-chain feed is integrated.
"""
import sys

print(
    "BLOCKED: legacy options alerts are paused. "
    "This workflow has no verified CoinSwitch option-chain premium feed; "
    "spot candles must never be presented as option BUY CALL/PUT signals. "
    "No Telegram message or order was sent.",
    file=sys.stderr,
)
sys.exit(2)
