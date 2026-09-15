"""A single presentation wrapper, never a permissive JSON/command extractor."""
import re


def unwrap_json(text):
    if not isinstance(text, str):
        raise ValueError('JSON response must be text')
    value = text.strip()
    if value.startswith('```'):
        match = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n```', value, re.S | re.I)
        if not match:
            raise ValueError('multiple, incomplete or mixed JSON fences')
        value = match[1].strip()
    return value
