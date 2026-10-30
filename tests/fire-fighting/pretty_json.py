import json

json_data = {}


def pretty_json(json_data):
    return json.dumps(json_data, indent=4)


print(pretty_json(json_data=json_data))


def keys(json_data):
    return list(json_data.keys())


print(keys(json_data=json_data))
