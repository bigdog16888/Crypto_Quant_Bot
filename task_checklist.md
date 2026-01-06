# Tasks

- [ ] Project Assessment & Planning
    - [x] Explore current file structure and verify modularity <!-- id: 0 -->
    - [x] Review existing Streamlit entry point and UI layout <!-- id: 1 -->
    - [x] Verify SQLite schema design and integration <!-- id: 2 -->
    - [x] Create Implementation Plan for structural/UI improvements <!-- id: 3 -->
    - [x] Request user review of Implementation Plan <!-- id: 4 -->

- [ ] Scalability & Extensibility Assessment
    - [x] Review strategy design pattern in `engine/strategy.py`
    - [x] Check indicator implementation
    - [x] Evaluate symbol/pair handling for USDC support

- [ ] UI Refactoring <!-- id: 5 -->
    - [x] Create `ui/views/monitor.py` <!-- id: 6 -->
    - [x] Create `ui/views/bot_creator.py` <!-- id: 7 -->
    - [x] Refactor `ui/app.py` to use new views <!-- id: 8 -->
    - [x] Delete `ui/dashboard.py` <!-- id: 9 -->
    - [x] Verify functionality <!-- id: 10 -->

- [ ] Architecture Upgrade Implementation <!-- id: 11 -->
    - [x] Create `engine/strategies/` directory and `base.py` <!-- id: 12 -->
    - [x] Port MQL4 logic to `engine/strategies/mql4_strategy.py` <!-- id: 13 -->
    - [x] Create `engine/strategies/market_maker.py` template <!-- id: 14 -->
    - [x] Update `engine/exchange_interface.py` for Futures/USDC <!-- id: 15 -->
    - [x] Update `ui/views/bot_creator.py` for market selection <!-- id: 16 -->
    - [x] Check and update `engine/order_manager.py` dependencies <!-- id: 16b -->
    - [x] Verify architecture changes <!-- id: 17 -->

- [ ] Architectural Oversight & Preparation <!-- id: 18 -->
    - [x] Audit `engine/indicators.py` for modularity <!-- id: 19 -->
    - [x] Audit `engine/risk.py` for safety and flexibility <!-- id: 20 -->
    - [x] Establish coding standards for Quant/Executor agents <!-- id: 21 -->

- [x] Phase 2 Implementation & UI Enhancements <!-- id: 22 -->
    - [x] specific `engine/risk.py` updates (ATR Grid) <!-- id: 23 -->
    - [x] Create `engine/manager.py` (Early Exit) <!-- id: 24 -->
    - [x] Implement `config/settings.py` <!-- id: 25 -->
    - [x] Implement Live Charts in `ui/views/monitor.py` <!-- id: 26 -->
    - [x] Verify Phase 2 with `verify_advanced.py` <!-- id: 27 -->

- [x] Phase 3 Integration & UI Parameter Expansion <!-- id: 28 -->
    - [x] Implement `check_moving_profit_target` & `check_hedge_entry` in `manager.py` <!-- id: 29 -->
    - [x] Add Stoch, MACD, MA to `mql4_strategy.py` <!-- id: 30 -->
    - [x] Update `ui/views/bot_creator.py` with full parameter suite <!-- id: 31 -->
    - [x] Verify Phase 3 logic <!-- id: 32 -->

- [x] Multi-Timeframe Strategy & UI Fixes <!-- id: 38 -->
    - [x] Fix `st.form` nesting error in `bot_creator.py` <!-- id: 39 -->
    - [x] Add per-indicator timeframe selectors in UI <!-- id: 40 -->
    - [x] Update `mql4_strategy.py` to handle multi-timeframe data <!-- id: 41 -->
    - [x] Verify multi-timeframe logic <!-- id: 42 -->

- [x] System Cleanup & Architecture Review <!-- id: 43 -->
    - [x] Review project structure for modularity <!-- id: 44 -->
    - [x] Clean up deprecated/placeholder files <!-- id: 45 -->
    - [x] Consolidate verified features into master branch <!-- id: 46 -->
- [x] Prepare clean slate for Next Steps <!-- id: 47 -->

- [x] Phase 4: Bot Execution Engine <!-- id: 48 -->
    - [x] Create `engine/runner.py` (Main Loop) <!-- id: 49 -->
    - [x] Implement `load_active_bots()` from DB <!-- id: 50 -->
    - [x] Integrate `ExchangeInterface` with Runner <!-- id: 51 -->
    - [x] Integrate `MQL4Strategy` signal check into Runner <!-- id: 52 -->
    - [x] Implement Trade Execution (Place Order) <!-- id: 53 -->
    - [x] Implement Bot Loop Control in UI (Start/Stop) <!-- id: 54 -->
    - [x] Verify End-to-End Execution (Dry Run) <!-- id: 55 -->

- [x] Phase 5: Live Verification & Polish <!-- id: 56 -->
    - [x] Fix UI Crash on "Stop Engine" (Windows Subprocess Detachment) <!-- id: 57 -->
    - [x] Restart Streamlit App <!-- id: 58 -->
    - [x] Deploy Test Bot (Paper Mode) <!-- id: 59 -->
    - [x] Monitor Bot Execution in Logs <!-- id: 60 -->
    - [x] Visualize Active Bot Levels on Chart <!-- id: 61 -->

- [x] Phase 6: Advanced Bot Management & Configuration <!-- id: 62 -->
    - [x] Create `ui/views/bot_manager.py` (List/Delete/Toggle) <!-- id: 63 -->
    - [x] Integrate Bot Manager into `app.py` <!-- id: 64 -->
    - [x] Audit `mql4_strategy.py` for Hedging/DollarTP logic <!-- id: 65 -->
    - [x] Update `bot_creator.py` with Advanced Settings (Hedging, Dollar TP) <!-- id: 66 -->
    - [x] Verify Advanced Settings Persistence <!-- id: 67 -->
    - [x] Upgrade `runner.py` with TP/Grid/Hedge execution logic <!-- id: 68 -->

- [x] Phase 7: Advanced Confluence & Risk (Latest)
    - [x] Implement 11-Trigger System in `mql4_strategy.py`
    - [x] Implement "Emergency Stop" file logic in `runner.py`
    - [x] Implement "Accelerated Early Exit" in `manager.py`
    - [x] Update UI with "JSON View" and Advanced Triggers
    - [x] Verify Advanced Logic (`tests/test_advanced_logic.py`)
