#!/usr/bin/env python3
"""Arena TCG local playtest.

  pip install kaggle-environments==1.32.0
  python playtest.py --bundle . --games 30

Plays your bundle offline against two opponents and reports what the platform
would never tell you. Nothing proprietary ships here; the engine comes from the
kaggle-environments package you install yourself, which is what the Arena runs.

Your strategy runs in a separate interpreter started with `python -I -S` and
sys.path = [bundle root], so it sees no site-packages, exactly like the sandbox.
It is re-imported for every decision, like the sandbox. The platform's time
budgets are enforced: 1.5 s to import, 750 ms per decision, 15 s of compute per
match. Overrunning any of them forfeits the game here, as it does online.
"""
import argparse, collections, importlib, itertools, json, logging, math, os, shutil, statistics
import subprocess, sys, tempfile, time, urllib.request, zipfile

logging.getLogger("kaggle_environments").setLevel(logging.ERROR)

ENGINE_VERSION = "1.32.0"
CARDS_URL = "https://arena.dev.fun/api/arena/tcg/cards"
MAX_LEGAL_ACTIONS = 1000          # full_match_runner.py MAX_LEGAL_ACTIONS
MAX_DECISIONS = 1000              # full_match_runner.py MAX_DECISIONS: past this the platform voids the match
IMPORT_BUDGET_MS = 1500           # hosted_runner.py startup validation
DECISION_BUDGET_MS = 750          # arena-engine-tcg-panel-match.ts DECISION_BUDGET_MS
MATCH_COMPUTE_BUDGET_MS = 15000   # entrant cumulative compute per match
ENTRYPOINT = "harness/strategy.py"

# The engine's own sample deck (kaggle_environments/envs/cabt/cabt.py). A real
# list with trainers, so your deck is measured against something other than itself.
REFERENCE_DECK = [721, 721, 722, 722, 722, 722, 723, 723, 723, 723, 1092, 1121, 1121, 1145, 1145,
                  1163, 1163, 1219, 1219, 1219, 1219, 1227, 1227, 1227, 1227, 1262, 1262] + [3] * 33

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- engine facing

def legal_actions(select):
    """Verbatim behaviour of the platform runner, including the 1000 cap."""
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


def plain(obj):
    """kaggle hands agents a Struct (attribute access); the platform hands a dict."""
    return json.loads(json.dumps(obj))


def platform_context(match_id, seq, view, offered):
    """Exactly what full_match_runner.py builds, minus decisionBudgetMs (popped before act)."""
    current = view.get("current") or {}
    return {
        "matchId": match_id,
        "seq": seq,
        "observation": {
            "turn": current.get("turn"),
            "current": current,
            "select": view.get("select") or {},
            "logs": view.get("logs", []),
        },
        "legalActions": offered,
    }


class Forfeit(Exception):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


