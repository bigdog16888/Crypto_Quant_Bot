import os

def tail(filename, n=50):
    try:
        with open(filename, 'rb') as f:
            f.seek(0, 2)
            filesize = f.tell()
            block_size = 1024
            
            lines = []
            block = b""
            
            # Read backwards
            for i in range(1, int(filesize / block_size) + 2):
                if filesize < block_size * i:
                    f.seek(0)
                    block = f.read(filesize)
                else:
                    f.seek(filesize - block_size * i)
                    block = f.read(block_size)
                
                # Split lines
                chunk = block + b"".join(lines[0:1]) if lines else block
                new_lines = chunk.splitlines()
                
                # Keep collecting lines
                if lines:
                    lines = new_lines[:-1] + [new_lines[-1] + lines[0]] + lines[1:]
                else:
                    lines = new_lines
                
                if len(lines) > n:
                    break
            
            # Print last n lines
            for line in lines[-n:]:
                print(line.decode('utf-8', errors='ignore'))
                
    except Exception as e:
        print(f"Error reading log: {e}")

if __name__ == "__main__":
    tail('engine.log', 100)
