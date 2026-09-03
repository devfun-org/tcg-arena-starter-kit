"""Pick uniformly at random among the legal actions.

Copy over harness/strategy.py to try it:
    cp examples/random_legal.py harness/strategy.py
"""
import random


def act(context):
    legal = context["legalActions"]
    try:
        return random.choice(legal)
    except Exception:
        return legal[0]
