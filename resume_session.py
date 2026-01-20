#!/usr/bin/env python3
"""
Crypto Quant Bot - Session Resume Script

Run this after restarting your computer to:
1. Check current system status
2. Verify ownership state
3. Start the bot service

Usage:
    python resume_session.py
    python resume_session.py --check-only
    python resume_session.py --start-bot
"""
import os
import sys
import subprocess
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


def print_header(title: str):
    """Print a formatted header"""
    print(f"\n{'='*70}")
    print(f" {title}")
    print(f"{'='*70}\n")


def check_python_env():
    """Check if Python environment is ready"""
    print("📦 Checking Python environment...")
    
    # Check if venv exists
    venv_python = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
    if os.name == 'nt':  # Windows
        venv_python = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
    else:  # Linux/Mac
        venv_python = PROJECT_ROOT / "venv" / "bin" / "python"
    
    if venv_python.exists():
        print(f"  ✅ Virtual environment found")
        return True
    else:
        print(f"  ⚠️  Virtual environment not found")
        print(f"      Expected: {venv_python}")
        print(f"      Run: python -m venv venv")
        return False


def check_ownership_status():
    """Check current ownership status"""
    print("🏗️  Checking ownership system...")
    
    try:
        from engine.ownership import get_all_active_ownerships, init_ownership_tables
        
        # Initialize tables if needed
        init_ownership_tables()
        
        # Get all active ownerships
        active = get_all_active_ownerships()
        
        if not active:
            print("  ℹ️  No active ownership records")
            return True
        
        print(f"  Found {len(active)} pairs with ownership:\n")
        
        for po in active:
            print(f"  {po.pair}:")
            if po.owner:
                print(f"    🏆 OWNER: Bot {po.owner.bot_id}")
                print(f"       State: {po.owner.state.value}")
                print(f"       Position: ${po.owner.position_size:.2f}")
            else:
                print(f"    🏆 OWNER: None")
            
            if po.passengers:
                print(f"    👥 PASSENGERS: {len(po.passengers)}")
                for p in po.passengers:
                    print(f"       - Bot {p.bot_id}")
        
        return True
        
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False


def check_database():
    """Check database state"""
    print("💾 Checking database...")
    
    db_path = PROJECT_ROOT / "crypto_bot.db"
    
    if not db_path.exists():
        print(f"  ⚠️  Database not found: {db_path}")
        return False
    
    print(f"  ✅ Database exists: {db_path.name}")
    print(f"     Size: {db_path.stat().st_size / 1024:.1f} KB")
    
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        
        # Check bots
        cur.execute("SELECT COUNT(*) FROM bots WHERE is_active=1")
        active_bots = cur.fetchone()[0]
        
        # Check positions
        cur.execute("SELECT COUNT(*) FROM trades WHERE total_invested > 0")
        active_positions = cur.fetchone()[0]
        
        print(f"     Active bots: {active_bots}")
        print(f"     Bots with positions: {active_positions}")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"  ❌ Database error: {e}")
        return False


def check_config():
    """Check configuration files"""
    print("⚙️  Checking configuration...")
    
    env_file = PROJECT_ROOT / ".env"
    
    if env_file.exists():
        print(f"  ✅ .env file exists")
        # Check for required keys
        with open(env_file) as f:
            content = f.read()
            required = ['BINANCE_API_KEY', 'BINANCE_SECRET']
            missing = [k for k in required if k not in content]
            if missing:
                print(f"  ⚠️  Missing keys in .env: {', '.join(missing)}")
            else:
                print(f"     All required API keys present")
        return True
    else:
        print(f"  ❌ .env file not found!")
        print(f"     Copy .env.example to .env and fill in your API keys")
        return False


def start_bot():
    """Start the bot service"""
    print("\n🚀 Starting bot service...")
    
    try:
        # Change to project directory
        os.chdir(PROJECT_ROOT)
        
        # Run the bot
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "engine" / "runner.py")],
            cwd=str(PROJECT_ROOT)
        )
        return result.returncode == 0
        
    except KeyboardInterrupt:
        print("\n⏹️  Bot stopped by user")
        return True
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
        return False


def show_summary(checks: dict):
    """Show status summary"""
    print_header("SESSION RESUME SUMMARY")
    
    all_passed = all(checks.values())
    
    for check, passed in checks.items():
        status = "✅" if passed else "❌"
        print(f"  {status} {check}")
    
    print()
    
    if all_passed:
        print("🚀 System is ready! Run `python engine/runner.py` to start the bot.")
        print("\nOr run this script with --start-bot:")
        print("    python resume_session.py --start-bot")
    else:
        print("⚠️  Some checks failed. Please resolve the issues above.")
    
    return all_passed


def main():
    """Main entry point"""
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    
    print_header("CRYPTO QUANT BOT - SESSION RESUME")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Python: {sys.executable}")
    
    checks = {}
    
    # Run checks unless --start-bot is passed
    if "--start-bot" not in args:
        checks["Python Environment"] = check_python_env()
        checks["Configuration"] = check_config()
        checks["Database"] = check_database()
        checks["Ownership System"] = check_ownership_status()
        
        show_summary(checks)
    
    # Start bot if requested
    if "--start-bot" in args or ("--check-only" not in args and not args):
        if all(checks.values()) or "--check-only" not in args:
            start_bot()


if __name__ == "__main__":
    main()
