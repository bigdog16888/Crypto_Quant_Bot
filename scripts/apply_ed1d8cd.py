"""Apply ed1d8cd 7-closure fixes to ground_truth_reconciler.py"""
with open('engine/ground_truth_reconciler.py', 'r') as f:
    content = f.read()

# 1. _delete_active_positions - drop conn.close()
content = content.replace(
    '''                def _delete_active_positions():
                    from engine.database import get_connection
                    conn = get_connection()
                    conn.execute("DELETE FROM active_positions")
                    conn.commit()
                    conn.close()
            
                WriteQueue().put_and_wait(_delete_active_positions)''',
    '''                def _delete_active_positions():
                    from engine.database import get_connection
                    conn = get_connection()
                    conn.execute("DELETE FROM active_positions")
                    conn.commit()
                WriteQueue().put_and_wait(_delete_active_positions)'''
)

# 2. _insert_active_positions - drop conn.close()
content = content.replace(
    '''                                def _insert_active_positions(bot_id, pair, side, size, entry_price, ts):
                                    from engine.database import get_connection
                                    conn = get_connection()
                                    conn.execute("""
                                        INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                                        VALUES (?, ?, ?, ?, ?, ?
                                    """, (bot_id, pair, side, size, entry_price, ts))
                                    conn.commit()
                                    conn.close()
                            
                                WriteQueue().put_and_wait(
                                    _insert_active_positions, 
                                    share['id'], symbol, share['dir'], share['qty'], share['avg'], ts
                                )''',
    '''                                def _insert_active_positions(bot_id, pair, side, size, entry_price, ts):
                                    from engine.database import get_connection
                                    conn = get_connection()
                                    conn.execute("""
                                        INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                                        VALUES (?, ?, ?, ?, ?, ?
                                    """, (bot_id, pair, side, size, entry_price, ts))
                                    conn.commit()
                                WriteQueue().put_and_wait(
                                    _insert_active_positions, 
                                    share['id'], symbol, share['dir'], share['qty'], share['avg'], ts
                                )'''
)

# 3. _insert_active_positions_mismatch - drop conn.close()
content = content.replace(
    '''                        def _insert_active_positions_mismatch(owner_id, symbol, side, size, avg_price, ts):
                            from engine.database import get_connection
                            conn = get_connection()
                            conn.execute("""
                                INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                                VALUES (?, ?, ?, ?, ?, ?
                            """, (owner_id, symbol, side, size, avg_price, ts))
                            conn.commit()
                            conn.close()
                    
                        WriteQueue().put_and_wait(
                            _insert_active_positions_mismatch,
                            owner_id, symbol, side, data['size'], avg_price, ts
                        )''',
    '''                        def _insert_active_positions_mismatch(owner_id, symbol, side, size, avg_price, ts):
                            from engine.database import get_connection
                            conn = get_connection()
                            conn.execute("""
                                INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                                VALUES (?, ?, ?, ?, ?, ?
                            """, (owner_id, symbol, side, size, avg_price, ts))
                            conn.commit()
                        WriteQueue().put_and_wait(
                            _insert_active_positions_mismatch,
                            owner_id, symbol, side, data['size'], avg_price, ts
                        )'''
)

# 4. _insert_global_flat - drop conn.close()
content = content.replace(
    '''                if not agg_positions:
                    def _insert_global_flat(ts):
                        from engine.database import get_connection
                        conn = get_connection()
                        conn.execute("""
                            INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                            VALUES (?, ?, ?, ?, ?, ?
                        """, (0, 'GLOBAL', 'FLAT', 0.0, 0.0, ts))
                        conn.commit()
                        conn.close()
                
                    WriteQueue().put_and_wait(_insert_global_flat, ts)''',
    '''                if not agg_positions:
                    def _insert_global_flat(ts):
                        from engine.database import get_connection
                        conn = get_connection()
                        conn.execute("""
                            INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                            VALUES (?, ?, ?, ?, ?, ?
                        """, (0, 'GLOBAL', 'FLAT', 0.0, 0.0, ts))
                        conn.commit()
                    WriteQueue().put_and_wait(_insert_global_flat, ts)'''
)

