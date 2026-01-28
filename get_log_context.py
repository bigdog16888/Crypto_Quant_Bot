
import os

def context_search(filename, pattern):
    with open(filename, 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 20000)) # Last 20KB
        text = f.read().decode('utf-8', errors='ignore')
        
        if pattern in text:
            index = text.find(pattern)
            start = max(0, index - 200)
            end = min(len(text), index + 200)
            print("--- CONTEXT ---")
            print(text[start:end])
        else:
            print("Pattern not found in last 20KB")

context_search('engine.log', 'choose reduce only')
