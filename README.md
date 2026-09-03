# TCG Arena starter kit

A Pokémon TCG bundle that already runs, plus an offline playtester.

The point of the playtester: **a season gives you 100 matches and they are gone
fast.** Every version you submit spends from that same 100 — a new deck does not
reset it. Local games cost nothing, so find out here that a deck cannot open or
that your code silently broke, not on the ladder.

## Quickstart

```bash
git clone https://github.com/devfun-org/tcg-arena-starter-kit
cd tcg-arena-starter-kit
pip install kaggle-environments==1.32.0

python playtest.py --bundle . --games 30
```

That plays your bundle against a baseline opponent and reports what happened.
Nothing proprietary ships here; the engine comes from the package you just
installed, which is the same one the Arena runs.

## What the playtester checks

Before a single game it validates your deck against the live card catalog and
refuses to run if the platform would reject it:

```
This deck would be REJECTED by the platform:
  - more than 4 copies of the same card NAME (not id): {'Yveltal': 5}
  - at most 1 ACE SPEC card in the whole deck; found 2
  - evolutions with no pre-evolution in the deck (dead cards): missing ['Scraggy']
```

Then it plays, and afterwards it tells you about the two failures that are
otherwise invisible:

```
FATAL your act() raised on 236 of 236 decisions.
  first error: RuntimeError: boom
  On the platform an uncaught exception FORFEITS the match immediately.
```

```
decisions: 493, of which 99% took the first legal option
  WARNING that is what a fallback path looks like: your code never once chose
  differently from the trivial baseline. Something it needs probably failed to
  load and it is silently degrading. The platform will not tell you either — it
  will just look like bad play.
```

That second one is the expensive bug. A bundle whose import failed still
validates, still shows `Active`, still plays all 100 matches, and loses nearly
all of them, with nothing anywhere saying the brain never loaded.

## Write your agent

`harness/strategy.py` defines `act(context)`. The platform calls it at every
decision point.

```python
def act(context):
    legal = context["legalActions"]      # every legal choice, already enumerated
    obs = context["observation"]         # turn, current board, select, logs
    return legal[0]                      # return one of those strings
```

Return one of the strings in `context["legalActions"]`, or
`{"action": "<that string>"}`. Both are accepted.

Three sandbox rules that catch people out:

- **No network.** Anything you import must be inside the zip. You cannot pip
  install at match time.
- **Linux only.** Any binary you ship must be built for the platform's Linux
  sandbox. A macOS build will fail to load, and if your code catches that, you
  get the silent degradation above.
- **An uncaught exception forfeits the match.** Always fall back to a legal
  action. A weak move scores better than a forfeit.

`examples/` has two drop-in alternatives to compare against.

## Build a deck

`manifest.json` holds your 60 card ids under `joinPayload`. The deck shipped here
is legal and functional but deliberately plain — four non-ex basics and a lot of
energy. Beating it is the easy part.

The full catalog is public and needs no auth:

```
GET https://arena.dev.fun/api/arena/tcg/cards
```

1267 cards with `hp`, `retreatCost`, `energyType`, `weakness`, `resistance`,
`evolvesFrom`, `skills` (full effect text for every Trainer, Item, Supporter,
Stadium and Special Energy), and the flags `basic` / `stage1` / `stage2` / `ex` /
`megaEx` / `aceSpec`.

Rules the engine enforces:

| Rule | Note |
|---|---|
| Exactly 60 cards | |
| At most 4 of any one card **name** | Name, not id — 154 names have more than one id. Basic Energy is exempt |
| At most 1 ACE SPEC in the whole deck | Across all 29 of them together, not one each |
| At least one Basic Pokémon | Or you cannot open |
| An evolution needs its pre-evolution | Otherwise it is a dead card |

## Submit

```bash
zip -r bundle.zip manifest.json harness assets
curl -X POST https://arena.dev.fun/api/arena/submissions \
  -H "x-arena-api-key: $ARENA_KEY" \
  -F competitionId=$COMPETITION_ID \
  -F template=engine-agent \
  -F file=@bundle.zip
```

Poll `GET /api/arena/submissions/{id}` until it reads `Active`. Then the platform
pairs and plays you; do not poll for actions and do not post any.

## Your season budget

100 matches per season, per agent, shared across every version you submit.
Matches played by a superseded version still count, permanently.

You can resubmit as often as you like — what runs out is matches, not
submissions. Pairing is fast enough that a full allowance can be spent in under
half an hour, so revise early, while enough is left to prove the replacement.

## What is in the box

| | |
|---|---|
| `manifest.json` | A legal 60-card deck, ready to submit |
| `harness/strategy.py` | Commented `act()` template |
| `playtest.py` | Offline playtester and deck validator |
| `examples/` | Drop-in alternative strategies |

MIT licensed. Pokémon card data and the match engine are the property of their
respective owners and are not covered by this license; this repository does not
redistribute them.
