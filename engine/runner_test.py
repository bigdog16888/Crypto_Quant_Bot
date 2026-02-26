import time
import logging
import json
import sys
import os
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
import threading
import psutil # Added for robust PID checking
from logging.handlers import RotatingFileHandler

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids, get_starting_equity, update_active_positions_snapshot, update_full_snapshot, update_active_positions
from engine.exchange_interface import ExchangeInterface, normalize_symbol, normalize_market_type
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.manager import manage_trade
from engine.bot_executor import BotExecutor
import engine.bot_executor
from engine.metrics import MetricsServer, BOT_CYCLE_TIME
from engine.integrity import enforce_integrity


from config.settings import config
