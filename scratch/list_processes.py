import subprocess

try:
    output = subprocess.check_output('wmic process where "name=\'python.exe\'" get CommandLine, ProcessId', shell=True).decode('utf-8', errors='ignore')
    print(output)
except Exception as e:
    print("Error listing processes:", e)
