# Version Backup Guide

Use this before major upgrades, config changes, or when the system reaches a stable green state.

## Quick backup

```bat
create_backup.bat
```

Output: `backups/Crypto_Quant_Bot_v4.3.8_YYYY-MM-DD_HHMM.zip`

## What is included

| Included | Excluded |
|----------|----------|
| Full source tree | `scratch/` (local debug) |
| `crypto_bot.db` (WAL checkpointed) | `backups/` (avoid nesting) |
| `BACKUP_MANIFEST.txt` inside zip | `engine.log`, `*.pid`, `__pycache__` |
| Config JSON, tests, docs | `.pytest_cache`, `.gemini` |

## Include API keys (optional)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\create_version_backup.ps1 -IncludeEnv
```

**Warning:** The zip will contain `.env` secrets. Store offline and never commit.

## Restore procedure

1. Stop the engine (dashboard → Stop Monitoring, or `restart_runner.bat`).
2. Copy your current `crypto_bot.db` somewhere safe.
3. Extract the zip to a folder.
4. Copy `crypto_bot.db` from the archive over the live DB.
5. `pip install -r requirements.txt`
6. `run_stack.bat`

## Pre-backup checklist (green system)

- [ ] Dashboard shows **HEALTHY** / zero parity mismatches
- [ ] No bots stuck in `REQUIRE_MANUAL_PROOF`
- [ ] `python check_state.py` reports **SYSTEM HEALTHY**
- [ ] `pytest` passes (optional but recommended)

## Version reference

Current version is defined in:
- `config/settings.py` → `Config.VERSION`
- `CODEBASE_GUIDE.md` header
- `docs/CHANGELOG.md`