# 5. _heal_ghost_virtual_internal - drop cursor param, open connection inside
content = content.replace(
    '''            def _heal_ghost_virtual_internal(c, bot_id, cycle_id):
                c.execute("""
                    UPDATE trades SET 
                        open_qty=0, total_invested=0, avg_entry_price=0,
                        current_step=0, entry_confirmed=0,
                        cycle_id = cycle_id + 1
                    WHERE bot_id=?
                """, (bot_id,))
                c.execute(
                    "UPDATE bots SET status='Scanning', cascade_started_at=0 WHERE id=?", (bot_id,)
                )
                c.execute("""
                    UPDATE bot_orders SET status='reset_cleared', updated_at=?
                    WHERE bot_id=? AND cycle_id=?
                    AND status NOT IN ('reset_cleared','auto_closed','filled','cancelled')
                """, (int(time.time()), bot_id, cycle_id))
        
        WriteQueue().put_and_wait(_heal_ghost_virtual_internal, bot_id, cycle_id)''',
    '''            def _heal_ghost_virtual_internal(bot_id, cycle_id):
                from engine.database import get_connection
                conn = get_connection()
                conn.execute("""
                    UPDATE trades SET 
                        open_qty=0, total_invested=0, avg_entry_price=0,
                        current_step=0, entry_confirmed=0,
                        cycle_id = cycle_id + 1
                    WHERE bot_id=?
                """, (bot_id,))
                conn.execute(
                    "UPDATE bots SET status='Scanning', cascade_started_at=0 WHERE id=?", (bot_id,)
                )
                conn.execute("""
                    UPDATE bot_orders SET status='reset_cleared', updated_at=?
                    WHERE bot_id=? AND cycle_id=?
                    AND status NOT IN ('reset_cleared','auto_closed','filled','cancelled')
                """, (int(time.time()), bot_id, cycle_id))
                conn.commit()
        WriteQueue().put_and_wait(_heal_ghost_virtual_internal, bot_id, cycle_id)'''
)

# 6. _reset_hedge_close - drop cursor param, open connection inside
content = content.replace(
    '''                def _reset_hedge_close(c, bot_id):
                    c.execute(
                        "UPDATE bots SET status='Scanning', cascade_started_at=0 WHERE id=?", (bot_id,)
                    )
                    c.execute("""
                        UPDATE trades SET cycle_id=cycle_id+1, current_step=0,
                        open_qty=0, total_invested=0, avg_entry_price=0,
                        entry_confirmed=0 WHERE bot_id=?
                    """, (bot_id,))
            
                WriteQueue().put_and_wait(_reset_hedge_close, bot_id)''',
    '''                def _reset_hedge_close(bot_id):
                    from engine.database import get_connection
                    conn = get_connection()
                    conn.execute(
                        "UPDATE bots SET status='Scanning', cascade_started_at=0 WHERE id=?", (bot_id,)
                    )
                    conn.execute("""
                        UPDATE trades SET cycle_id=cycle_id+1, current_step=0,
                        open_qty=0, total_invested=0, avg_entry_price=0,
                        entry_confirmed=0 WHERE bot_id=?
                    """, (bot_id,))
                    conn.commit()
                WriteQueue().put_and_wait(_reset_hedge_close, bot_id)'''
)

# 7. _set_pending_flatten - drop cursor param, open connection inside
content = content.replace(
    '''                def _set_pending_flatten(c, bot_id, ts):
                    c.execute(
                        "UPDATE bots SET status='pending_flatten', cascade_started_at=? WHERE id=?", (ts, bot_id)
                    )
            
                WriteQueue().put_and_wait(_set_pending_flatten, bot_id, int(time.time()))''',
    '''                def _set_pending_flatten(bot_id, ts):
                    from engine.database import get_connection
                    conn = get_connection()
                    conn.execute(
                        "UPDATE bots SET status='pending_flatten', cascade_started_at=? WHERE id=?", (ts, bot_id)
                    )
                    conn.commit()
                WriteQueue().put_and_wait(_set_pending_flatten, bot_id, int(time.time()))'''
)

with open('engine/ground_truth_reconciler.py', 'w') as f:
    f.write(content)

import py_compile
py_compile.compile('engine/ground_truth_reconciler.py', doraise=True)
print("Syntax OK")