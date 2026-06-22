# v3.6.2 Fix Plan — Direction-Aware TP Capacity Clip

## Two bugs, one file (bot_executor.py)

---

## Bug 1 — `_prepare_tp_order_params` clip is direction-blind (line ~384)

### What it does wrong
`_get_phys_pos` returns the net exchange position regardless of side.
In a net-LONG pair (+3403 SUI), a SHORT bot's BUY TP fetches size=3403,
computes capacity=3403, sees tp_qty=10.9 < 3403 → no clip → TP sent
with reduceOnly=True → exchange rejects (-4118) because buying on a
net-LONG position increases exposure, not reduces it.

### The fix
The clip must compare the physical position SIDE against what the bot
is trying to close. A SHORT bot's BUY TP requires SHORT-side capacity.
If the physical net is LONG, SHORT-side capacity is zero.

**Find exactly:**
```python
        # v3.1.6: Clip tp_qty to live physical capacity to prevent -4118.
        # In multi-bot pairs, physical net < each bot's virtual qty.
        # A full-size reduceOnly TP exceeds physical capacity and is rejected.
        try:
            _live_pos = self._get_phys_pos(pair, direction=direction)
            if _live_pos and _live_pos['size'] > 0:
```

**Replace with exactly:**
```python
        # v3.6.2: Direction-aware TP capacity clip to prevent -4118.
        # A LONG bot's SELL TP requires LONG-side physical capacity.
        # A SHORT bot's BUY TP requires SHORT-side physical capacity.
        # If the physical net is on the OPPOSITE side, capacity = 0
        # and the order must use GTX (non-reduceOnly) instead.
        try:
            _live_pos = self._get_phys_pos(pair, direction=direction)
            # Determine what side the physical position must be on
            # for this bot's TP to be a genuine reduction.
            _required_phys_side = 'LONG' if direction.upper() == 'LONG' else 'SHORT'
            _actual_phys_side = str(_live_pos.get('side', '')).upper() if _live_pos else ''
            if _live_pos and _live_pos['size'] > 0 and _actual_phys_side == _required_phys_side:
```

That single condition change (`and _actual_phys_side == _required_phys_side`) is the
entire fix. Everything inside the block (the capacity calculation, the clip) stays
exactly as-is. Only the outer `if` condition changes.

**What this does:**
- LONG bot SELL TP, physical=LONG: `_required=LONG, _actual=LONG` → True → clip runs normally
- SHORT bot BUY TP, physical=LONG: `_required=SHORT, _actual=LONG` → False → clip skips
- SHORT bot BUY TP, physical=SHORT: `_required=SHORT, _actual=SHORT` → True → clip runs normally
- LONG bot SELL TP, physical=SHORT: `_required=LONG, _actual=SHORT` → False → clip skips

When clip skips (wrong side), tp_qty stays at its computed value.
`_is_order_net_reducing` then returns False (correctly, since buying on LONG increases net).
The order falls through to GTX (non-reduceOnly maker order).
GTX on 10.9 SUI × $0.91 = ~$10 notional. This is above $5 mainnet min
but check if it clears testnet $100 min. If below testnet min, DUST_CHASER fires —
which is correct, the position is too small to close independently and will resolve
when the opposing LONG bot's TP fires and physical net shifts.

---

## Bug 2 — Sole-bot override fires on stale sibling check (line ~220)

### What it does wrong
`_is_order_net_reducing` counts siblings via `total_invested > 0`.
If the opposing LONG bot just hit its TP (total_invested → 0) but
the physical position hasn't been updated yet, sibling_count = 0.
The sole-bot override fires: SHORT bot BUY → `not bot_is_long and
not order_is_sell` = True → returns True → reduceOnly=True →
-4118 on a still-LONG physical net.

### The fix
The sole-bot override must also verify the physical net direction
matches what the override assumes. If override says "I'm reducing"
but physical says the net would increase, the override is wrong.

