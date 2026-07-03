# ADR-004: Position Lifecycle Integrity

## Status
Implemented (v4.1.3 / v4.1.4)

## Context & Problem Statement
To guarantee position lifecycle integrity, the bot executor, ledger, and database must enforce strict state machine rules. The current implementation has three key vulnerabilities that can lead to orphan positions, drift, and incorrect resets.

---

## Proposed Changes

### Item 1 — TP reset must verify all grid positions are closed before wiping cycle

* **Target Function**: `handle_tp_completion` in [engine/ledger.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/ledger.py#L1182) and `_reset_bot_after_tp_internal` in [engine/database.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/database.py#L1245)
* **Status Machine Fix**: Add `PARTIAL_CLOSE_PENDING` cycle phase.
* **Logic**: Before executing the cycle wipe, we must verify that the bot's virtual `open_qty` after crediting the TP fill equals zero. If `open_qty > 0.0001`, it means grid orders filled after the TP was placed. In this case, we do NOT reset the cycle. Instead, we transition `cycle_phase` to `PARTIAL_CLOSE_PENDING`, place a market close order for the remaining quantity, and wait for it to fill. When filled, `maintain_orders` will trigger the final reset.

#### Current Incorrect Code

* **In [engine/ledger.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/ledger.py#L1182)**:
```python
        # --- Step 3: Full atomic reset via existing reset_bot_after_tp ---
        # This handles: mark reset_cleared, increment cycle_id, zero trades row
        # Pass the exchange fill timestamp so cycle_start_time is anchored to
        # the actual TP execution moment, not the engine processing time.
        try:
            reset_bot_after_tp(
                bot_id=bot_id,
                exit_price=exit_price,
                action_label='TP_HIT',
                notes=f'Cascade via ledger.handle_tp_completion @ {exit_price:.6f}',
                exit_fill_ts=exit_fill_ts,
                exchange=exchange,
            )
            logger.info(f"[TP-CASCADE] ✅ Bot {bot_id}: Reset to Scanning. Cycle {cycle_id} → {cycle_id + 1} (cst={exit_fill_ts}).")
            return True
```

* **In [engine/database.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/database.py#L1245)**:
```python
    cursor.execute("UPDATE trades SET cycle_id = ? WHERE bot_id = ?", (new_cycle, bot_id))
    cursor.execute(
        "UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, "
        "target_tp_price = 0, last_exit_price = ?, last_exit_time = ?, basket_start_time = 0, "
        "entry_confirmed = 0, entry_order_id = NULL, tp_order_id = NULL, "
        "bot_position_id = NULL, close_type = ?, cycle_id = ?, cycle_phase = ?, "
        "open_qty = 0, wipe_wall_ts = ?, cycle_start_time = ? WHERE bot_id = ?",
        (exit_price, now_ts, action_label, new_cycle, new_cycle_phase,
         now_ts, new_cycle_start_time, bot_id)
    )
```

#### Proposed Correct Code

* **In [engine/ledger.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/ledger.py#L1182)**:
```python
        # --- Step 3: Full atomic reset via existing reset_bot_after_tp ---
        # This handles: mark reset_cleared, increment cycle_id, zero trades row
        # Pass the exchange fill timestamp so cycle_start_time is anchored to
        # the actual TP execution moment, not the engine processing time.
        try:
            # Seal trade state first to ensure trades.open_qty matches the ledger truth after the TP fill.
            from engine.ledger import seal_trade_state
            seal_trade_state(bot_id)

            _row_qty = conn.execute(
                "SELECT open_qty, direction, cycle_id FROM trades t JOIN bots b ON b.id = t.bot_id WHERE t.bot_id = ?",
                (bot_id,)
            ).fetchone()
            _open_qty = float(_row_qty[0] or 0) if _row_qty else 0.0
            _direction = str(_row_qty[1] or 'LONG').upper() if _row_qty else 'LONG'
            _cycle_id = int(_row_qty[2] or 1) if _row_qty else 1

            if _open_qty > 0.0001:
                logger.warning(
                    f"[TP-CASCADE] 🛑 Bot {bot_id}: open_qty={_open_qty:.6f} > 0 after TP fill. "
                    f"Grid orders filled after TP placed. Transitioning to PARTIAL_CLOSE_PENDING."
                )
                conn.execute(
                    "UPDATE trades SET cycle_phase = 'PARTIAL_CLOSE_PENDING' WHERE bot_id = ?",
                    (bot_id,)
                )
                conn.commit()

                # Place a reduce-only close order for the remaining open_qty
                close_side = 'sell' if _direction == 'LONG' else 'buy'
                close_cid = f"CQB_{bot_id}_CLOSE_{_cycle_id}_{int(time.time())}"
                from engine.database import save_bot_order
                save_bot_order(
                    bot_id, 'close', close_cid,
                    price=0.0, amount=_open_qty, step=0,
                    status='pending_placement',
                    client_order_id=close_cid,
                    notes=f"PARTIAL_CLOSE_PENDING: Close remaining position of {_open_qty:.6f}",
                    cycle_id=_cycle_id
                )
                try:
                    _testnet = bool(getattr(exchange, 'is_testnet', False) or
                                    getattr(getattr(exchange, 'exchange', None), 'sandbox', False))
                    _params = {
                        'reduceOnly': True,
                        'newClientOrderId': close_cid,
                    }
                    from engine.bot_executor import BotExecutor
                    _params = BotExecutor._resolve_position_side_param(_params, _testnet)
                    close_order = exchange.create_order(
                        normalize_symbol(pair), 'market', close_side, _open_qty,
                        params=_params
                    )
                    if close_order:
                        conn.execute(
                            "UPDATE bot_orders SET order_id = ?, status = ?, updated_at = ? WHERE client_order_id = ?",
                            (close_order['id'], close_order.get('status', 'open'), int(time.time()), close_cid)
                        )
                        conn.commit()
                        logger.info(f"[TP-CASCADE] Placed close order {close_order['id']} for remaining {_open_qty:.6f}")
                except Exception as e_close:
                    logger.error(f"[TP-CASCADE] Failed to place close order: {e_close}")
                return False

            reset_bot_after_tp(
                bot_id=bot_id,
                exit_price=exit_price,
                action_label='TP_HIT',
                notes=f'Cascade via ledger.handle_tp_completion @ {exit_price:.6f}',
                exit_fill_ts=exit_fill_ts,
                exchange=exchange,
            )
            logger.info(f"[TP-CASCADE] ✅ Bot {bot_id}: Reset to Scanning. Cycle {cycle_id} → {cycle_id + 1} (cst={exit_fill_ts}).")
            return True
```

* **In [engine/database.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/database.py#L1245)** (at start of `_reset_bot_after_tp_internal` before cycle wipe):
```python
    # Ensure no active/unclosed positions exist when trying to reset via TP_HIT
    if action_label == 'TP_HIT' and old_net_qty > 0.0001:
        raise ValueError(
            f"Cannot reset bot {bot_id} after TP: remaining position is not zero (net_qty={old_net_qty:.6f})."
        )
```

* **In [engine/bot_executor.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/bot_executor.py#L3098)** (inside `maintain_orders`, before standard bot grid checks):
```python
        # CASE 1b: PARTIAL_CLOSE_PENDING GUARD [v3.9.23]
        if bot_status.get('cycle_phase') == 'PARTIAL_CLOSE_PENDING':
            from engine.ledger import seal_trade_state as _sts_partial
            _sts_partial(bot_id)
            from engine.database import get_bot_status as _gbs_partial
            bot_status = _gbs_partial(bot_id)
            current_open_qty = float(bot_status.get('open_qty', 0) or 0)
            
            if current_open_qty <= 0.0001:
                logger.info(f"🎉 [PARTIAL_CLOSE_PENDING] Bot {name} ({bot_id}) remaining position closed. Resetting bot.")
                from engine.database import reset_bot_after_tp as _rb_partial
                _rb_partial(
                    bot_id=bot_id,
                    exit_price=current_price,
                    action_label='TP_HIT',
                    notes=f'Partial close settled. Resetting bot.',
                    exchange=exchange
                )
            else:
                logger.info(f"⏳ [PARTIAL_CLOSE_PENDING] Bot {name} ({bot_id}) still has open_qty={current_open_qty:.6f}. Awaiting settlement.")
                # If the close order was cancelled or failed, re-place it
                close_orders = [o for o in bot_open_orders if '_CLOSE_' in o.get('clientOrderId', '')]
                if not close_orders:
                    logger.warning(f"⚠️ [PARTIAL_CLOSE_PENDING] Bot {name} ({bot_id}) has open_qty={current_open_qty:.6f} but no active close order. Re-placing.")
                    close_side = 'sell' if direction == 'LONG' else 'buy'
                    close_cid = f"CQB_{bot_id}_CLOSE_{bot_status.get('cycle_id', 1)}_{int(time.time())}"
                    from engine.database import save_bot_order as _sbo_partial
                    _sbo_partial(
                        bot_id, 'close', close_cid,
                        price=0.0, amount=current_open_qty, step=0,
                        status='pending_placement',
                        client_order_id=close_cid,
                        notes=f"PARTIAL_CLOSE_PENDING fallback: Close remaining position of {current_open_qty:.6f}",
                        cycle_id=bot_status.get('cycle_id', 1)
                    )
                    try:
                        _testnet = bool(getattr(exchange, 'is_testnet', False) or
                                        getattr(getattr(exchange, 'exchange', None), 'sandbox', False))
                        _params = {
                            'reduceOnly': True,
                            'newClientOrderId': close_cid,
                        }
                        _params = self._resolve_position_side_param(_params, _testnet)
                        close_order = exchange.create_order(
                            pair, 'market', close_side, current_open_qty,
                            params=_params
                        )
                        if close_order:
                            from engine.database import update_order_status as _uos_partial
                            _uos_partial(close_order['id'], close_order.get('status', 'open'), bot_id=bot_id, filled_qty=0.0)
                    except Exception as e_close:
                        logger.error(f"[PARTIAL_CLOSE_PENDING] Fallback place close failed: {e_close}")
            return None
```

---

### Item 2 — TP replacement must use remaining qty not original qty

* **Target Function**: `_sync_replace_tp` in [engine/bot_executor.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/bot_executor.py#L1431)
* **Logic**: When a TP is cancelled with a partial fill and a replacement is placed, the replacement qty must read the actual virtual `trades.open_qty` at replacement time instead of using the cancelled order's original target amount. The correct qty is updated atomically in `trades.open_qty` by the WS fill processor when `credit_fill` runs.

#### Current Incorrect Code
```python
            # 🚀 ROOT CAUSE FIX: Re-read open_qty from DB after sleep!
            # The ledger (trades.open_qty) is the absolute ground truth.
            # If the cancelled order was partially filled just before cancellation,
            # the WebSocket will have updated open_qty during the 500ms sleep.
            try:
                from engine.database import get_connection
                _conn = get_connection()
                _latest_qty_row = _conn.execute("SELECT open_qty FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
                if _latest_qty_row:
                    _latest_qty = float(_latest_qty_row[0] or 0)
                    if abs(_latest_qty - db_qty) > 1e-8:
                        logger.info(f"🔄 [TP-SYNC] {name}: Ledger changed from {db_qty:.4f} to {_latest_qty:.4f} (WS processed a fill). Syncing to absolute ledger truth.")
                        db_qty = _latest_qty
            except Exception as e:
                logger.error(f"[TP-SYNC] Failed to re-verify open_qty: {e}")
```

#### Proposed Correct Code
```python
            # 🚀 ROOT CAUSE FIX: Re-read open_qty from DB after sleep!
            # The ledger (trades.open_qty) is the absolute ground truth.
            # If the cancelled order was partially filled just before cancellation,
            # the WebSocket / credit_fill will have updated trades.open_qty.
            # We unconditionally read trades.open_qty to ensure the replacement
            # uses the remaining qty at replacement time.
            try:
                from engine.database import get_connection
                _conn = get_connection()
                _latest_qty_row = _conn.execute("SELECT open_qty FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
                if _latest_qty_row:
                    _latest_qty = float(_latest_qty_row[0] or 0)
                    logger.info(f"🔄 [TP-SYNC] {name}: Setting replacement TP qty to current trades.open_qty: {_latest_qty:.6f} (cancelled order original target was {db_qty:.6f})")
                    db_qty = _latest_qty
            except Exception as e:
                logger.error(f"[TP-SYNC] Failed to re-verify open_qty from trades: {e}")
```

---

### Item 3 — hedge child TP qty must use virtual open_qty not exchange physical

* **Target Functions**: `seal_trade_state` in [engine/ledger.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/ledger.py#L545) and `maintain_orders` in [engine/bot_executor.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/bot_executor.py#L2867)
* **Logic**: For hedge child bots, the TP quantity must strictly be `trades.open_qty` for that bot rather than pulling from the exchange net position (which combines parent and child positions). A hedge child owns only its virtual position, so closing the full exchange physical would close another bot's position too.

#### Current Incorrect Code

* **In [engine/ledger.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/ledger.py#L545)**:
```python
            is_hedge_child = False
            _bot_type_row = conn.execute("SELECT bot_type, pair, direction, config FROM bots WHERE id = ?", (bot_id,)).fetchone()
            if _bot_type_row and _bot_type_row[0] == 'hedge_child':
                is_hedge_child = True
                _pair = _bot_type_row[1]
                _dir = _bot_type_row[2]
                _cfg_str = _bot_type_row[3]
```
*(This triggers physical position check later in the function, overwriting `qty` based on physical net)*

* **In [engine/bot_executor.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/bot_executor.py#L2867)**:
```python
                    # Invariant B: Exchange-Authoritative TP Qty
                    _phys = None
                    try:
                        positions = exchange.fetch_positions()
                        if positions:
                            from engine.exchange_interface import normalize_symbol
                            target_symbol_norm = normalize_symbol(pair)
                            expected_side = 'long' if child_direction.upper() == 'LONG' else 'short'
                            for p in positions:
                                if normalize_symbol(p['symbol']) == target_symbol_norm and p['side'] == expected_side:
                                    _phys = {'size': float(p['qty'])}
                                    break
                    except Exception as e_phys:
                        logger.error(f"[HEDGE-MAINTAIN] Failed to fetch positions from exchange for TP sizing: {e_phys}")

                    # Fallback to DB cache if exchange call fails
                    if _phys is None:
                        logger.warning(f"[HEDGE-MAINTAIN] Falling back to DB cache for TP sizing on {pair}")
                        _phys = self._get_phys_pos(pair, direction=child_direction)

                    if not _phys or _phys['size'] < 0.0001:
                        # ...
                    else:
                        tp_amount = _phys['size']
```

#### Proposed Correct Code

* **In [engine/ledger.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/ledger.py#L545)**:
```python
            is_hedge_child = False
            _bot_type_row = conn.execute("SELECT bot_type, pair, direction, config FROM bots WHERE id = ?", (bot_id,)).fetchone()
            if _bot_type_row and _bot_type_row[0] == 'hedge_child':
                is_hedge_child = True
                # Skip exchange drift check and get_exchange_signed_net calculation entirely
                # The hedge child owns only its virtual position.
            
            # ...
            if not is_hedge_child and positions is not None:
                # Only perform physical position drift check for standard bots
```

* **In [engine/bot_executor.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/bot_executor.py#L2867)**:
```python
                    # Invariant B: TP Qty must use trades.open_qty for hedge child
                    tp_amount = float(bot_status.get('open_qty', 0) or 0)
```