class Worker:
    """One isolated strategy process per game."""

    def __init__(self, root):
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-S", os.path.join(HERE, "_worker.py"), root],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.compute_ms = 0.0
        self.first = True

    def decide(self, context):
        remaining = MATCH_COMPUTE_BUDGET_MS - self.compute_ms
        if remaining <= 0:
            raise Forfeit("strategy_compute_budget_exhausted",
                          f"used the whole {MATCH_COMPUTE_BUDGET_MS} ms compute budget for this match")
        budget = (IMPORT_BUDGET_MS + DECISION_BUDGET_MS) if self.first else DECISION_BUDGET_MS
        self.proc.stdin.write(json.dumps({"context": context}) + "\n")
        self.proc.stdin.flush()
        t0 = time.perf_counter()
        line = self._readline(budget / 1000.0)
        wall = (time.perf_counter() - t0) * 1000
        if line is None:
            self.kill()
            which = "strategy_startup_timeout" if self.first else "strategy_decision_timeout"
            raise Forfeit(which, f"act() took more than {budget:.0f} ms (budget: import {IMPORT_BUDGET_MS} ms"
                                 f"{' on first call' if self.first else ''}, then {DECISION_BUDGET_MS} ms per decision)")
        self.first = False
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            raise Forfeit("strategy_error", f"worker returned garbage: {line[:120]!r}")
        self.compute_ms += r.get("importMs", 0) + r.get("computeMs", 0)
        if "error" in r:
            raise Forfeit("strategy_error", r["error"] + ("\n" + r["traceback"] if r.get("traceback") else ""))
        return r["action"], wall, r.get("importMs", 0)

    def _readline(self, timeout_s):
        import select as _sel
        ready, _, _ = _sel.select([self.proc.stdout], [], [], timeout_s)
        if not ready:
            return None
        return self.proc.stdout.readline()

    def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass

    def close(self):
        try:
            self.proc.stdin.write(json.dumps({"op": "exit"}) + "\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=2)
        except Exception:
            self.kill()


# --------------------------------------------------------------------------- deck validation

def fetch_catalog(competition_id=None):
    url = CARDS_URL + (f"?competitionId={competition_id}" if competition_id else "")
    raw = urllib.request.urlopen(url, timeout=60).read()
    data = json.loads(raw)
    cards = data if isinstance(data, list) else data.get("cards", [])
    return {c["cardId"]: c for c in cards}


def attack_table():
    try:
        from kaggle_environments.envs.cabt.cg import api
        # all_attack() returns Attack dataclasses. energies lists one entry per energy
        # required; 0 is Colorless, which still costs one energy of any type.
        return {a.attackId: {"name": a.name, "damage": a.damage, "cost": len(a.energies)} for a in api.all_attack()}
    except Exception:
        return {}


def validate_deck(deck, by_id):
    """Returns (rejections, warnings). Rejections mirror server.py validate_deck exactly."""
    rej, warn = [], []
    if len(deck) != 60:
        rej.append(f"a deck is exactly 60 cards; this one has {len(deck)}")
    unknown = sorted({c for c in deck if c not in by_id})
    if unknown:
        rej.append(f"unknown card ids: {unknown[:10]}")
        return rej, warn
    import re
    basic_energy = re.compile(r"^Basic \{[A-Z]\} Energy$")
    counts = collections.Counter()
    for cid in deck:
        name = by_id[cid]["name"]
        if basic_energy.match(name):
            continue
        counts[name] += 1
    over = {n: c for n, c in counts.items() if c > 4}
    if over:
        rej.append(f"more than 4 copies of the same card NAME (not id): {over}")
    ace = sum(1 for c in deck if by_id[c].get("aceSpec") is True)
    if ace > 1:
        rej.append(f"deck can contain at most 1 ACE SPEC card (found {ace})")

    # Not submit-time rejections. The platform accepts these and then voids or loses every match.
    if not any(by_id[c].get("basic") for c in deck):
        warn.append("no Basic Pokemon: the platform ACCEPTS this deck and then voids every match "
                    "as entrant_deck_invalid, because you cannot open a game")
    present = {by_id[c]["name"] for c in deck}
    orphans = sorted({by_id[c]["evolvesFrom"] for c in deck
                      if by_id[c].get("evolvesFrom") and by_id[c]["evolvesFrom"] not in present})
    if orphans:
        warn.append(f"evolutions whose pre-evolution is not in the deck: missing {orphans}. "
                    "Legal (Rare Candy can skip a stage), but dead cards without one")
    attacks = attack_table()
    if attacks:
        energy = sum(1 for c in deck if by_id[c].get("cardType") in (5, 6))
        costs = [attacks[a]["cost"] for c in deck for a in (by_id[c].get("attacks") or []) if a in attacks]
        if costs and energy == 0:
            warn.append("zero Energy cards: nothing in this deck can ever attack")
        elif costs and energy < min(costs):
            warn.append(f"{energy} Energy cards but the cheapest attack costs {min(costs)}")
    return rej, warn


# --------------------------------------------------------------------------- bundle

def open_bundle(path):
    if os.path.isdir(path):
        return os.path.abspath(path), None
    tmp = tempfile.mkdtemp(prefix="arena-playtest-")
    with zipfile.ZipFile(path) as z:
        z.extractall(tmp)
    entries = [e for e in os.listdir(tmp) if e != "__MACOSX"]
    if "manifest.json" not in entries and len(entries) == 1 and os.path.isdir(os.path.join(tmp, entries[0])):
        inner = os.path.join(tmp, entries[0])
        if os.path.isfile(os.path.join(inner, "manifest.json")):
            print(f"NOTE {path} has a top-level folder '{entries[0]}/'. manifest.json must be at the zip root.\n"
                  f"     Build it from inside the directory: cd {entries[0]} && zip -r ../bundle.zip . -x '*__pycache__*'\n"
                  f"     Testing the inner folder anyway.\n")
            return inner, tmp
    return tmp, tmp


def load_manifest(root):
    mpath = os.path.join(root, "manifest.json")
    if not os.path.isfile(mpath):
        raise SystemExit("no manifest.json at the bundle root — a bundle needs manifest.json and harness/strategy.py")
    try:
        m = json.load(open(mpath))
    except json.JSONDecodeError as e:
        raise SystemExit(f"manifest.json is not valid JSON: {e}")
    ep = m.get("entrypoint")
    if ep != ENTRYPOINT:
        raise SystemExit(f"manifest.entrypoint must be exactly \"{ENTRYPOINT}\" (the platform rejects anything else); got {ep!r}")
    deck = m.get("joinPayload")
    if not isinstance(deck, list):
        raise SystemExit("manifest.joinPayload must be a list of 60 integer card ids")
    for i, c in enumerate(deck):
        if not isinstance(c, int) or isinstance(c, bool):
            raise SystemExit(f"manifest.joinPayload[{i}] is {c!r}; every entry must be an integer card id")
    if not os.path.isfile(os.path.join(root, ENTRYPOINT)):
        raise SystemExit(f"no {ENTRYPOINT} in the bundle")
    return deck


# --------------------------------------------------------------------------- play

def greedy(deck):
    """Opponent: the first legal option. With this engine's option ordering that is
    'attach energy to the active, attack when able' — a competent baseline, not a floor."""
    def agent(obs):
        if obs.get("select") is None:
            return list(deck)
        a = legal_actions(plain(obs.get("select") or {}))
        return json.loads(a[0]) if a else []
    return agent


def infer_reason(final_steps, my_index, i_lost):
    """The local env does not surface the platform's result reason. Each player's last
    observation is a slightly different snapshot (the loser's is one action stale), so
    read both boards and take the first explanation consistent with the result."""
    def from_view(view):
        players = (view.get("current") or {}).get("players") or []
        if len(players) < 2:
            return None
        me, opp = players[my_index], players[1 - my_index]
        if i_lost:
            if len(opp.get("prize") or []) == 0:
                return "opponent took all prizes"
            if not (me.get("active") or []) or (me.get("active") or [None])[0] is None:
                return "no active Pokemon left"
            if (me.get("deckCount") or 0) == 0:
                return "decked out"
        else:
            if len(me.get("prize") or []) == 0:
                return "took all prizes"
            if not (opp.get("active") or []) or (opp.get("active") or [None])[0] is None:
                return "opponent had no active Pokemon"
            if (opp.get("deckCount") or 0) == 0:
                return "opponent decked out"
        return None
    try:
        views = [plain(st.get("observation") or {}) for st in final_steps]
        for v in sorted(views, key=lambda v: -((v.get("current") or {}).get("turnActionCount") or 0)):
            r = from_view(v)
            if r:
                return r
    except Exception:
        pass
    return "unknown"


def play_one(root, deck, opp_deck, game_no, stats):
    from kaggle_environments import make
    worker = Worker(root)
    state = {"seq": 0, "forfeit": None, "void": None, "last_view": None, "my_index": game_no % 2}
    match_id = f"local-{game_no}"

    def me(obs):
        if obs.get("select") is None:
            return list(deck)
        view = plain(obs)
        state["last_view"] = view
        offered = legal_actions(view.get("select") or {})
        if len(offered) >= MAX_LEGAL_ACTIONS:
            stats["truncated"] += 1
        ctx = platform_context(match_id, state["seq"], view, offered)
        state["seq"] += 1
        stats["decisions"] += 1
        if state["seq"] > MAX_DECISIONS and not state["void"]:
            state["void"] = "match_decision_limit"
        if state["forfeit"] or state["void"]:
            return [-1]
        try:
            action, wall_ms, import_ms = worker.decide(ctx)
        except Forfeit as f:
            state["forfeit"] = f
            stats["forfeits"][f.reason] += 1
            if stats["first_error"] is None:
                stats["first_error"] = f"{f.reason}: {f.detail}"
            return [-1]
        stats["wall_ms"].append(wall_ms)
        if action not in offered:
            state["forfeit"] = Forfeit("strategy_illegal_action",
                                       f"returned {action!r}, which is not one of context['legalActions']")
            stats["forfeits"]["strategy_illegal_action"] += 1
            if stats["first_error"] is None:
                stats["first_error"] = state["forfeit"].detail
            return [-1]
        if len(offered) > 1:
            stats["multi"] += 1
            if action == offered[0]:
                stats["first_choice"] += 1
        return json.loads(action)

    def opp(obs):
        if obs.get("select") is None:
            return list(opp_deck)
        state["seq"] += 1
        if state["seq"] > MAX_DECISIONS and not state["void"]:
            state["void"] = "match_decision_limit"
        if state["void"]:
            return [-1]
        a = legal_actions(plain(obs.get("select") or {}))
        return json.loads(a[0]) if a else []

    env = make("cabt")
    order = [me, opp] if state["my_index"] == 0 else [opp, me]
    try:
        res = env.run(order)
    finally:
        worker.close()
    last = res[-1]
    mine, theirs = last[state["my_index"]], last[1 - state["my_index"]]
    if state["void"]:
        return "void", f"{state['void']} (the platform voids matches past {MAX_DECISIONS} decisions; not counted)", env
    if state["forfeit"]:
        return "forfeit", state["forfeit"].reason, env
    if mine.get("status") == "INVALID" or (theirs.get("status") == "INVALID" and mine.get("reward") is None):
        raise SystemExit("the engine rejected a deck at match start (kaggle status INVALID). "
                         "Run with validation on, or check the deck for a rule the validator does not cover.")
    if theirs.get("reward") is None or mine.get("reward") is None:
        return "void", "engine error (not counted)", env
    a, b = mine["reward"], theirs["reward"]
    outcome = "win" if a > b else "loss" if b > a else "draw"
    reason = infer_reason(last, state["my_index"], outcome == "loss")
    return outcome, reason, env


def wilson(w, n, z=1.96):
    if n == 0:
        return 0, 0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0, c - h), min(1, c + h)


