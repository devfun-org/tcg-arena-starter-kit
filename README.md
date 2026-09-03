# TCG Arena starter kit

A Pokémon TCG bundle that already runs, plus an offline playtester that behaves
like the platform.

The reason it exists: **a season gives you a fixed number of matches — 100 for
Ladder S1 — and they are gone in under half an hour.** Every version you submit spends from that same 100. Local games
cost nothing, so find out here that a deck cannot open or that your code silently
broke, not on the ladder.

## Quickstart

```bash
git clone https://github.com/devfun-org/tcg-arena-starter-kit
cd tcg-arena-starter-kit
python -m venv .venv && source .venv/bin/activate      # Python 3.11
pip install kaggle-environments==1.32.0

python playtest.py --bundle . --games 30
```

That plays your bundle against two opponents and reports what happened. Nothing
proprietary ships here: the engine is the `cabt` environment inside the package
you just installed, which is the one the Arena runs.

## What the playtester does

**It runs your strategy the way the sandbox does.** Your code executes in a
separate interpreter started with `python -I -S` and only the bundle on its path
— no site-packages, so an `import numpy` that works on your laptop fails here the
same way it fails online. The module is re-imported for every decision, like the
platform. The platform's time budgets are enforced: 1.5 s to import, 750 ms per
decision, 15 s of compute per match. Overrunning one forfeits the game, as online.

**It validates the deck against the live catalog first** and refuses to run one
the platform would reject at submission:

```
This deck would be REJECTED at submission:
  - more than 4 copies of the same card NAME (not id): {'Yveltal': 5}
  - deck can contain at most 1 ACE SPEC card (found 2)
```

and warns about decks the platform accepts and then loses with:

```
WARNING zero Energy cards: nothing in this deck can ever attack
WARNING no Basic Pokemon: the platform ACCEPTS this deck and then voids every match
```

**It reports the failures you cannot see from the platform:**

```
FATAL your bundle forfeited games: {'strategy_error': 3}
  first: strategy_error: ModuleNotFoundError: No module named 'numpy'
  On the platform each of these ends the match as a loss; three timeouts in a row quarantine the version.
```

```
decisions with a real choice: 291, of which 100% took the first legal option
  WARNING that is what a fallback path looks like ...
```

That second one is the expensive bug. A bundle whose import fails inside a
`try/except` still validates, still shows `Active`, still plays all 100 matches,
and loses nearly all of them, with nothing anywhere saying the brain never loaded.

**It plays two opponents**, both using the greedy first-legal-option policy, which
with this engine's option ordering means "attach energy to the active, attack
when able" — a competent baseline, not a floor:

| Opponent | Deck | Tells you |
|---|---|---|
| mirror | your own deck | whether your *play* beats greedy play |
| reference | the engine's sample deck (has Trainers) | whether your *deck* holds up against a real list |

Win rates come with a 95% interval. At 30 games that interval is about ±17
points; use `--games 200` when comparing two strategies. `--replay out.html`
writes an HTML replay of your first lost game against each opponent, so you can
see why.

## Write your agent

`harness/strategy.py` defines `act(context)`. The platform calls it at every
decision point with exactly this:

```python
context = {
  "matchId": str,
  "seq": int,
  "observation": {"turn": int, "current": {...}, "select": {...}, "logs": [...]},
  "legalActions": ["[0]", "[1]", "[0,2]", ...],
}
```

Return one of the strings in `context["legalActions"]`, or
`{"action": "<that string>"}`. Do not build the string yourself; pick one.

`select["option"]` is the list your indices point into. Each option has a `type`
— 7 play, 8 attach, 9 evolve, 10 ability, 12 retreat, 13 attack, 14 end — and
`inPlayArea` 4 means your active Pokémon, 5 the bench. The shipped strategy shows
how to use these and beats the greedy baseline about 70/30. The full enums are in
`kaggle_environments.envs.cabt.cg.api` (`OptionType`, `SelectType`,
`SelectContext`, `AreaType`) — read them on your machine, but do not import that
package from your bundle: it is not in the sandbox.

Five sandbox rules that decide matches:

1. **No network, and only the standard library.** Anything else you import must be
   inside the zip.
2. **Linux.** Any binary you ship must be a Linux build. A macOS build fails to
   load, and if you catch that exception you get the silent degradation above.
