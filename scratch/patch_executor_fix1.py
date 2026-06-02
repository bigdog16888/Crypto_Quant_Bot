import os

path = r'engine/bot_executor.py'
if not os.path.exists(path):
    print("Error: bot_executor.py not found at", path)
    exit(1)

with open(path, 'r', encoding='utf-8') as f:
    code = f.read()

# Target for Fix 1
target = """        force_no_reduce = False
        try:
            _live_pos = self._get_phys_pos(pair, direction=direction)
            # Determine what side the physical position must be on
            # for this bot's TP to be a genuine reduction.
            _required_phys_side = 'LONG' if direction.upper() == 'LONG' else 'SHORT'
            _actual_phys_side = str(_live_pos.get('side', '')).upper() if _live_pos else ''

            if _live_pos and _live_pos['size'] > 0 and _actual_phys_side == _required_phys_side:
                from engine.database import get_connection as _gc_clip
                with _gc_clip() as _cc:
                    _open_orders = _cc.cursor().execute(
                        "SELECT bo.bot_id, bo.amount, bo.order_type, b.direction "
                        "FROM bot_orders bo "
                        "JOIN bots b ON bo.bot_id = b.id "
                        "WHERE bo.status IN ('open', 'new') AND (b.pair = ? OR b.normalized_pair = ?)",
                        (pair, norm_pair)
                    ).fetchall()

                _other_open_tp = 0.0
                for _o_bot_id, _o_amount, _o_type, _o_dir in _open_orders:
                    if _o_bot_id == bot_id and _o_type in ('tp', 'dust_close'):
                        continue
                    _is_long_bot = (_o_dir.upper() == 'LONG')
                    _is_entry = _o_type in ('entry', 'grid', 'adoption_add', 'adoption', 'carry')
                    _o_side = 'buy' if (_is_long_bot and _is_entry) or (not _is_long_bot and not _is_entry) else 'sell'
                    if _o_side == side.lower():
                        _other_open_tp += float(_o_amount)

                _phys_capacity = exchange.round_to_step(
                    max(0.0, _live_pos['size'] - _other_open_tp),
                    prec['step_size']
                )
                if _phys_capacity > 0:
                    if tp_qty > _phys_capacity:
                        logger.warning(
                            f"[TP-CLIP] {name}: tp_qty {tp_qty:.6f} > physical capacity "
                            f"{_phys_capacity:.6f} (phys={_live_pos['size']:.6f}, "
                            f"other_tp={_other_open_tp:.6f}). Clipping to prevent -4118."
                        )
                        tp_qty = _phys_capacity
                else:
                    force_no_reduce = True
            else:
                force_no_reduce = True
        except Exception as _clip_err:
            logger.debug(f"[TP-CLIP] {name}: capacity clip check failed (non-blocking): {_clip_err}")"""

replacement = """        force_no_reduce = False
        try:
            # v3.6.4: Use actual exchange net position, not per-bot virtual record.
            # active_positions stores gross per-bot splits; exchange enforces the net.
            # Capacity must be computed from net to match what the exchange enforces.
            try:
                from engine.database import get_connection as _gc_net
                with _gc_net() as _cn:
                    _net_rows = _cn.execute(
                        "SELECT side, size FROM active_positions "
                        "WHERE pair = ? OR pair = ?",
                        (pair, norm_pair)
                    ).fetchall()
                _exchange_net = sum(
                    r[1] if str(r[0]).upper() == 'LONG' else -r[1]
                    for r in _net_rows
                )
            except Exception:
                _exchange_net = 0.0

            # For a LONG bot SELL TP: need positive net (LONG capacity)
            # For a SHORT bot BUY TP: need negative net (SHORT capacity)
            _required_sign = 1 if direction.upper() == 'LONG' else -1
            _net_on_correct_side = _exchange_net * _required_sign  # positive if correct side

            if _net_on_correct_side <= 0.0001:
                # Physical net is on wrong side or flat — zero capacity, use GTX
                logger.warning(
                    f"[TP-CLIP] {name}: Exchange net {_exchange_net:.6f} has no "
                    f"{direction} capacity. Setting force_no_reduce=True."
                )
                force_no_reduce = True
            else:
                # Subtract same-side open orders from the real net
                from engine.database import get_connection as _gc_clip
                with _gc_clip() as _cc:
                    _open_orders = _cc.cursor().execute(
                        "SELECT bo.bot_id, bo.amount, bo.order_type, b.direction "
                        "FROM bot_orders bo "
                        "JOIN bots b ON bo.bot_id = b.id "
                        "WHERE bo.status IN ('open', 'new') AND (b.pair = ? OR b.normalized_pair = ?)",
                        (pair, norm_pair)
                    ).fetchall()

                _other_open_tp = 0.0
                for _o_bot_id, _o_amount, _o_type, _o_dir in _open_orders:
                    if _o_bot_id == bot_id and _o_type in ('tp', 'dust_close'):
                        continue
                    _is_long_bot = (_o_dir.upper() == 'LONG')
                    _is_entry = _o_type in ('entry', 'grid', 'adoption_add', 'adoption', 'carry')
                    _o_side = 'buy' if (_is_long_bot and _is_entry) or (not _is_long_bot and not _is_entry) else 'sell'
                    if _o_side == side.lower():
                        _other_open_tp += float(_o_amount)

                _phys_capacity = exchange.round_to_step(
                    max(0.0, _net_on_correct_side - _other_open_tp),
                    prec['step_size']
                )
                if _phys_capacity <= 0.0001:
                    logger.warning(
                        f"[TP-CLIP] {name}: Net capacity exhausted by sibling orders "
                        f"(net={_net_on_correct_side:.6f}, other_open={_other_open_tp:.6f}). "
                        f"Setting force_no_reduce=True."
                    )
                    force_no_reduce = True
                elif tp_qty > _phys_capacity:
                    logger.warning(
                        f"[TP-CLIP] {name}: tp_qty {tp_qty:.6f} > net capacity "
                        f"{_phys_capacity:.6f}. Clipping."
                    )
                    tp_qty = _phys_capacity
        except Exception as _clip_err:
            logger.debug(f"[TP-CLIP] {name}: capacity clip check failed (non-blocking): {_clip_err}")"""

# Handle CRLF newlines correctly by normalizing to LF for replace, then keeping CRLF if needed
# Python read converts to LF, write converts back based on os line separator, so it is safe.
if target in code:
    code = code.replace(target, replacement)
    print("Fix 1 target replaced successfully (LF).")
else:
    target_crlf = target.replace("\n", "\r\n")
    replacement_crlf = replacement.replace("\n", "\r\n")
    if target_crlf in code:
        code = code.replace(target_crlf, replacement_crlf)
        print("Fix 1 target replaced successfully (CRLF).")
    else:
        print("Fix 1 target NOT found in file.")

with open(path, 'w', encoding='utf-8') as f:
    f.write(code)

print("Patching complete.")
