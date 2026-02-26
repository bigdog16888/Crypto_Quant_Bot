from engine.exchange_interface import ExchangeInterface
import json
import sqlite3

def audit():
    ex = ExchangeInterface()
    print("--- RAW EXCHANGE POSITIONS ---")
    res = ex._raw_request('/fapi/v2/account')
    if res and 'positions' in res:
        for p in res['positions']:
            amt = float(p.get('positionAmt', 0))
            if amt != 0:
                print(f"SYMBOL: {p['symbol']}, AMT: {amt}, PRICE: {p['entryPrice']}")
    else:
        print("Failed to fetch positions.")

    print("\n--- DB TRADES STATE ---")
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT bot_id, pair, direction, total_invested FROM trades WHERE total_invested > 0")
    for row in cursor.fetchall():
        print(f"BOT {row[0]} | {row[1]} | {row[2]} | INVESTED: {row[3]}")
    conn.close()

if __name__ == '__main__':
    audit()
