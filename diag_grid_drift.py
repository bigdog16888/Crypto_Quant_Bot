import sys, os, json
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.strategies.martingale_strategy import MartingaleStrategy

conn = get_connection()
c = conn.cursor()
c.execute("SELECT b.name, b.direction, b.config, t.total_invested, t.avg_entry_price, t.current_step FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.pair LIKE '%BTC%' OR b.pair LIKE '%SUI%'")
for r in c.fetchall():
    name, direction, config_raw, inv, entry, step = r
    cfg = json.loads(config_raw)
    
    # Check what parameters are present
    print(f"\nBot: {name} (Dir={direction}, Entry={entry:.4f})")
    for k in ['base_grid', 'grid_dist_pct', 'StepPct', 'UseATRSpacing', 'SpacingType']:
        print(f"  {k}: {cfg.get(k)}")
        
    strategy = MartingaleStrategy(cfg)
    grid_1 = strategy.calculate_next_grid_price(direction, entry, entry, step, None)
    grid_2 = strategy.calculate_next_grid_price(direction, entry * 1.01, entry, step, None)
    
    print(f"  Calc Grid w/ current_price = entry:      {grid_1}")
    print(f"  Calc Grid w/ current_price = entry+1%:   {grid_2}")
    if grid_1 != grid_2:
        print("  ⚠️ Grid price vibrates with current_price!")

conn.close()
