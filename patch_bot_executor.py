
import os

file_path = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\engine\bot_executor.py"

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# The target block to replace (copied from recent view_file)
# Note: Indentation must match exactly what's in the file (14 spaces for 'try:')
target_block = """                      try:
                          final_order = ex.wait_for_fill(order, timeout_seconds=current_timeout)
                          if final_order and final_order.get('status') in ['closed', 'filled']:
                              fill_price = float(final_order.get('average', 0.0) or final_order.get('price', 0.0))
                              success = True
                              break
                      except Exception:
                          # Timeout -> Cancel and Retry
                          logger.info(f"Chase timeout ({current_timeout}s). Repricing...")
                          try:
                              ex.exchange.cancel_order(order_id, pair)
                          except Exception as cancel_err:
                              # Could be already filled or canceled
                              logger.warning(f"Cancel failed: {cancel_err}")
                              # Check status one last time
                              final_check = ex.fetch_order(order_id, pair)
                              if final_check and final_check.get('status') == 'filled':
                                  fill_price = float(final_check.get('average', 0.0))
                                  success = True
                                  break"""

# The replacement block (Robust logic)
replacement_block = """                      try:
                          final_order = ex.wait_for_fill(order, timeout_seconds=current_timeout)
                          status = final_order.get('status') if final_order else 'unknown'
                          
                          if status in ['closed', 'filled']:
                              fill_price = float(final_order.get('average', 0.0) or final_order.get('price', 0.0))
                              success = True
                              break
                          else:
                              # Timeout or Open -> CANCEL
                              logger.info(f"Chase timeout ({current_timeout}s). Status: {status}. Cancelling...")
                              try:
                                  ex.exchange.cancel_order(order_id, pair)
                              except Exception as cancel_err:
                                  logger.warning(f"Cancel failed: {cancel_err}")
                              
                              # Final status check
                              try:
                                  final_check = ex.fetch_order(order_id, pair)
                                  if final_check and final_check.get('status') == 'filled':
                                      fill_price = float(final_check.get('average', 0.0))
                                      success = True
                                      break
                              except:
                                  pass
                                  
                      except Exception as e:
                          logger.error(f"Error waiting for fill: {e}")
                          try: ex.exchange.cancel_order(order_id, pair)
                          except: pass"""

if target_block in content:
    new_content = content.replace(target_block, replacement_block)
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("✅ Successfully patched bot_executor.py")
else:
    print("❌ Target block not found. Checking indentation...")
    # Debug: Print the section around where we expect it
    start_idx = content.find("final_order = ex.wait_for_fill")
    if start_idx != -1:
        print(f"Found wait_for_fill at index {start_idx}. Context:")
        print(content[start_idx-100:start_idx+300])
    else:
        print("Could not find wait_for_fill call at all.")
