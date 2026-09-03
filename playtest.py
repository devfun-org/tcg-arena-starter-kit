#!/usr/bin/env python3
"""Arena TCG local playtest — play your bundle against a baseline, offline.

  pip install kaggle-environments==1.32.0
  python playtest.py --bundle mybundle.zip --games 50

Nothing proprietary ships with this file. The engine comes from the
kaggle-environments package you install yourself.
"""
import argparse, itertools, json, os, shutil, sys, tempfile, zipfile, collections, importlib.util, warnings, urllib.request
warnings.filterwarnings("ignore")

MAX_LEGAL_ACTIONS = 3000
CARDS_URL = "https://arena.dev.fun/api/arena/tcg/cards"
STATS = {"decisions": 0, "first_choice": 0, "crashed": 0, "illegal": 0, "first_error": None}

def legal_actions(select):
    """Verbatim from the platform runner: every index combination of size
    minCount..maxCount over select.option, as JSON arrays."""
    n = len(select.get("option", []))
    lo = max(0, int(select.get("minCount", 0)))
    hi = min(n, int(select.get("maxCount", 0)))
    out = []
    for size in range(lo, hi + 1):
        if size == 0:
            out.append("[]")
        else:
            for combo in itertools.combinations(range(n), size):
                out.append(json.dumps(list(combo), separators=(",", ":")))
                if len(out) >= MAX_LEGAL_ACTIONS:
                    return out
    return out

def load_act(strategy_path):
    spec = importlib.util.spec_from_file_location("entrant_strategy", strategy_path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(strategy_path))))
    spec.loader.exec_module(mod)
    fn = getattr(mod, "act", None) or getattr(mod, "choose_action", None)
    if not callable(fn):
        raise SystemExit("strategy must define act(context) or choose_action(context)")
    for attr in ("_load_error", "load_error", "LOAD_ERROR"):
        err = getattr(mod, attr, None)
        if err:
            print(f"WARNING your strategy recorded a load error and will run degraded:\n  {attr} = {err!r}\n")
    return fn

def as_kaggle_agent(act, deck, budget_ms=750):
    """Wrap an Arena act(context) so the cabt env can drive it.

    The env asks for the deck first (select is None). The platform supplies that
    from manifest.joinPayload, so your act() never sees it; we do the same here."""
    def agent(obs):
        if obs.get("select") is None:
            return list(deck)
        select = obs.get("select") or {}
        offered = legal_actions(select)
        ctx = {"matchId": "local", "seq": 0, "observation": obs,
               "legalActions": offered, "decisionBudgetMs": budget_ms}
        STATS["decisions"] += 1
        try:
            r = act(ctx)
        except Exception as e:
            STATS["crashed"] += 1
            if STATS["first_error"] is None:
                STATS["first_error"] = f"{type(e).__name__}: {e}"
            return json.loads(offered[0]) if offered else []
        s = r if isinstance(r, str) else (r or {}).get("action")
        if not isinstance(s, str) or s not in offered:
            STATS["illegal"] += 1
            if STATS["first_error"] is None:
                STATS["first_error"] = f"returned {r!r}, which is not one of context['legalActions']"
            return json.loads(offered[0]) if offered else []
        if offered and s == offered[0] and len(offered) > 1:
            STATS["first_choice"] += 1
        return json.loads(s)
    return agent

def make_baseline(deck):
    """Floor opponent: always the first legal action. Plays your own deck, so the
    only thing under test is your strategy code and the deck's ability to function."""
    def agent(obs):
        if obs.get("select") is None:
            return list(deck)
        a = legal_actions(obs.get("select") or {})
        return json.loads(a[0]) if a else []
    return agent

def fetch_catalog():
    """The platform validates decks against this exact catalog."""
    try:
        raw = urllib.request.urlopen(CARDS_URL, timeout=60).read()
    except Exception as e:
        print(f"NOTE could not reach the card catalog ({e}); skipping deck validation.\n")
        return None
    data = json.loads(raw)
    cards = data if isinstance(data, list) else data.get("cards", [])
    return {c["cardId"]: c for c in cards}


def validate_deck(deck):
    """Reproduce the platform's deck rules locally. Returns a list of problems."""
    out = []
    if len(deck) != 60:
        out.append(f"a deck is exactly 60 cards; this one has {len(deck)}")
    by_id = fetch_catalog()
    if by_id is None:
        return out
    unknown = sorted({c for c in deck if c not in by_id})
    if unknown:
        out.append(f"unknown card ids: {unknown[:10]}")
        return out
    counts = collections.Counter()
    for cid in deck:
        card = by_id[cid]
        if card["name"].startswith("Basic {"):
            continue          # basic Energy is exempt from the 4-copy limit
        counts[card["name"]] += 1
    over = {n: c for n, c in counts.items() if c > 4}
    if over:
        out.append(f"more than 4 copies of the same card NAME (not id): {over}")
    ace = [by_id[c]["name"] for c in deck if by_id[c].get("aceSpec")]
    if len(ace) > 1:
        out.append(f"at most 1 ACE SPEC card in the whole deck; found {len(ace)}: {sorted(set(ace))}")
    if not any(by_id[c].get("basic") for c in deck):
        out.append("no Basic Pokemon, so you cannot open a game")
    present = {by_id[c]["name"] for c in deck}
    orphans = sorted({by_id[c]["evolvesFrom"] for c in deck
                      if by_id[c].get("evolvesFrom") and by_id[c]["evolvesFrom"] not in present})
    if orphans:
        out.append(f"evolutions with no pre-evolution in the deck (dead cards): missing {orphans}")
    return out


