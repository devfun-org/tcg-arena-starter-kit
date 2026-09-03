"""The greedy baseline the playtester uses as your opponent: always the first
legal option. With this engine's option ordering that means "attach energy to
the active Pokemon, attack when able" — competent, not a floor.

Submit this first if you just want to prove the pipeline end to end.
"""


def act(context):
    return context["legalActions"][0]
