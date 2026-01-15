"""
Clean up ghost trades from database

These bots were created by test_multibot_stress.py with mocked exchange.
They have positions in the trades table but never had real orders placed.
No trade_history entries exist for them, confirming they're ghost trades.
"""
import sqlite3
from engine.database import DB_PATH

print("=" * 80)
print("GHOST TRADE CLEANUP")
print("=" * 80)

conn = sqlite3.connect(DB_PATH, timeout=30.0)
cursor = conn.cursor()

try:
    # Bots identified as ghost trades:
    # 3: test (0G/USDT)
    # 4: test1 (BTC/USDC)
    # 5: StressTest_Bot_0_1768289089 (BTC/USDT)
    # 6: StressTest_Bot_1_1768289089 (BTC/USDT)
    # 7: StressTest_Bot_2_1768289089 (BTC/USDT)
    # 9: StressTest_Bot_4_1768289089 (BTC/USDT)

    ghost_bot_ids = [3, 4, 5, 6, 7, 9]

    print("\nDeleting ghost trades and bots...")
    print("-" * 80)

    for bot_id in ghost_bot_ids:
        # Get bot name for logging
        cursor.execute('SELECT name FROM bots WHERE id = ?', (bot_id,))
        result = cursor.fetchone()

        if result:
            name = result[0]
            print(f"Deleting bot [{bot_id}] {name}...")

            # Delete from trade_history first (FK constraint)
            cursor.execute('DELETE FROM trade_history WHERE bot_id = ?', (bot_id,))

            # Delete from trades (active position)
            cursor.execute('DELETE FROM trades WHERE bot_id = ?', (bot_id,))

            # Delete from bots
            cursor.execute('DELETE FROM bots WHERE id = ?', (bot_id,))

            print(f"  ✅ Deleted trade_history: {cursor.rowcount} records")
            print(f"  ✅ Deleted trades record")
            print(f"  ✅ Deleted bot record")
            print()

    conn.commit()

    print("-" * 80)
    print("GHOST TRADE CLEANUP COMPLETE")
    print("=" * 80)

    # Verify cleanup
    cursor.execute('SELECT COUNT(*) FROM bots WHERE id IN (3,4,5,6,7,9)')
    remaining = cursor.fetchone()[0]

    if remaining == 0:
        print("✅ All ghost trades successfully removed!")
    else:
        print(f"⚠️ WARNING: {remaining} ghost bots still remain")

except Exception as e:
    conn.rollback()
    print(f"❌ Error during cleanup: {e}")
    import traceback
    traceback.print_exc()

finally:
    conn.close()
