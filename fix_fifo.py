path = r"C:\\Users\\Gionie\\Documents\\GitHub\\Crypto_Quant_Bot\\engine\\database.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

old_block = """        for b in buys:
            signed_qty = b['qty'] if bot_side == 'LONG' else -b['qty']
            if accum_sold > 0.0:
                if signed_qty <= accum_sold:
                    accum_sold = round(accum_sold - signed_qty, 8)
                else:
                    part_qty = round(abs(signed_qty) - accum_sold, 8)
                    signed_part_qty = part_qty if bot_side == 'LONG' else -part_qty
                    active_buys.append({
                        'step': b['step'],
                        'price': b['price'],
                        'qty': signed_part_qty
                    })
                    accum_sold = 0.0
            else:
                active_buys.append({
                    'step': b['step'],
                    'price': b['price'],
                    'qty': signed_qty
                })"""

new_block = """        for b in buys:
            signed_qty = b['qty'] if bot_side == 'LONG' else -b['qty']
            if bot_side == 'LONG':
                if signed_qty <= accum_sold:
                    accum_sold = round(accum_sold - signed_qty, 8)
                else:
                    part_qty = round(signed_qty - accum_sold, 8)
                    active_buys.append({
                        'step': b['step'],
                        'price': b['price'],
                        'qty': part_qty
                    })
                    accum_sold = 0.0
            else:  # SHORT side
                abs_entry = abs(signed_qty)
                if accum_sold > 0.0:
                    if abs_entry <= accum_sold:
                        accum_sold = round(accum_sold - abs_entry, 8)
                    else:
                        remaining = round(abs_entry - accum_sold, 8)
                        active_buys.append({
                            'step': b['step'],
                            'price': b['price'],
                            'qty': -remaining
                        })
                        accum_sold = 0.0
                else:
                    active_buys.append({
                        'step': b['step'],
                        'price': b['price'],
                        'qty': signed_qty
                    })"""

if old_block not in content:
    print("OLD BLOCK NOT FOUND - aborting, no changes made")
else:
    count = content.count(old_block)
    print(f"Found old block {count} time(s)")
    if count == 1:
        new_content = content.replace(old_block, new_block)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print("Replacement written successfully")
    else:
        print("Old block appears more than once - aborting to avoid ambiguous replacement")
