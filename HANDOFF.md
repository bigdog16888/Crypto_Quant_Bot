# Project Handoff: Crypto Quant Bot

**Last Updated**: 2025-12-29
**Status**: Phase 6 Started (Bot Manager UI Implemented)

## Current State
- **UI**: Streamlit interface is fully functional with 3 tabs: Monitor, Creator, Manager.
- **Engine**: Runnner logic (`runner.py`) is verified in Dry Run mode.
- **Database**: SQLite (`crypto_bot.db`) is initialized and populated with test bots.
- **Environment**: Dry Run mode (`DRY_RUN=True`) is active.

## How to Resume
1.  **Clone/Copy** this directory to the new machine.
2.  **Environment Setup**:
    ```bash
    pip install -r requirements.txt
    ```
    *(Ensure `pandas`, `streamlit`, `ccxt`, `pandas_ta`, `plotly` are installed)*.
3.  **Clean Start**:
    - Delete `engine.pid` (if it exists).
    - Delete `engine.log` (optional, to clear history).
    - `crypto_bot.db` contains your bot config. Keep it to preserve bots, or delete it to start fresh.
4.  **Run Application**:
    ```bash
    streamlit run ui/app.py
    ```
5.  **Next Steps (Phase 6)**:
    - [ ] Verify `ui/views/bot_manager.py` functionality (Toggle/Delete should work now).
    - [ ] Update `bot_creator.py` to include "Pro" settings (Hedging, Dollar TP).
    - [ ] Update `runner.py` to implement the logic for those new settings.

## Important Notes
- **Import Error Fixed**: `engine/database.py` was updated to include `get_all_bots`. If you see an import error, ensure the file matches the latest version in this repo.
- **Process Management**: The engine runs as a detached subprocess. Use the "Stop Engine" button in the Sidebar to kill it cleanly.
