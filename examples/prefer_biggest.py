"""Take the largest legal selection - "use what you can" rather than "do nothing".

This is what harness/strategy.py ships with. It is a placeholder, not a strategy.
"""
import json


def act(context):
    legal = context["legalActions"]
    try:
        return max(legal, key=lambda a: len(json.loads(a)))
    except Exception:
        return legal[0]
