#!/usr/bin/env python3
"""
Database Backup Script for Crypto Quant Bot

Creates timestamped backups of the database before any major operations.
Backups are stored in the 'backups/' folder.

Usage:
    python tools/backup_db.py              # Create backup
    python tools/backup_db.py --restore    # Restore from latest backup
    python tools/backup_db.py --list       # List available backups
"""

import sqlite3
import os
import shutil
import argparse
from datetime import datetime
from pathlib import Path

# Configuration
BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "crypto_bot.db"
BACKUP_DIR = BASE_DIR / "backups"
MAX_BACKUPS = 10  # Keep last 10 backups


def ensure_backup_dir():
    """Create backup directory if it doesn't exist."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print(f"📁 Backup directory: {BACKUP_DIR}")


def create_backup():
    """Create a timestamped backup of the database."""
    if not DB_PATH.exists():
        print(f"❌ Database not found: {DB_PATH}")
        return False
    
    ensure_backup_dir()
    
    # Generate timestamped filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"crypto_bot_{timestamp}.db"
    backup_path = BACKUP_DIR / backup_name
    
    try:
        # Create backup using SQLite backup API
        conn = sqlite3.connect(str(DB_PATH))
        backup_conn = sqlite3.connect(str(backup_path))
        
        # Backup the database
        with backup_conn:
            conn.backup(backup_conn)
        
        conn.close()
        backup_conn.close()
        
        # Get file size
        size_mb = backup_path.stat().st_size / (1024 * 1024)
        
        print(f"✅ Backup created: {backup_name}")
        print(f"   Size: {size_mb:.2f} MB")
        
        # Clean up old backups
        cleanup_old_backups()
        
        return True
        
    except Exception as e:
        print(f"❌ Backup failed: {e}")
        return False


def cleanup_old_backups():
    """Remove old backups beyond MAX_BACKUPS."""
    if not BACKUP_DIR.exists():
        return
    
    # Get all backup files sorted by modification time
    backups = sorted(BACKUP_DIR.glob("crypto_bot_*.db"), key=os.path.getmtime)
    
    # Remove oldest if we have too many
    if len(backups) > MAX_BACKUPS:
        to_remove = backups[:len(backups) - MAX_BACKUPS]
        for old_backup in to_remove:
            try:
                old_backup.unlink()
                print(f"🗑️  Removed old backup: {old_backup.name}")
            except Exception as e:
                print(f"⚠️  Could not remove {old_backup.name}: {e}")


def list_backups():
    """List all available backups."""
    ensure_backup_dir()
    
    backups = sorted(BACKUP_DIR.glob("crypto_bot_*.db"), key=os.path.getmtime, reverse=True)
    
    if not backups:
        print("📭 No backups found.")
        return
    
    print(f"\n📚 Available Backups ({len(backups)} total):\n")
    
    for i, backup in enumerate(backups):
        size_mb = backup.stat().st_size / (1024 * 1024)
        mtime = datetime.fromtimestamp(backup.stat().st_mtime)
        
        marker = "← LATEST" if i == 0 else ""
        print(f"  {i+1}. {backup.name} ({size_mb:.2f} MB) - {mtime} {marker}")
    
    print()


def restore_backup(backup_name=None):
    """Restore database from a backup."""
    ensure_backup_dir()
    
    # Find the backup to restore
    if backup_name is None:
        # Use the latest backup
        backups = sorted(BACKUP_DIR.glob("crypto_bot_*.db"), key=os.path.getmtime, reverse=True)
        if not backups:
            print("❌ No backups found.")
            return False
        backup_path = backups[0]
        print(f"Using latest backup: {backup_path.name}")
    else:
        backup_path = BACKUP_DIR / backup_name
        if not backup_path.exists():
            print(f"❌ Backup not found: {backup_name}")
            return False
    
    # Create a backup of the current state first
    print("📸 Creating backup of current state...")
    create_backup()
    
    # Restore
    try:
        # Close any existing connections
        # Copy backup to main database
        shutil.copy2(str(backup_path), str(DB_PATH))
        print(f"✅ Restored from: {backup_path.name}")
        return True
    except Exception as e:
        print(f"❌ Restore failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Crypto Quant Bot Database Backup Tool")
    parser.add_argument("--restore", action="store_true", help="Restore from latest backup")
    parser.add_argument("--list", action="store_true", help="List available backups")
    parser.add_argument("--name", type=str, help="Specific backup to restore (with --restore)")
    
    args = parser.parse_args()
    
    print("\n" + "="*60)
    print("  CRYPTO QUANT BOT - DATABASE BACKUP TOOL")
    print("="*60 + "\n")
    
    if args.list:
        list_backups()
    elif args.restore:
        restore_backup(args.name)
    else:
        create_backup()
    
    print()


if __name__ == "__main__":
    main()