def open_bundle(path):
    if os.path.isdir(path):
        return path, None
    tmp = tempfile.mkdtemp(prefix="arena-playtest-")
    with zipfile.ZipFile(path) as z:
        z.extractall(tmp)
    return tmp, tmp

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", required=True, help="your bundle.zip or its unzipped directory")
    p.add_argument("--games", type=int, default=30)
    args = p.parse_args()

    root, cleanup = open_bundle(args.bundle)
    try:
        mpath = os.path.join(root, "manifest.json")
        if not os.path.isfile(mpath):
            raise SystemExit(f"no manifest.json in {args.bundle} — a bundle needs manifest.json and harness/strategy.py")
        try:
            manifest = json.load(open(mpath))
        except json.JSONDecodeError as e:
            raise SystemExit(f"manifest.json is not valid JSON: {e}")
        deck = manifest.get("joinPayload")
        if not isinstance(deck, list) or not all(isinstance(c, int) for c in deck):
            raise SystemExit("manifest.joinPayload must be a flat list of integer card ids")
        spath = os.path.join(root, "harness", "strategy.py")
        if not os.path.isfile(spath):
            raise SystemExit("no harness/strategy.py in the bundle")
        problems = validate_deck(deck)
        if problems:
            print("This deck would be REJECTED by the platform:")
            for line in problems:
                print(f"  - {line}")
            raise SystemExit("Fix manifest.joinPayload, then run again.")
        act = load_act(spath)
        me = as_kaggle_agent(act, deck)
        floor = make_baseline(deck)

        try:
            from kaggle_environments import make
        except ImportError:
            raise SystemExit(
                "The match engine is missing. Install it once:\n"
                "    pip install kaggle-environments==1.32.0\n"
                "This is the same engine the Arena runs; nothing else plays this card pool.")
        wins = losses = draws = 0
        reasons = collections.Counter()
        for g in range(args.games):
            env = make("cabt")
            order = [me, floor] if g % 2 == 0 else [floor, me]
            res = env.run(order)
            mine = 0 if g % 2 == 0 else 1
            r = [s.get("reward") for s in res[-1]]
            a, b = r[mine], r[1 - mine]
            if a is None: losses += 1; reasons["crashed_or_timeout"] += 1
            elif b is None: wins += 1
            elif a > b: wins += 1
            elif b > a: losses += 1
            else: draws += 1
            print(f"  game {g+1}/{args.games}: {wins}W/{losses}L/{draws}D", flush=True)
        total = wins + losses
        print(f"\n{wins}W / {losses}L / {draws}D  win rate {wins/total*100:.0f}%" if total else "no decisive games")
        if reasons: print("issues:", dict(reasons))
        d = STATS["decisions"]
        if STATS["crashed"]:
            print(f"\nFATAL your act() raised on {STATS['crashed']} of {d} decisions.")
            print(f"  first error: {STATS['first_error']}")
            print("  On the platform an uncaught exception FORFEITS the match immediately.")
            print("  Wrap your logic and fall back to a legal action. Fix this before you submit.")
        if STATS["illegal"]:
            print(f"\nFATAL your act() returned something illegal on {STATS['illegal']} of {d} decisions.")
            print(f"  first case: {STATS['first_error']}")
            print("  Return one of the strings in context['legalActions'], or {'action': <that string>}.")
        f = STATS["first_choice"]
        if d and not STATS["crashed"] and not STATS["illegal"]:
            pct = f / d * 100
            print(f"decisions: {d}, of which {pct:.0f}% took the first legal option")
            if pct > 98:
                print("  WARNING that is what a fallback path looks like: your code never once chose\n"
                      "  differently from the trivial baseline. Something it needs probably failed to\n"
                      "  load and it is silently degrading. The platform will not tell you either — it\n"
                      "  will just look like bad play. Check your imports, and check that any binary\n"
                      "  you ship is built for Linux.")
            elif pct > 85:
                print("  Note: your strategy rarely differs from picking the first option. That may be\n"
                      "  fine if most decision points offer only one real choice, but it is worth\n"
                      "  confirming your logic is actually running.")
    finally:
        if cleanup: shutil.rmtree(cleanup, ignore_errors=True)

if __name__ == "__main__":
    main()
