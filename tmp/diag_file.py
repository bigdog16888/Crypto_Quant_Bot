with open(r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\engine\bot_management.py', 'rb') as f:
    content = f.read()
    if b'\x00' in content:
        print("FOUND NULL BYTE")
    else:
        print("NO NULL BYTE")
    
    # Check for non-ascii
    try:
        content.decode('utf-8')
        print("UTF-8 OK")
    except Exception as e:
        print(f"UTF-8 ERROR: {e}")
