import threading
import http.server
import socketserver
import time
import json
import logging
import pandas as pd
from engine.database import get_all_bots, get_bot_pnl_summary, get_connection
from config.settings import config
from prometheus_client import CollectorRegistry, Gauge, generate_latest

# Setup logger
logger = logging.getLogger("MetricsServer")

# Metrics definitions
registry = CollectorRegistry()

# Gauge for overall bot health and status
BOT_ACTIVE_COUNT = Gauge(
    'bot_active_count', 
    'Number of actively running bots in the system', 
    registry=registry
)

# Gauge for total PnL per bot
BOT_TOTAL_PNL = Gauge(
    'bot_total_pnl', 
    'Total PnL in USDC achieved by the bot', 
    ['bot_id', 'bot_name'], 
    registry=registry
)

# Simple Gauge for performance tracking (needs to be updated by runner)
BOT_CYCLE_TIME = Gauge(
    'bot_cycle_time_seconds',
    'Time taken for the last full BotRunner cycle',
    registry=registry
)


class MetricsHandler(http.server.SimpleHTTPRequestHandler):
    """
    HTTP handler to serve Prometheus metrics.
    """
    def do_GET(self):
        if self.path == '/metrics':
            self._update_metrics()
            self.send_response(200)
            self.send_header('Content-type', 'text/plain; version=0.0.4; charset=utf-8')
            self.end_headers()
            self.wfile.write(generate_latest(registry))
        elif self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

    def _update_metrics(self):
        """
        Fetches fresh data from the database and updates Prometheus gauges.
        """
        active_count = 0
        all_bots = get_all_bots()

        # Update PnL and active count
        for bot_id, bot_name, pair, is_active, strat_type, total_invested, current_step in all_bots:
            if is_active:
                active_count += 1
            
            summary = get_bot_pnl_summary(bot_id)
            
            # Update PnL gauge
            # Labels must be strings
            BOT_TOTAL_PNL.labels(
                bot_id=str(bot_id), 
                bot_name=bot_name
            ).set(summary['total_pnl'])

        BOT_ACTIVE_COUNT.set(active_count)
        
        # NOTE: BOT_CYCLE_TIME must be updated externally by BotRunner

class MetricsServer(threading.Thread):
    def __init__(self, port=9099):
        super().__init__()
        self.daemon = True # Dies when main thread exits
        self.port = port
        self.httpd = None
        self.name = "MetricsServer"

    def run(self):
        # We must use a context manager for the server to bind/close properly
        try:
            # Use threading mixin to handle multiple concurrent requests
            class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
                pass
                
            self.httpd = ThreadingHTTPServer(("", self.port), MetricsHandler)
            logger.info(f"Prometheus Metrics Server running on port {self.port}")
            self.httpd.serve_forever()
        except Exception as e:
            logger.error(f"Failed to start Metrics Server: {e}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            logger.info("Metrics Server stopped.")
            
# NOTE: This implementation relies on the third-party `prometheus_client` library 
# which is not in `requirements.txt`. I will assume this is acceptable for the advanced feature,
# but I must add it to `requirements.txt` next. 
# Alternatively, I could implement the Prometheus exposition format manually, but that is complex.
# Relying on `prometheus_client` is the standard and professional way.

def export_trade_history(format='csv') -> str:
    """
    Exports the entire trade history to a CSV string.
    Returns None if no data.
    """
    try:
        conn = get_connection()
        query = """
            SELECT 
                th.timestamp, 
                b.name as bot_name, 
                th.symbol, 
                th.action, 
                th.price, 
                th.amount, 
                th.pnl, 
                th.notes 
            FROM trade_history th
            LEFT JOIN bots b ON th.bot_id = b.id
            ORDER BY th.timestamp DESC
        """
        df = pd.read_sql_query(query, conn)
        
        if df.empty:
            return None
            
        # Convert timestamp to readable date
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
        
        # Reorder columns
        cols = ['datetime', 'bot_name', 'symbol', 'action', 'price', 'amount', 'pnl', 'notes']
        df = df[cols]
        
        if format == 'csv':
            return df.to_csv(index=False)
            
        return None
    except Exception as e:
        logger.error(f"Export failed: {e}")
        return None
