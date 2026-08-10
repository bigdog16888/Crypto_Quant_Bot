-- ============================================================
-- SURGICAL DIFF: Bot 100317 ONLY (10016 unchanged from Phase 2)
-- ============================================================
-- Updates trades row to exchange-verified net: -0.021 BTC SHORT
-- Marks phantom bot_orders rows as auto_closed, zeroes filled_amount
-- ============================================================

-- 1. Update trades table for bot 100317
UPDATE trades 
SET 
    cycle_id = 3,
    open_qty = -0.021,
    total_invested = 1361.30,      -- 0.071 * 64823.80 - 0.050 * 64914.60 ≈ 1361.30
    avg_entry_price = 64823.80,    -- weighted avg of real entry fill
    current_step = 2,              -- 1 real entry + 1 flatten
    entry_confirmed = 1,
    cycle_phase = 'ACTIVE',
    entry_order_id = '961145669',  -- only verified entry
    tp_order_id = NULL,
    wipe_wall_ts = 1785174411,     -- flatten timestamp
    cycle_start_time = 1785162672  -- first real entry timestamp
WHERE bot_id = 100317;

-- 2. Zero phantom bot_orders (no exchange match, regardless of status)
-- Phantom rows: 
--   id=325 (oid=914761721) - cycle 1 entry, reset_cleared
--   id=326 (oid=914762162) - cycle 1 entry, reset_cleared
--   id=872 (cid=CQB_100317_LIVE_GUARD_INV30_3_1) - cycle 3 entry, reset_cleared
--   id=1003 (cid=CQB_100317_LIVE_GUARD_RECON_3_2) - cycle 3 entry, status=filled (NO exchange match)
UPDATE bot_orders 
SET 
    filled_amount = 0, 
    status = 'auto_closed',
    updated_at = CAST(strftime('%s', 'now') AS INTEGER)
WHERE bot_id = 100317 
  AND filled_amount > 0
  AND (
    -- Reset_cleared entries with no exchange match
    (status = 'reset_cleared' AND order_id NOT IN ('961145669', '962590689'))
    OR
    -- 'filled' status entries with no exchange match (phantom LIVE_GUARD_RECON)
    (client_order_id = 'CQB_100317_LIVE_GUARD_RECON_3_2')
  );

-- 3. Verify result
SELECT 
    'trades' as table_name,
    bot_id, cycle_id, open_qty, total_invested, avg_entry_price, 
    current_step, entry_confirmed, cycle_phase
FROM trades WHERE bot_id = 100317
UNION ALL
SELECT 
    'bot_orders' as table_name,
    bot_id, cycle_id, 
    CAST(SUM(filled_amount) AS REAL), 
    0, 0, 0, 0, 
    GROUP_CONCAT(DISTINCT status)
FROM bot_orders WHERE bot_id = 100317 AND filled_amount > 0
GROUP BY bot_id, cycle_id;