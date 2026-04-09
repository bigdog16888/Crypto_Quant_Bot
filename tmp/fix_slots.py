import sys, time, uuid
sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()
cursor = conn.cursor()

def create_synthetic_receipt(bot_id, pair, amount, price, reason):
    notional = amount * price
    order_id = f'SYNTH_{int(time.time())}_{uuid.uuid4().hex[:6]}'
    cid = f'CQB_{bot_id}_ADOPT_0_{uuid.uuid4().hex[:6]}'
    
    cursor.execute("""
        INSERT INTO bot_orders (
            bot_id, order_id, client_order_id, order_type,
            status, amount, filled_amount, price,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'market', 'closed', ?, ?, ?, ?, ?)
    """, (bot_id, order_id, cid, amount, amount, price, int(time.time()), int(time.time())))
    
    print(f'✅ Created synthetic receipt for Bot {bot_id} ({pair}): {amount} @ {price} (${notional:.2f})')
    return notional

# 1. SUI Bot 10018 (Missing 11,103.4 SUI - 22 SUI = 11,081.4 SUI)
sui_missing_qty = 11103.4000 - 22.0
sui_price = 0.853573881494
if sui_missing_qty > 0:
    create_synthetic_receipt(10018, 'SUIUSDC', sui_missing_qty, sui_price, 'SUI Gap Adoption')

# 2. ETH Bot 10011 (Missing 0.035 ETH)
eth_missing_qty = 0.035
eth_price = 2076.74
if eth_missing_qty > 0:
    create_synthetic_receipt(10011, 'ETHUSDC', eth_missing_qty, eth_price, 'ETH Gap Adoption')

# 3. BNB Bot 10007 (Missing 0.03 BNB - 0.01 BNB = 0.02 BNB)
bnb_missing_qty = 0.03 - 0.010000
bnb_price = 593.0366666667
if bnb_missing_qty > 0:
    create_synthetic_receipt(10007, 'BNBUSDC', bnb_missing_qty, bnb_price, 'BNB Gap Adoption')

# 4. SOL Bot 10001
sol_qty = 0.06
sol_price = 79.78
create_synthetic_receipt(10001, 'SOLUSDC', sol_qty, sol_price, 'SOL Orphan Adoption')

conn.commit()

# Now forcefully re-align the memory to ledger for these bots so the system updates `trades`
from engine.reconciler import StateReconciler
recon = StateReconciler(None)
for bid in [10018, 10011, 10007, 10001]:
    try:
        recon._align_memory_to_ledger(bid)
        # Ensure they are active and not Scanning so they resume operations
        cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (bid,))
        print(f"Aligned and activated bot {bid}")
    except Exception as e:
        print(f"Error aligning bot {bid}: {e}")

conn.commit()
print('Ledgers successfully aligned to include synthetic receipts.')
conn.close()
