import sys, json
sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()
bots = conn.execute("""
    SELECT b.id, b.name, b.config
    FROM bots b JOIN trades t ON b.id=t.bot_id
    WHERE t.total_invested > 0 AND b.is_active=1
    LIMIT 3
""").fetchall()
conn.close()

for bot_id, name, config_str in bots:
    print(f"\n=== {name} ({bot_id}) ===")
    try:
        cfg = json.loads(config_str)
        for k, v in cfg.items():
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"  Error: {e}")
        print(f"  Raw: {config_str[:200]}")
