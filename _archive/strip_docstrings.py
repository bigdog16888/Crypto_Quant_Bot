import re

filename = r"c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\engine\database.py"

with open(filename, 'r', encoding='utf-8') as f:
    text = f.read()

# Regex to remove triple-quoted strings (docstrings)
# Matches """...""" or '''...''' including newlines, non-greedy
pattern = re.compile(r'(""".*?"""|\'\'\'.*?\'\'\')', re.DOTALL)

new_text = re.sub(pattern, '# Docstring removed', text)

with open(filename, 'w', encoding='utf-8') as f:
    f.write(new_text)

print("Docstrings stripped.")
