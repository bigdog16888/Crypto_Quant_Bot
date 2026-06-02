import os
import json

import os

brain_dir = r"C:\Users\Gionie\.gemini\antigravity\brain"
for folder in os.listdir(brain_dir):
    folder_path = os.path.join(brain_dir, folder)
    if os.path.isdir(folder_path):
        transcript_path = os.path.join(folder_path, ".system_generated", "logs", "transcript.jsonl")
        if os.path.exists(transcript_path):
            with open(transcript_path, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f):
                    if '64.18' in line:
                        print(f"[{folder}] Line {i}: {line[:200]}...")
