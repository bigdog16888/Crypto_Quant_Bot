with open('C:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot_INV31/engine/bot_executor.py', 'r') as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    if i == 4916:  # Line 4917 (0-indexed) - the elif line
        new_lines.append(line)
    elif 4917 <= i <= 4925:  # Lines 4918-4926 (0-indexed)
        if i == 4917:
            new_lines.append('                            # Skip if already frozen with same marker to avoid re-freeze spam\n')
            new_lines.append('                            _existing = _conn_hcs.execute(\n')
            new_lines.append('                                "SELECT status, last_error FROM bots WHERE id = ?", (bot_id,)\n')
            new_lines.append('                            ).fetchone()\n')
            new_lines.append('                            _already_frozen = (\n')
            new_lines.append('                                _existing and _existing[0] == "REQUIRE_MANUAL_PROOF"\n')
            new_lines.append('                                and _existing[1] and _existing[1].startswith("HEDGE_ENGAGE_FAILURE:")\n')
            new_lines.append('                            )\n')
            new_lines.append('                            if not _already_frozen:\n')
            new_lines.append('                                import time as _wd_time\n')
            new_lines.append('                                _conn_hcs.execute(\n')
            new_lines.append('                                    "UPDATE bots SET status=\'REQUIRE_MANUAL_PROOF\', last_error=?, last_error_time=? WHERE id=?",\n')
            new_lines.append('                                    ("HEDGE_ENGAGE_FAILURE:" + _wd.get(\'reason\', \'\'), int(_wd_time.time()), bot_id)\n')
            new_lines.append('                                )\n')
            new_lines.append('                                _conn_hcs.commit()\n')
            new_lines.append('                                logger.critical(f"\\U0001f6e1\\ufe0f O-10 HEDGE-FREEZE: {name} hedge failed to engage - bot locked to REQUIRE_MANUAL_PROOF.")\n')
            new_lines.append('                            else:\n')
            new_lines.append('                                logger.debug(f"O-10: {name} already frozen with HEDGE_ENGAGE_FAILURE, skipping re-freeze.")\n')
        # skip the original lines
    else:
        new_lines.append(line)

with open('C:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot_INV31/engine/bot_executor.py', 'w') as f:
    f.writelines(new_lines)

print('Done')