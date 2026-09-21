import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv
load_dotenv()
import sqlite3
from engine.position_ledger import _get_bot_checkpoint, compute_pair_position
conn = sqlite3.connect("crypto_bot.db")
c = conn.cursor()
print("=== BOTS FOR XAU ===")
for r in c.execute("SELECT id, name, pair, direction, is_active FROM bots WHERE pair LIKE '%XAU%'").fetchall():
    print(" ", r)
print()
print("=== ACTIVE_POSITIONS FOR XAU ===")
for r in c.execute("SELECT bot_id, pair, side, size, last_checked FROM active_positions WHERE pair LIKE '%XAU%'").fetchall():
    print(" ", r)
print()
print("=== CHECKPOINT LOOKUP RESULT ===")
for bot_id in [10019, 100319]:
    cp = _get_bot_checkpoint(conn, bot_id)
    print("Bot", bot_id, "checkpoint:", cp)
print()
print("=== compute_pair_position RESULT ===")
pp = compute_pair_position("XAU/USDT:USDT", conn=conn)
print("XAU/USDT:USDT:", pp)
pp_short = compute_pair_position("XAUUSDT", conn=conn)
print("XAUUSDT:", pp_short)
conn.close()
