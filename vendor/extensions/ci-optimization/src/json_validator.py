import json

def is_valid_json(json_string):
    try:
        json.loads(json_string)
        return True
    except ValueError:
        return False
