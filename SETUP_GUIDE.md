# Multi-Machine Setup Guide

This guide explains how to set up the Crypto Quant Bot on multiple computers with different `.env` files (API keys) but shared code.

## Overview

| Component | Shared? | Why |
|-----------|---------|-----|
| **Code** | ✅ Yes | Git repository |
| **.env file** | ❌ No | Machine-specific API keys |
| **Database** | ❌ No | Each machine has its own |
| **Logs** | ❌ No | Local to each machine |

## Setup Steps

### 1. Clone Repository (on each machine)

```bash
# On Machine A (e.g., Desktop)
cd C:\Users\Gionie\Documents\GitHub
git clone https://github.com/YOUR_USERNAME/Crypto_Quant_Bot.git
cd Crypto_Quant_Bot

# On Machine B (e.g., Laptop)
cd C:\Users\YourName\Projects
git clone https://github.com/YOUR_USERNAME/Crypto_Quant_Bot.git
cd Crypto_Quant_Bot
```

### 2. Create Virtual Environment

```bash
# On each machine
python -m venv venv

# Windows
.\venv\Scripts\activate

# Linux/Mac
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure .env (MACHINE-SPECIFIC)

Create a `.env` file on **each machine** with your API keys:

```bash
# Machine A - Desktop (Windows)
cp .env.example .env
# Edit .env with your Binance API keys for this machine

# Machine B - Laptop (Windows)
cp .env.example .env
# Edit .env with YOUR Binance API keys for this machine
```

**Important**: Each machine must have its own `.env` file. The `.gitignore` prevents these from being committed to Git.

### 4. Database Setup

Each machine has its own `crypto_bot.db`:

```bash
# On Machine A
python -c "from engine.database import init_db; init_db()"

# On Machine B  
python -c "from engine.database import init_db; init_db()"
```

**Note**: Bots and trades are NOT shared between machines. Each machine manages its own positions independently.

### 5. Verify Setup

```bash
# Run the resume script
python resume_session.py

# Should show:
# ✅ Python Environment
# ✅ Configuration
# ✅ Database
# ✅ Ownership System
```

## Usage

### Starting the Bot

```bash
# Activate virtual environment
.\venv\Scripts\activate

# Start the bot
python engine/runner.py

# Or use the resume script
python resume_session.py --start-bot
```

### Running as Windows Service

```bash
# Install as service (run as Administrator)
python service_manager.py install

# Start the service
net start CryptoQuantBot

# Stop the service
net stop CryptoQuantBot
```

## Cross-Machine Workflow

### When at Machine A (Desktop)

1. Make code changes
2. Test locally
3. Commit and push:
   ```bash
   git add .
   git commit -m "Describe your changes"
   git push origin main
   ```

### When at Machine B (Laptop)

1. Pull latest changes:
   ```bash
   git pull origin main
   ```
2. Your `.env` and `crypto_bot.db` remain unchanged
3. Restart the bot to apply new code

### Key Points

- **API Keys**: Each machine has its own (in `.env`)
- **Database**: Each machine has its own (`crypto_bot.db`)
- **Bot Configs**: Stored in database, NOT shared
- **Code**: Shared via Git, same on all machines

## Troubleshooting

### "Module not found" errors

```bash
# Make sure venv is activated
.\venv\Scripts\activate

# Reinstall dependencies
pip install -r requirements.txt
```

### "API key not found" errors

```bash
# Check .env file exists
dir .env

# Verify contents
cat .env
```

### Database errors

```bash
# Backup existing database first!
copy crypto_bot.db crypto_bot.db.backup

# Reinitialize
python -c "from engine.database import init_db; init_db()"
```

### Ownership state issues

```bash
# Reset ownership state (keeps bots and trades)
python -c "
from engine.ownership import init_ownership_tables
init_ownership_tables()
print('Ownership tables reset')
"

# Full reset (WARNING: loses ownership history)
python -c "
from engine.database import get_connection
conn = get_connection()
conn.execute('DELETE FROM bot_ownership_state')
conn.execute('DELETE FROM bot_ownership_history')
conn.commit()
conn.close()
print('All ownership data deleted')
"
```

## Best Practices

1. **Always pull before making changes** on a new machine
2. **Test changes on one machine first** before pushing
3. **Keep .env backed up** (but never commit to Git!)
4. **Document API key rotations** in your password manager
5. **Use separate API keys** for each machine if possible

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    GitHub Repository                        │
│                    (Shared Code)                            │
└─────────────────────────┬───────────────────────────────────┘
                          │
          ┌───────────────┴───────────────┐
          │                               │
    ┌─────▼─────┐                   ┌─────▼─────┐
    │ Machine A │                   │ Machine B │
    │  Desktop  │                   │  Laptop   │
    └─────┬─────┘                   └─────┬─────┘
          │                               │
    ┌─────▼─────┐                   ┌─────▼─────┐
    │  .env     │                   │  .env     │
    │ (API Keys)│                   │ (API Keys)│
    └───────────┘                   └───────────┘
    ┌─────▼─────┐                   ┌─────▼─────┐
    │crypto_bot │                   │crypto_bot │
    │   .db     │                   │   .db     │
    └───────────┘                   └───────────┘
    ┌─────▼─────┐                   ┌─────▼─────┐
    │   venv/   │                   │   venv/   │
    └───────────┘                   └───────────┘
          │                               │
          └───────────┬───────────────────┘
                      │
              Different exchange accounts
              Different positions
              Different everything
```

## Quick Reference Commands

```bash
# Clone on new machine
git clone <repo-url>
cd Crypto_Quant_Bot
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
python -c "from engine.database import init_db; init_db()"
python resume_session.py
```
