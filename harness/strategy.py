"""Your agent. The platform calls act(context) at every decision point.

context = {
  "matchId": str,
  "seq": int,                      # decisions so far in this match, both players
  "observation": {
      "turn": int,
      "current": {...},            # the board: players, active, bench, hand, prizes, ...
      "select": {...},             # the question being asked (see below)
      "logs": [...],
  },
  "legalActions": ["[0]", "[1]", "[0,2]", ...],   # every legal answer, already enumerated
}

Return one of the strings in context["legalActions"], or {"action": "<that string>"}.
Do not build the string yourself; pick one from the list.

Sandbox rules that decide matches:
  - No network. Anything you import must be inside the zip; the sandbox has only the
    Python 3.11 standard library.
  - Linux. Any binary you ship must be a Linux build.
  - 1.5 s to import, 750 ms per decision, 15 s of compute per match. Over any of them
    forfeits the match.
  - Your module is re-imported for EVERY decision. Nothing you store at module level
    survives to the next one, and import time counts against the budget.
  - An uncaught exception forfeits the match. Always fall back to a legal action.

Reading `select`: each entry in select["option"] has a "type". The values you will
care about first (from the engine's OptionType enum):
    7 PLAY      put a card from hand onto the board
    8 ATTACH    attach an Energy card
    9 EVOLVE    evolve a Pokemon
   10 ABILITY   use an ability
   12 RETREAT   swap the active for a benched Pokemon
   13 ATTACK    attack with the active Pokemon
   14 END       end your turn
An option's "inPlayArea" is 4 for your active Pokemon and 5 for the bench.
"""
import json
import os

# Files you ship go under assets/. Resolve them from __file__, never from the
# working directory. If something fails to load, record it in _load_error and
# fall back; the playtester surfaces _load_error so you find out.
_ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
_load_error = None
try:
    pass  # e.g. TABLE = json.load(open(os.path.join(_ASSETS, "table.json")))
except Exception as e:  # noqa: BLE001
    _load_error = repr(e)

PLAY, ATTACH, EVOLVE, ABILITY, RETREAT, ATTACK, END = 7, 8, 9, 10, 12, 13, 14
ACTIVE = 4


def act(context):
    legal = context["legalActions"]
    try:
        return choose(context["observation"], legal)
    except Exception:  # noqa: BLE001
        return legal[0]  # a weak move beats a forfeit


def choose(obs, legal):
    select = obs.get("select") or {}
    options = select.get("option") or []

    # Only single-pick prompts get this treatment; multi-pick prompts (bench setup,
    # discards) take the first legal answer, which is usually "choose the minimum".
    single = [a for a in legal if len(json.loads(a)) == 1]
    if not single or len(single) != len(legal):
        return legal[0]

    def first(pred):
        for a in single:
            i = json.loads(a)[0]
            if i < len(options) and pred(options[i]):
                return a
        return None

    # A turn in the right order: develop the board and power up the active Pokemon
    # first, and attack LAST, because attacking ends the turn. Attacking before you
    # attach energy throws away that energy for the turn.
    return (first(lambda o: o.get("type") == EVOLVE)
            or first(lambda o: o.get("type") == ABILITY)
            or first(lambda o: o.get("type") == PLAY)
            or first(lambda o: o.get("type") == ATTACH and o.get("inPlayArea") == ACTIVE)
            or first(lambda o: o.get("type") == ATTACH)
            or first(lambda o: o.get("type") == ATTACK)
            or first(lambda o: o.get("type") == END)
            or legal[0])
