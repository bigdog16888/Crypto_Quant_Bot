import sys, os, json
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()
c.execute("SELECT name, config FROM bots WHERE pair LIKE '%BTC%' OR pair LIKE '%SUI%'")
for r in c.fetchall():
    cfg = json.loads(r[1])
    # Extract grid calculation related fields
    spacing_type = cfg.get('SpacingType', 'unknown')
    atr = cfg.get('UseATRSpacing', 'Not Set')
    step_pct = cfg.get('StepPct', 'Not Set')
    print(f"Bot: {r[0]}")
    print(f"  SpacingType: {spacing_type}")
    print(f"  UseATRSpacing: {atr}")
    print(f"  StepPct: {step_pct}")

conn.close()
