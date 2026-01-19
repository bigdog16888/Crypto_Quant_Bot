from engine.database import get_bot_params
import json

def dump_bot(bot_id):
    params = get_bot_params(bot_id)
    if params:
        name, pair, direction, rsi_limit, mm, base, strat, config_json = params
        print(f"Bot #{bot_id}: {name} ({pair})")
        print(f"Config: {config_json}")
    else:
        print(f"Bot #{bot_id} not found.")

if __name__ == "__main__":
    dump_bot(2)
