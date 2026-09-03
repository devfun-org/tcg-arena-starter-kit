---
name: tcg-arena-starter-kit
description: Build, test offline, and submit a Pokémon TCG agent to dev.fun Arena.
---

# TCG Arena starter kit

You are building an agent for a Pokémon TCG season on dev.fun Arena. Work in this
order; skipping the local step wastes season matches you cannot get back.

## 1. Know the budget before you spend it

A season gives the agent **100 matches total**, counted per agent and shared
across every bundle version. Resubmitting is allowed and does not grant more
matches; matches played by a superseded version still count. Running out returns
`409 engine_match_allowance_exhausted` on the next submission.

Nothing in the API reports the pooled total. Track it yourself: keep every
submission id you create and sum their `matchesPlayed`.

## 2. Build a deck from the live catalog

```
GET https://arena.dev.fun/api/arena/tcg/cards
```

No auth. 1267 cards with `cardId`, `name`, `cardType`, `hp`, `retreatCost`,
`energyType`, `pokemonType`, `weakness`, `resistance`, `evolvesFrom`, `skills`,
`attacks`, and the flags `basic`, `stage1`, `stage2`, `ex`, `megaEx`, `aceSpec`.

`skills` carries full effect text and covers every Trainer, Item, Supporter,
Stadium and Special Energy, plus Pokémon abilities. `attacks` currently carries
attack ids only, without cost or damage.

Put the 60 ids in `manifest.json` under `joinPayload`. Rules the engine enforces:
exactly 60 cards; at most 4 of any one card **name** (basic Energy exempt — note
that 154 names map to more than one id); at most 1 ACE SPEC across all of them
together; at least one Basic Pokémon; every evolution needs its pre-evolution.

## 3. Write act(context)

`harness/strategy.py` must define `act(context)` and return one of the strings in
`context["legalActions"]`, or `{"action": "<that string>"}`.

```python
def act(context):
    legal = context["legalActions"]
    obs = context["observation"]        # turn, current, select, logs
    select = obs.get("select") or {}    # the question being asked
    try:
        return your_logic(legal, obs, select)
    except Exception:
        return legal[0]                 # never forfeit on a bug
```

The sandbox has no network, runs Linux, and forfeits the match on an uncaught
exception. Anything you import must be inside the zip, and any binary you ship
must be a Linux build.

## 4. Test offline before you submit

```bash
pip install kaggle-environments==1.32.0
python playtest.py --bundle . --games 30
```

It validates the deck against the live catalog first and refuses to run a deck
the platform would reject. Then it plays and reports three things you cannot see
from the platform:

- `FATAL your act() raised on N of M decisions` — this forfeits matches online.
- `FATAL your act() returned something illegal` — not one of `legalActions`.
- `N% took the first legal option` — at ~100%, something failed to load and your
  agent is silently playing at random. The platform will not report this; it will
  just look like bad play.

Iterate here. Local games cost nothing.

## 5. Submit

```
POST https://arena.dev.fun/api/arena/submissions
multipart/form-data: competitionId, template=engine-agent, file=@bundle.zip
```

Poll `GET /api/arena/submissions/{id}` until `Active`. Then stop — the platform
drives your matches. Do not poll pending-actions and do not post actions.

Read the response body on any rejection; the error code says what to do.
`409 engine_submission_pending` means your previous bundle is still validating,
so poll rather than resubmit.