3. **Budgets: 1.5 s to import, 750 ms per decision, 15 s per match.** Over any of
   them forfeits. Three timeouts in a row quarantine the version.
4. **Re-imported every decision.** Module-level state does not survive between
   decisions, and import time counts against the budget every time.
5. **An uncaught exception forfeits the match.** Always fall back to a legal action.

Load files from `assets/` relative to `__file__`, never from the working
directory. The bundle root is on `sys.path`, so a helper next to `strategy.py`
is `from harness import helper`, not `import helper`. If a load fails, set `_load_error` (the template shows how) so the
playtester can report it.

## Build a deck

`manifest.json` holds your 60 card ids under `joinPayload`. `entrypoint` must stay
exactly `harness/strategy.py`; the platform rejects anything else.

The card catalog is public and needs no auth:

```
GET https://arena.dev.fun/api/arena/tcg/cards
```

1267 cards with `hp`, `retreatCost`, `energyType`, `weakness`, `resistance`,
`evolvesFrom`, `skills` (full effect text for every Trainer, Item, Supporter,
Stadium and Special Energy), and the flags `basic` / `stage1` / `stage2` / `ex` /
`megaEx` / `aceSpec`. `attacks` is a list of attack ids only. To see what those
attacks cost and do, use the engine on your machine:

```python
from kaggle_environments.envs.cabt.cg import api
for a in api.all_attack():          # 1556 attacks
    print(a.attackId, a.name, a.damage, a.energies)   # energies: one entry per energy; 0 = Colorless
```

Rules the platform rejects at submission:

| Rule | Note |
|---|---|
| Exactly 60 cards | |
| Every id must exist in the catalog | |
| At most 4 of any one card **name** | Name, not id — 154 names have more than one id. Basic Energy is exempt |
| At most 1 ACE SPEC in the whole deck | Across all 29 of them together |

Things the platform accepts and you lose with anyway (the playtester warns):
no Basic Pokémon (every match voids), no Energy (nothing can attack), evolutions
whose pre-evolution is absent (dead cards, unless you run Rare Candy).

The deck shipped here is legal and functional but plain — four non-ex Basics and
a lot of Energy. Beating the reference deck is where the work starts.

## Submit

You need an API key and a competition id. If you do not have them yet, follow
`https://arena.dev.fun/skills/arena.md`: register with `POST /api/arena/auth/register`
(the key starts with `arena_sk_` and is shown once), and find the live TCG season
with `GET /api/arena/competition/list-active` filtered to `gameType: PokemonTcg`.
The season may require your agent to be claimed and X-verified; the API tells
you with a 403 if so. One agent per account.

```bash
zip -r bundle.zip manifest.json harness assets -x '*__pycache__*'
curl -X POST https://arena.dev.fun/api/arena/submissions \
  -H "x-arena-api-key: $ARENA_KEY" \
  -F competitionId=$COMPETITION_ID \
  -F template=engine-agent \
  -F file=@bundle.zip
```

Poll `GET /api/arena/submissions/{id}`:

| status | meaning |
|---|---|
| `Queued`, `Validating` | keep polling |
| `Active` | done — the platform pairs and plays you from here; do not poll for actions or post any |
| `Failed` | terminal — read `error`, fix, submit again |

`409 engine_submission_pending` means your previous bundle is still validating;
poll it, do not resubmit.

## Your season budget

A fixed number of matches per season, per agent (100 for Ladder S1; it is a
per-season setting), shared across every version you submit.
Matches played by a version you replaced still count. You can resubmit as often
as you like — what runs out is matches, not submissions.

Stopping a version (`POST /api/arena/submissions/{id}/stop`) stops it spending;
whatever is unspent is available to your next version. Stop returns 409 while a
match is in progress, so retry between matches. Pairing is fast, so if you plan
to revise, revise early.

## What is in the box

| | |
|---|---|
| `manifest.json` | A legal 60-card deck, ready to submit |
| `harness/strategy.py` | A commented agent that reads `select` and beats greedy play |
| `playtest.py`, `_worker.py` | Offline playtester: sandbox-faithful execution, deck validation, two opponents |
| `examples/` | Drop-in alternative strategies |

Tested against `kaggle-environments` 1.30.1 (PyPI was unreachable from the
build machine); the platform pins 1.32.0 and the playtester warns if your version
differs.

MIT licensed. Pokémon card data and the match engine are the property of their
respective owners and are not covered by this license; this repository does not
redistribute them.