**Find exactly:**
```python
        # Sole-bot path: bot's virtual exit IS a physical reduction
        if sibling_count == 0 and bot_id and bot_direction:
            bot_is_long = bot_direction.upper() == 'LONG'
            order_is_sell = side.lower() == 'sell'
            if (bot_is_long and order_is_sell) or (not bot_is_long and not order_is_sell):
                return True
```

**Replace with exactly:**
```python
        # Sole-bot path: bot's virtual exit IS a physical reduction —
        # but ONLY if the physical net confirms this bot owns the position.
        # v3.6.2: Guard against stale sibling count (sibling just reset,
        # physical net hasn't updated yet). Verify physical side matches.
        if sibling_count == 0 and bot_id and bot_direction:
            bot_is_long = bot_direction.upper() == 'LONG'
            order_is_sell = side.lower() == 'sell'
            is_closing = (bot_is_long and order_is_sell) or (not bot_is_long and not order_is_sell)
            if is_closing:
                # Verify physical net direction matches before using reduceOnly
                try:
                    from engine.exchange_interface import normalize_symbol
                    from engine.database import get_connection as _gc_sv
                    with _gc_sv() as _c_sv:
                        _sv_rows = _c_sv.execute(
                            "SELECT side, size FROM active_positions WHERE pair=?",
                            (normalize_symbol(pair),)
                        ).fetchall()
                    _sv_net = sum(r[1] if str(r[0]).upper()=='LONG' else -r[1] for r in _sv_rows)
                    # Physical net must be on the same side as the bot's position
                    _phys_matches = (_sv_net > 0.0001 and bot_is_long) or \
                                    (_sv_net < -0.0001 and not bot_is_long)
                    if _phys_matches:
                        return True
                    else:
                        logger.warning(
                            f"[NET-REDUCE] Sole-bot override suppressed: "
                            f"bot={bot_direction} but phys_net={_sv_net:.6f} "
                            f"(opposite side). Using account-net path."
                        )
                except Exception:
                    pass  # Fall through to account-net path below
```

---

## CODEBASE_GUIDE addition — invariant 3.21

Add to Section 3 after 3.20:

```
### 3.21. TP Capacity is Direction-Aware (v3.6.2)

The TP capacity clip in _prepare_tp_order_params compares the
physical position SIDE against the bot's closing direction:

- LONG bot SELL TP: requires LONG-side physical capacity
- SHORT bot BUY TP: requires SHORT-side physical capacity

If the physical net is on the opposite side, capacity = 0 and
the order falls through to GTX (non-reduceOnly maker order).

The sole-bot override in _is_order_net_reducing also verifies
physical net direction before returning True. A stale sibling
count (sibling just reset but physical not yet updated) will not
cause a false reduceOnly=True on the wrong-side physical net.

This is the permanent fix for MARGIN HELD on SHORT bots in
net-LONG pairs (and vice versa).
```

---

## Version bump

CODEBASE_GUIDE header → v3.6.2
CHANGELOG entry:
```
### v3.6.2 — YYYY-MM-DD
engine/bot_executor.py:
- Fix 1: _prepare_tp_order_params clip now checks physical position
  SIDE against bot's closing direction. SHORT bot BUY TP on net-LONG
  pair correctly gets capacity=0 and falls to GTX instead of firing
  reduceOnly into a -4118 rejection.
- Fix 2: _is_order_net_reducing sole-bot override now verifies
  physical net direction before returning True. Prevents stale
  sibling count (sibling just reset) from triggering false
  reduceOnly on wrong-side physical net.
CODEBASE_GUIDE: Added invariant 3.21 (TP Capacity Direction-Aware).
```

---

## Verification checklist

- [ ] In `_prepare_tp_order_params` clip outer `if`: confirm
      `and _actual_phys_side == _required_phys_side` is present
- [ ] In `_is_order_net_reducing` sole-bot path: confirm physical
      net verification exists before `return True`
- [ ] After restart: `short sui` logs `[TP-CLIP]` skip or GTX fallback
- [ ] After restart: no -4118 for `short sui` TP placement
- [ ] After restart: `[NET-REDUCE] Sole-bot override suppressed` log
      does NOT appear (means the stale-sibling race isn't firing)
