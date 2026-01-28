
import re

file_path = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\engine\bot_executor.py"

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Regex to match the block loosely
# Matches:
# try:
#   final_order = ex.wait_for_fill(...)
#   ...
# except Exception:
#   ...
#   break (end of block)

pattern = r"(try:\s+final_order = ex\.wait_for_fill\(order, timeout_seconds=current_timeout\).*?except Exception:.*?break)"

# The replacement block (Robust logic)
# We need to ensure indentation is correct for the replacement (14 spaces to match 'try:')
indent = " " * 22 # Based on previous view_file (658 was 22 chars in?)
# Actually, let's just use the indentation from the match if possible, or hardcode what we saw.
# In step 405, 658: "                      try:" (22 spaces)
# So we use 22 spaces.

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

match = re.search(pattern, content, re.DOTALL)
if match:
    print("✅ Found match!")
    # We replace the match with our clean block
    # Note: Regex replacement needs care.
    new_content = content.replace(match.group(1), replacement_block)
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("✅ Successfully patched bot_executor.py")
else:
    print("❌ Pattern not found.")
    # Dump context for debugging
    idx = content.find("wait_for_fill")
    if idx != -1:
        print("Context around wait_for_fill:")
        print(content[idx-50:idx+200])
