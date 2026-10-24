"""Bounded advisory review output, with no executable actions or authority."""
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['summary', 'findings'],
    'properties': {
        'summary': {'type': 'string', 'maxLength': 4096},
        'findings': {'type': 'array', 'maxItems': 16,
                     'items': {'type': 'string', 'maxLength': 2048}},
    },
}


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'summary', 'findings'}
            or not isinstance(value['summary'], str) or len(value['summary']) > 4096
            or not isinstance(value['findings'], list) or len(value['findings']) > 16
            or any(not isinstance(item, str) or len(item) > 2048 for item in value['findings'])):
        raise ValueError('Invalid advisory review')
    return value
