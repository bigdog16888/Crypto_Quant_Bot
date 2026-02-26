def extract_post():
    with open('engine.log', 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    post_lines = [l for l in lines[-5000:] if 'RAW POST' in l and 'BTCUSDC' in l]
    
    with open('_post_analysis.txt', 'w', encoding='utf-8') as f:
        f.writelines(post_lines[-50:])

extract_post()
print("Post logs extracted.")