def run_series(root, deck, opp_deck, games, label, replay_path=None):
    stats = {"decisions": 0, "multi": 0, "first_choice": 0, "truncated": 0,
             "forfeits": collections.Counter(), "first_error": None, "wall_ms": [], "replay_env": None}
    tally = collections.Counter()
    reasons = collections.Counter()
    print(f"vs {label}: ", end="", flush=True)
    for g in range(games):
        outcome, reason, env = play_one(root, deck, opp_deck, g, stats)
        tally[outcome] += 1
        if outcome in ("loss", "forfeit"):
            reasons[reason] += 1
            if stats["replay_env"] is None:
                stats["replay_env"] = env
        if stats["replay_env"] is None and g == games - 1:
            stats["replay_env"] = env
        print(".", end="", flush=True)
        if outcome == "forfeit" and sum(stats["forfeits"].values()) >= 3:
            print(f"\n  stopped after 3 forfeits — the platform quarantines a version after 3 consecutive timeouts")
            break
    print()
    w, l, d, f, v = tally["win"], tally["loss"], tally["draw"], tally["forfeit"], tally["void"]
    n = w + l + d + f
    lo, hi = wilson(w, n) if n else (0, 0)
    print(f"  {w}W / {l}L / {d}D" + (f" / {f} forfeit" if f else "") + (f" / {v} void" if v else "")
          + (f"   win rate {w / n * 100:.0f}%  (95% CI {lo * 100:.0f}–{hi * 100:.0f}%)" if n else ""))
    if reasons:
        print("  losses by (inferred):", dict(reasons.most_common()))
    if replay_path and stats["replay_env"] is not None:
        try:
            html = stats["replay_env"].render(mode="html")
            with open(replay_path, "w") as f:
                f.write(html)
            print(f"  replay of the first loss written to {replay_path}")
        except Exception as e:
            print(f"  (could not write replay: {e})")
    return stats


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bundle", required=True, help="your bundle.zip or its directory")
    p.add_argument("--games", type=int, default=30, help="games per opponent (default 30; use 200 to compare strategies)")
    p.add_argument("--opponent", choices=["both", "mirror", "reference"], default="both")
    p.add_argument("--skip-validation", action="store_true", help="play even if the card catalog cannot be fetched")
    p.add_argument("--competition", metavar="ID", help="validate against that competition's catalog instead of the platform default")
    p.add_argument("--replay", metavar="FILE.html", help="write an HTML replay of your first lost game (per opponent, suffixed)")
    args = p.parse_args()
    if args.games < 1:
        p.error("--games must be at least 1")

    try:
        import kaggle_environments
    except ImportError:
        raise SystemExit("The match engine is missing. Install it once:\n"
                         f"    pip install kaggle-environments=={ENGINE_VERSION}\n"
                         "That is the package the Arena runs; nothing else plays this card pool.")
    if getattr(kaggle_environments, "__version__", "?") != ENGINE_VERSION:
        print(f"WARNING kaggle-environments is {kaggle_environments.__version__}; the platform runs {ENGINE_VERSION}. "
              f"Rules can differ between builds.\n")

    root, cleanup = open_bundle(args.bundle)
    try:
        deck = load_manifest(root)
        try:
            by_id = fetch_catalog(args.competition)
        except Exception as e:
            if not args.skip_validation:
                raise SystemExit(f"could not fetch the card catalog ({e}). The platform validates against it, so this "
                                 f"tool will not guess. Retry, or pass --skip-validation to play unvalidated.")
            print(f"NOTE catalog unreachable ({e}); playing WITHOUT deck validation.\n")
            by_id = None
        if by_id is not None:
            rej, warn = validate_deck(deck, by_id)
            if rej:
                print("This deck would be REJECTED at submission:")
                for r in rej:
                    print(f"  - {r}")
                raise SystemExit("Fix manifest.joinPayload, then run again.")
            for w in warn:
                print(f"WARNING {w}")
            if warn:
                print()

        opponents = []
        if args.opponent in ("both", "mirror"):
            opponents.append(("mirror (your own deck, greedy play)", deck))
        if args.opponent in ("both", "reference"):
            opponents.append(("reference (the engine's sample deck, greedy play)", REFERENCE_DECK))

        all_stats = []
        for label, opp_deck in opponents:
            rp = None
            if args.replay:
                base, ext = os.path.splitext(args.replay)
                rp = f"{base}-{label.split()[0]}{ext or '.html'}"
            all_stats.append(run_series(root, deck, opp_deck, args.games, label, rp))
            print()

        # aggregate diagnostics
        forfeits = collections.Counter()
        first_error = None
        wall = []
        decisions = multi = first = truncated = 0
        for s in all_stats:
            forfeits.update(s["forfeits"]); wall += s["wall_ms"]
            decisions += s["decisions"]; multi += s["multi"]; first += s["first_choice"]; truncated += s["truncated"]
            first_error = first_error or s["first_error"]

        if forfeits:
            print("FATAL your bundle forfeited games:", dict(forfeits))
            print(f"  first: {first_error}")
            print("  On the platform each of these ends the match as a loss; three timeouts in a row quarantine the version.")
            print("  Fix this before you submit.\n")
        if wall:
            wall.sort()
            p95 = wall[int(len(wall) * 0.95) - 1] if len(wall) >= 20 else wall[-1]
            print(f"decision time: max {wall[-1]:.0f} ms, p95 {p95:.0f} ms, budget {DECISION_BUDGET_MS} ms per decision")
        if truncated:
            print(f"NOTE {truncated} decision(s) offered more than {MAX_LEGAL_ACTIONS} legal actions; the platform truncates "
                  f"the list, so later combinations are simply not offered")
        if multi and not forfeits:
            pct = first / multi * 100
            print(f"decisions with a real choice: {multi}, of which {pct:.0f}% took the first legal option")
            if pct >= 95:
                print("  WARNING that is what a fallback path looks like: on decisions where there was a choice, your code\n"
                      "  almost never chose differently from the greedy baseline. Something it needs probably failed to\n"
                      "  load and it is degrading silently. The platform will not report this either; it will just look\n"
                      "  like bad play. Check every import and any binary you ship (the sandbox is Linux).")
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


if __name__ == "__main__":
    main()
