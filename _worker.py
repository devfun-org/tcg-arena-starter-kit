"""Strategy worker. Launched by playtest.py as `python -I -S _worker.py <bundle_root>`.

Runs with no site-packages and sys.path = [bundle_root], the way the platform's
sandbox does: anything the strategy imports must live inside the bundle.

Protocol: one JSON object per line on stdin (the context), one per line on stdout.
The strategy module is re-executed for every decision, because the platform does
that too — import-time state does not persist between decisions, and import time
is charged to the decision.
"""
import importlib.util
import json
import os
import sys
import time
import traceback


def main():
    root = os.path.abspath(sys.argv[1])
    strategy_path = os.path.join(root, "harness", "strategy.py")
    sys.path.insert(0, root)   # -I -S already dropped site-packages; keep the stdlib
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if request.get("op") == "exit":
            break
        t0 = time.perf_counter()
        try:
            # fresh module every decision, like the platform
            for name in list(sys.modules):
                m = sys.modules[name]
                f = getattr(m, "__file__", None) or ""
                if f and os.path.abspath(f).startswith(root):
                    del sys.modules[name]
            spec = importlib.util.spec_from_file_location("devfun_engine_strategy", strategy_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            import_ms = (time.perf_counter() - t0) * 1000
            fn = getattr(mod, "act", None) or getattr(mod, "choose_action", None)
            if not callable(fn):
                raise RuntimeError("strategy must define act(context) or choose_action(context)")
            t1 = time.perf_counter()
            result = fn(request["context"])
            compute_ms = (time.perf_counter() - t1) * 1000
            if isinstance(result, str):
                action = result
            elif isinstance(result, dict) and isinstance(result.get("action"), str):
                action = result["action"]
            else:
                raise TypeError(f"strategy result must be an action string or {{action: string}}, got {result!r}")
            out.write(json.dumps({"action": action, "importMs": import_ms, "computeMs": compute_ms}) + "\n")
        except BaseException as e:
            out.write(json.dumps({"error": f"{type(e).__name__}: {e}",
                                  "traceback": traceback.format_exc()[-1500:],
                                  "importMs": (time.perf_counter() - t0) * 1000}) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
