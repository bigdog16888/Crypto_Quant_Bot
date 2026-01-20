#!/usr/bin/env python3
"""
Investigation Context Loader
Run this at the START of any investigation session to restore context.

Usage:
    python load_context.py
    python load_context.py --diagnostic
    python load_context.py --checklist
"""

import os
import sys
import sqlite3
from pathlib import Path

CONTEXT_FILE = Path(__file__).parent / "CRASH_INVESTIGATION_CONTEXT.md"
CHECKLIST_FILE = Path(__file__).parent / "QUICK_INVESTIGATION_CHECKLIST.md"
DB_PATH = Path(__file__).parent / "crypto_bot.db"

def print_file(path, title):
    """Print file contents with title"""
    if path.exists():
        print(f"\n{'='*60}")
        print(f" {title}")
        print(f"{'='*60}")
        print(path.read_text())
    else:
        print(f"\n[WARN] {title} not found at {path}")

def quick_diagnostic():
    """Run quick database diagnostic (memory efficient)"""
    print(f"\n{'='*60}")
    print(" QUICK DATABASE DIAGNOSTIC")
    print(f"{'='*60}")
    
    if not DB_PATH.exists():
        print(f"[WARN] Database not found at {DB_PATH}")
        return
    
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        checks = [
            ("Running Bots", "SELECT COUNT(*) FROM bots WHERE is_active=1"),
            ("Bots with Invested > 0", "SELECT COUNT(*) FROM trades WHERE total_invested > 0"),
            ("Bots with entry_order_id", "SELECT COUNT(*) FROM trades WHERE entry_order_id IS NOT NULL"),
            ("Bots with tp_order_id", "SELECT COUNT(*) FROM trades WHERE tp_order_id IS NOT NULL"),
            ("Open orders in bot_orders", "SELECT COUNT(*) FROM bot_orders WHERE status='open'"),
            ("Total bots in DB", "SELECT COUNT(*) FROM bots"),
            ("Total trades records", "SELECT COUNT(*) FROM trades"),
        ]
        
        for name, query in checks:
            try:
                cursor.execute(query)
                result = cursor.fetchone()[0]
                print(f"  {name}: {result}")
            except Exception as e:
                print(f"  {name}: ERROR ({e})")
        
        # Show sample of bots with their order status
        print(f"\n  Sample bots with order tracking:")
        cursor.execute("""
            SELECT b.name, b.is_active, t.total_invested,
                   t.entry_order_id, t.tp_order_id
            FROM bots b
            LEFT JOIN trades t ON b.id = t.bot_id
            LIMIT 5
        """)
        for row in cursor.fetchall():
            name, active, invested, entry_id, tp_id = row
            entry_status = "✓" if entry_id else "✗"
            tp_status = "✓" if tp_id else "✗"
            print(f"    {name}: active={active}, invested=${invested:.2f}, entry={entry_status}, tp={tp_status}")
        
        conn.close()
        
    except Exception as e:
        print(f"[ERROR] Database diagnostic failed: {e}")

def main():
    """Main entry point"""
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    
    mode = "all"
    if "--diagnostic" in args:
        mode = "diagnostic"
    elif "--checklist" in args:
        mode = "checklist"
    elif "--help" in args or "-h" in args:
        print(__doc__)
        return
    
    print(f"\n{'#'*60}")
    print(f"# CRYPTO BOT INVESTIGATION - SESSION RESTORE")
    print(f"# {'='*46}")
    print(f"# Time: {os.popen('date /t && time /t').read().strip()}")
    print(f"{'#'*60}")
    
    if mode in ("all", "checklist"):
        print_file(CONTEXT_FILE, "MAIN CONTEXT FILE")
        print_file(CHECKLIST_FILE, "QUICK CHECKLIST")
    
    if mode in ("all", "diagnostic"):
        quick_diagnostic()
    
    print(f"\n{'#'*60}")
    print("# NEXT STEPS:")
    print("# 1. Read CRASH_INVESTIGATION_CONTEXT.md for full context")
    print("# 2. Run 'python load_context.py --diagnostic' for state check")
    print("# 3. Run 'python load_context.py --checklist' for checklist")
    print("# 4. If crash occurs, UPDATE context files before restarting!")
    print(f"{'#'*60}\n")

if __name__ == "__main__":
    main()
