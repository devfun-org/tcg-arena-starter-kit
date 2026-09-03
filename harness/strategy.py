"""Your agent. The platform calls act(context) at every decision point.

context = {
  "observation": {"turn": int, "current": {...}, "select": {...}, "logs": [...]},
  "legalActions": ["[0]", "[1]", "[0,2]", ...],   # every legal choice, already enumerated
  "decisionBudgetMs": int,
}

Return one of the strings in context["legalActions"], or {"action": "<that string>"}.

Hard rules of the sandbox:
  - No network. Anything you import must be inside this zip.
  - Any binary you ship must be built for Linux; the sandbox is Linux.
  - An uncaught exception forfeits the match. Always fall back to a legal action.
"""


def act(context):
    legal = context["legalActions"]
    obs = context["observation"]
    select = obs.get("select") or {}

    try:
        return choose(legal, obs, select)
    except Exception:
        # Never let a bug forfeit the match. A weak move beats no move.
        return legal[0]


def choose(legal, obs, select):
    # ---------------------------------------------------------------
    # YOUR LOGIC GOES HERE.
    #
    # `select` describes the question being asked: minCount / maxCount,
    # the option list, and what triggered it. Read it and decide.
    #
    # The starting behaviour below takes the largest legal selection,
    # which tends to mean "use what you can" rather than "do nothing".
    # It is a placeholder, not a strategy. Beating it is your first job.
    # ---------------------------------------------------------------
    import json
    return max(legal, key=lambda a: len(json.loads(a)))
