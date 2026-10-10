"""Run the evaluation set against the live assistant and write a scorecard.

  python -m scripts.run_eval                 # all cases
  python -m scripts.run_eval c-refund-v1 a-  # only ids starting with these prefixes

Writes eval/results.json (full answers) and eval/report.md (scorecard).
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict

import yaml

from app.agent.loop import Assistant
from app.config import ROOT
from app.rag.index import ensure_index
from scripts.build_derived import ensure_built

EVAL_FILE = ROOT / "eval" / "questions.yaml"


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[‐-―−]", "-", s)               # unicode hyphens/dashes -> '-'
    s = re.sub(r"[    ]", " ", s)         # unusual spaces -> ' '
    s = re.sub(r"[‘’ʼ]", "'", s)                # curly apostrophes -> "'"
    return s.replace("₹", "").replace(",", "").replace("**", "")


def check(case: dict, resp) -> list[str]:
    e, ans, used = case.get("expect", {}), norm(resp.answer), {t.name for t in resp.tool_calls}
    fails = []
    fails += [f"missing '{x}'" for x in e.get("all", []) if norm(str(x)) not in ans]
    if e.get("any") and not any(norm(str(x)) in ans for x in e["any"]):
        fails.append(f"none of {e['any']}")
    fails += [f"contains '{x}'" for x in e.get("none", []) if norm(str(x)) in ans]
    fails += [f"tool {t} not called" for t in e.get("tools", []) if t not in used]
    fails += [f"tool {t} called" for t in e.get("no_tools", []) if t in used]
    if "escalate" in e and resp.escalate != e["escalate"]:
        fails.append(f"escalate={resp.escalate}")
    if "status" in e and resp.status != e["status"]:
        fails.append(f"status={resp.status}")
    if "policy_source" in e and not any(s.type == "policy" and e["policy_source"] in s.ref for s in resp.sources):
        fails.append(f"no policy source {e['policy_source']}")
    if resp.status == "error":
        fails.append("LLM error")
    return fails


def main() -> None:
    prefixes = sys.argv[1:]
    cases = [c for c in yaml.safe_load(EVAL_FILE.read_text(encoding="utf-8"))
             if not prefixes or any(c["id"].startswith(p) for p in prefixes)]
    ensure_built()
    ensure_index()
    results, t_start = [], time.time()
    for c in cases:
        bot = Assistant(c["role"], c.get("customer_id"))
        resp = None
        for turn in c["turns"]:
            resp = bot.ask(turn)
        fails = check(c, resp)
        results.append({"id": c["id"], "category": c["category"], "role": c["role"], "turns": c["turns"],
                        "passed": not fails, "fails": fails, "latency_ms": resp.latency_ms,
                        "tools": [t.name for t in resp.tool_calls], "escalate": resp.escalate,
                        "answer": resp.answer})
        print(f"{'PASS' if not fails else 'FAIL'}  {c['id']:24} {resp.latency_ms:>6} ms  {'; '.join(fails)}", flush=True)
        bot.close()

    by_cat = defaultdict(lambda: [0, 0])
    for r in results:
        by_cat[r["category"]][0] += r["passed"]
        by_cat[r["category"]][1] += 1
    passed = sum(r["passed"] for r in results)
    lat = sorted(r["latency_ms"] for r in results)
    lines = [f"# Evaluation scorecard\n", f"**{passed}/{len(results)} passed** · median latency "
             f"{lat[len(lat) // 2] / 1000:.1f} s · p90 {lat[int(len(lat) * 0.9)] / 1000:.1f} s · "
             f"run took {time.time() - t_start:.0f} s\n", "| Category | Passed |", "|---|---|"]
    lines += [f"| {k} | {v[0]}/{v[1]} |" for k, v in sorted(by_cat.items())]
    lines += ["", "| Case | Result | Notes |", "|---|---|---|"]
    lines += [f"| {r['id']} | {'✅' if r['passed'] else '❌'} | {'; '.join(r['fails'])} |" for r in results]
    (ROOT / "eval" / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (ROOT / "eval" / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{passed}/{len(results)} passed. Report: eval/report.md")


if __name__ == "__main__":
    main()
