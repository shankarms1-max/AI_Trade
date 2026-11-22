"""AST_FUTURES_V1_DIRECT: frozen 22/3.9 NIFTY futures PAPER strategy. No optimizer, options or orders."""

from datetime import time

STRATEGY_ID = "AST_FUTURES_V1_DIRECT"
VERSION = STRATEGY_ID
CAPTURE_MODE = "AST_DIRECT_PAPER"
ATR_PERIOD = 22
MULTIPLIER_TENTHS = 39
BAR_SECONDS = 300
OPENING_RANGE_START = time(9, 15)
OPENING_RANGE_END = time(9, 30)
SESSION_EXIT_TIME = time(15, 20)
SESSION_CLOSE_TIME = time(15, 30)
FILL_BASIS = "FUTURES_LTP_PAPER_V1"
FILL_BASIS_NOTE = (
    "Observed NIFTY futures LTP used as a paper fill proxy. The capture schema exposes no trustworthy "
    "futures bid/ask or depth, so no spread or executable quantity is represented."
)
