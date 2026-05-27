#!/usr/bin/env python3
"""
ADR-002 One-Time Migration: converts existing hedge_qty state to hedge child bot rows.

Run ONCE after deploying Ticket-1 schema changes (i.e. after the engine has
started at least once with the updated database.py so that bot_type,
parent_bot_id, hedge_child_bot_id, and hedge_trigger_step columns exist).

Usage:
    python scripts/migrate_hedge_to_child_bot.py [--dry-run]

Idempotent: running a second time is a no-op.
"""

import sys
import os
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db
from engine.ledger import seal_trade_state
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s | %(message)s')
logger = logging.getLogger(__name__)


def migrate(dry_run: bool = False) -> None:
    # Do NOT call init_db() here — it runs DROP TABLE active_positions which would destroy
    # any existing orphan rows we need to reassign. The engine must have been started once
    # already (which calls init_db()) before this script is run.
    conn = get_connection()
    cursor = conn.cursor()

    # Verify ADR-002 columns exist before proceeding
    try:
        cursor.execute("SELECT bot_type, parent_bot_id, hedge_child_bot_id FROM bots LIMIT 1")
    except Exception as e:
        logger.error(
            f"ADR-002 schema columns missing: {e}\n"
            "Start the engine once with the updated database.py before running this script."
        )
        sys.exit(1)

    # Find all parent bots with outstanding hedge_qty
    rows = cursor.execute("""
        SELECT b.id, b.name, b.pair, b.normalized_pair, b.direction,
               b.config, COALESCE(t.hedge_qty, 0) as hedge_qty,
               COALESCE(t.cycle_id, 1) as cycle_id
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE t.hedge_qty > 0.0001
          AND b.bot_type = 'standard'
          AND b.is_active = 1
    """).fetchall()

    if not rows:
        logger.info("No bots with outstanding hedge_qty found. Nothing to migrate.")
        return

    migrated = 0
    for bot_id, name, pair, norm_pair, direction, config_json, hedge_qty, cycle_id in rows:
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Processing bot {bot_id} ({name}): hedge_qty={hedge_qty:.6f}")

        child_direction = 'SHORT' if str(direction).upper() == 'LONG' else 'LONG'
        child_name = f"{name}_hedge"

        # --- Idempotency guard ---
        existing_child = cursor.execute(
            "SELECT id FROM bots WHERE parent_bot_id = ? AND bot_type = 'hedge_child'",
            (bot_id,)
        ).fetchone()

        if existing_child:
            child_id = existing_child[0]
            logger.info(f"  ✅ Child bot already exists (id={child_id}). Checking state...")
        else:
            logger.info(f"  Creating hedge child bot: '{child_name}' ({child_direction})")
            if not dry_run:
                # Create child bot row (direction='SHORT' when parent is LONG)
                cursor.execute("""
                    INSERT INTO bots (
                        name, pair, normalized_pair, direction,
                        bot_type, parent_bot_id,
                        is_active, status,
                        config, rsi_limit, martingale_multiplier,
                        base_size, strategy_type
                    )
                    VALUES (?, ?, ?, ?, 'hedge_child', ?, 1, 'IN TRADE',
                            ?, 0, 1.0, 0, 'Martingale')
                """, (child_name, pair, norm_pair, child_direction,
                      bot_id, config_json))
                child_id = cursor.lastrowid
                logger.info(f"  ✅ Created child bot id={child_id}")

                # Create trades row for child
                cursor.execute("""
                    INSERT INTO trades (
                        bot_id, open_qty, hedge_qty, cycle_id,
                        position_side, total_invested, avg_entry_price,
                        current_step, entry_confirmed
                    )
                    VALUES (?, ?, 0, 1, ?, 0, 0, 1, 1)
                """, (child_id, hedge_qty, child_direction))

                # Audit entry in bot_orders — represents the inherited net position
                audit_cid = f"CQB_{child_id}_HEDGE_MIGRATE_{int(time.time())}"
                cursor.execute("""
                    INSERT INTO bot_orders (
                        bot_id, order_type, order_id, client_order_id,
                        price, amount, filled_amount, status,
                        step, cycle_id, created_at, notes, position_side
                    )
                    VALUES (?, 'entry', ?, ?, 0, ?, ?, 'filled', 1, 1, ?, ?, ?)
                """, (
                    child_id,
                    audit_cid, audit_cid,
                    hedge_qty, hedge_qty,
                    int(time.time()),
                    f"HEDGE_MIGRATION: inherited {hedge_qty:.6f} {child_direction} "
                    f"from parent bot {bot_id} ({name})",
                    child_direction,
                ))
            else:
                logger.info(f"  [DRY-RUN] Would create child bot '{child_name}' "
                            f"with open_qty={hedge_qty:.6f} {child_direction}")
                child_id = -1

        if not dry_run:
            # Link parent → child and set hedge_trigger_step from config (HedgeStartStep)
            import json
            hedge_trigger_step = None
            if config_json:
                try:
                    cfg = json.loads(config_json)
                    if 'HedgeStartStep' in cfg:
                        hedge_trigger_step = int(cfg['HedgeStartStep'])
                except Exception as ex:
                    logger.warning(f"  ⚠️ Failed to parse config_json for bot {bot_id}: {ex}")

            cursor.execute(
                "UPDATE bots SET hedge_child_bot_id = ?, hedge_trigger_step = ? WHERE id = ?",
                (child_id, hedge_trigger_step, bot_id)
            )
            # Zero hedge_qty on parent (INV-5)
            cursor.execute(
                "UPDATE trades SET hedge_qty = 0 WHERE bot_id = ?",
                (bot_id,)
            )

            # Reassign orphan active_positions row to child bot (INV-8)
            # Match any known pair format (normalized or raw) and case-insensitive side
            updated = cursor.execute("""
                UPDATE active_positions
                SET bot_id = ?
                WHERE UPPER(side) = UPPER(?)
                  AND bot_id = 0
                  AND (
                      pair = ?
                      OR pair = REPLACE(REPLACE(?, '/', ''), ':USDC', 'USDC')
                      OR pair = REPLACE(REPLACE(?, '/', ''), ':USDT', 'USDT')
                  )
            """, (child_id, child_direction,
                  norm_pair, norm_pair, norm_pair)).rowcount
            logger.info(f"  ✅ Reassigned {updated} active_positions row(s) to child bot {child_id}")

            conn.commit()
            logger.info(f"  ✅ Parent bot {bot_id} → child bot {child_id} linked. hedge_qty zeroed.")

            # Seal both bots to verify consistency
            seal_trade_state(bot_id)
            seal_trade_state(child_id)
            logger.info(f"  ✅ Sealed both bots.")

            # If the child has positions, register a pending break-even TP
            child_trade = cursor.execute(
                "SELECT open_qty, avg_entry_price, cycle_id, position_side FROM trades WHERE bot_id = ?",
                (child_id,)
            ).fetchone()
            if child_trade:
                child_qty, child_avg, child_cycle, child_side = child_trade
                child_qty = float(child_qty or 0)
                child_avg = float(child_avg or 0)
                if child_qty > 0.0001 and child_avg > 0:
                    be_cid = f"CQB_{child_id}_TP_{child_cycle}_BE"
                    # Check if already exists
                    existing_be = cursor.execute(
                        "SELECT id FROM bot_orders WHERE bot_id=? AND client_order_id=?",
                        (child_id, be_cid)
                    ).fetchone()
                    if not existing_be:
                        cursor.execute("""
                            INSERT INTO bot_orders (
                                bot_id, order_type, order_id, client_order_id,
                                price, amount, filled_amount, status,
                                step, cycle_id, created_at, notes, position_side
                            )
                            VALUES (?, 'tp', ?, ?, ?, ?, 0, 'pending_placement', 1, ?, ?, ?, ?)
                        """, (
                            child_id,
                            f"PENDING_BE_{child_id}_{child_cycle}",
                            be_cid,
                            child_avg,
                            child_qty,
                            child_cycle,
                            int(time.time()),
                            f"Break-even TP: migrated position",
                            child_side
                        ))
                        conn.commit()
                        logger.info(f"  ✅ Registered pending break-even TP for child {child_id}: {child_qty:.6f} @ {child_avg:.4f}")
            migrated += 1
        else:
            logger.info(f"  [DRY-RUN] Would link parent {bot_id} → child, "
                        f"zero hedge_qty, reassign active_positions.")

    action = "dry-run simulated" if dry_run else "migrated"
    logger.info(f"\n{'=' * 60}")
    logger.info(f"✅ Migration complete. {len(rows)} parent bot(s) {action}.")
    if not dry_run:
        logger.info("Next step: run full test suite, then deploy Ticket-3.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ADR-002 hedge child bot migration')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would happen without modifying the database')
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
