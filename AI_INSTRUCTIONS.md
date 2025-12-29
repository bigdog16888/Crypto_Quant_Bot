# Project Intelligence & Standards

## Agent Roles
### 1. The Architect (Structure & UI)
- **Focus**: Modular design, Streamlit UI/UX, File organization.
- **Standards**:
    - **Views**: All UI logic resides in `ui/views/`. `app.py` is for routing only.
    - **State**: Use `st.session_state` for persistence across reruns.
    - **Aesthetics**: Use "Rich Aesthetics" (Dark Mode, Cards, Visual Feedback).

### 2. The Quant (Logic & Math)
- **Focus**: Strategy implementation (`engine/strategies/`), Indicator calculation.
- **Standards**:
    - **Base Class**: All strategies MUST inherit from `BaseStrategy`.
    - **Vectorization**: Use vector operations (Pandas) over loops where possible.
    - **Validation**: Inputs must be type-checked. Math errors (div by zero) must be caught.

### 3. The Executor (Execution & API)
- **Focus**: `BotRunner` loop, Exchange API (`ccxt`), Order Management.
- **Standards**:
    - **Safety**: Always check `config.DRY_RUN` before executing.
    - **Error Handling**: Network calls must be wrapped in try/except with exponential backoff.
    - **Logging**: All trade actions must be logged to `engine.log`.
    - **Detachment**: Runner processes must be detached from the UI thread to prevent blocking.

## Coding Standards
1.  **Imports**: Absolute imports preferred (e.g., `from engine.database import ...`).
2.  **Database**: Use the context manager pattern or explicit `conn.close()`.
3.  **Config**: Hardcoded values belong in `config/settings.py` or the database, not code.
4.  **Formatting**: Markdown for documentation, standard Python PEP8.

## Project Structure
- `ui/`: Streamlit frontend.
- `engine/`: CORE logic (Strategies, Database, Runner).
- `config/`: Global settings.
- `tests/`: specific unit tests.
