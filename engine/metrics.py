import logging
import threading
import time
import pandas as pd
import io
from prometheus_client import Gauge, start_http_server, REGISTRY

from config.settings import config
from engine.database import get_connection

logger = logging.getLogger("MetricsServer")

# Prometheus Gauges - Safe registration to handle Streamlit reloads
def get_gauge(name, documentation, labelnames=None):
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return Gauge(name, documentation, labelnames or [])

BOT_CYCLE_TIME = get_gauge('bot_cycle_time_seconds', 'Time taken for one bot cycle')
BOT_ACTIVE_COUNT = get_gauge('bot_active_count', 'Number of active bots')
BOT_IN_TRADE_COUNT = get_gauge('bot_in_trade_count', 'Number of bots currently in trade')
ACCOUNT_EQUITY = get_gauge('account_equity_usd', 'Total account equity in USD')
ACCOUNT_DRAWDOWN_PCT = get_gauge('account_drawdown_percent', 'Percentage drawdown from initial equity')
ORDER_COUNT_DAILY = get_gauge('bot_order_count_daily', 'Daily order count per bot', ['bot_id', 'bot_name'])
ORDER_COUNT_CYCLE = get_gauge('bot_order_count_cycle', 'Orders placed in current cycle per bot', ['bot_id', 'bot_name'])

def export_trade_history(bot_id: int | None = None, format: str = 'csv') -> str:
    """
    Exports trade history from DB.
    Returns a CSV string.
    """
    logger.info(f"[METRICS] Exporting trade history for bot: {bot_id if bot_id else 'All'} (Format: {format})")
    try:
        conn = get_connection()
        query = "SELECT * FROM trade_history"
        if bot_id:
            query += f" WHERE bot_id = {bot_id}"
        query += " ORDER BY timestamp DESC"
        
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        if format == 'csv':
            return df.to_csv(index=False)
        return ""
    except Exception as e:
        logger.error(f"Failed to export trade history: {e}")
        return ""

class MetricsServer(threading.Thread):
    def __init__(self, port=config.METRICS_PORT):
        super().__init__()
        self.port = port
        self.running = False
        self.daemon = True # Allow main program to exit even if thread is running

    def run(self):
        self.running = True
        try:
            start_http_server(self.port)
            logger.info(f"Prometheus metrics server started on port {self.port}")
            while self.running:
                time.sleep(1) # Keep thread alive
        except Exception as e:
            logger.error(f"Failed to start or run Prometheus metrics server: {e}")
        finally:
            self.running = False

    def stop(self):
        self.running = False
        logger.info("Prometheus metrics server stopped.")
