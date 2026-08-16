import json

def json_to_dict(json_string):
    return json.loads(json_string)

def dict_to_json(dictionary):
    return json.dumps(dictionary)
