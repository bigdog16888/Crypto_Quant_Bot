
import os

def tail_search(filename, pattern, chunk_size=1024, max_chunks=10):
    with open(filename, 'rb') as f:
        f.seek(0, 2)
        file_size = f.tell()
        
        for i in range(max_chunks):
            pos = max(0, file_size - (i + 1) * chunk_size)
            length = min(chunk_size, file_size - pos)
            f.seek(pos)
            chunk = f.read(length)
            
            try:
                text = chunk.decode('utf-8', errors='ignore')
                if pattern in text:
                    # Find the specific line
                    lines = text.split('\n')
                    for line in lines:
                        if pattern in line:
                            print("FOUND MATCH:")
                            print(line.strip())
                            return
            except:
                pass
            
            if pos == 0:
                break
    print("Pattern not found")

print("Searching for 'reduce only'...")
tail_search('engine.log', 'reduce only', chunk_size=10240)

print("\nSearching for 'Error'...")
tail_search('engine.log', 'Error', chunk_size=10240)
