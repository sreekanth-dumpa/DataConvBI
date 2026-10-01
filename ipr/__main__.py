"""Command-line demo: ``python -m ipr demo`` runs the full workflow once."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .service import IPRService

DEMO_QUESTION = ("What was the 30-day quote conversion for NC personal auto quotes issued in August 2026 "
                 "by product version and channel, as of 2026-09-15?")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ipr")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run interpret -> ... -> present -> assess once")
    d.add_argument("--home", default=".ipr_data")
    d.add_argument("--question", default=DEMO_QUESTION)
    d.add_argument("--subject", default="analyst_nc")
    d.add_argument("--variant", choices=["fused", "staged", "partitioned"])
    d.add_argument("--compare-variants", action="store_true")
    d.add_argument("--provider", choices=["offline", "claude", "replay"], help="agent skills provider (default: env / offline)")
    m = sub.add_parser("monitor", help="print the monitoring snapshot")
    m.add_argument("--home", default=".ipr_data")
    args = ap.parse_args(argv)
    provider = None
    if getattr(args, "provider", None) == "claude":
        from .agents.providers import ClaudeProvider
        provider = ClaudeProvider()
    elif getattr(args, "provider", None) == "offline":
        from .agents.providers import OfflineProvider
        provider = OfflineProvider()
    svc = IPRService(Settings(home=Path(args.home).resolve()), llm_provider=provider)
    try:
        if args.cmd == "monitor":
            print(json.dumps(svc.monitoring(), indent=2, default=str))
            return 0
        out = svc.ask(args.question, {"subject": args.subject}, variant=args.variant)
        if out.get("status") not in ("succeeded", None) and "narrative" not in out:
            print(json.dumps(out, indent=2, default=str))
            return 1
        print(f"[{out['stage']}] origin={out.get('origin')} execution={out.get('execution_id')}")
        nar = out.get("agent_narrative") or {}
        if nar.get("headline"):
            ag = nar["agent"]
            print(f"\n[answer-narrator via {ag['provider']}{' (fallback)' if ag['fallback_used'] else ''}] {nar['headline']}")
            print(nar["narrative"], "\n")
        print(out["narrative"])
        print("\nAgent skills used:", ", ".join(sorted({r['skill'] for r in svc.agents.invocations(request_id=out['request_id'])})))
        if out.get("execution_id"):
            a = svc.assess(out["execution_id"], compare_variants=args.compare_variants)
            print("\nGates:", a["case"]["gates"])
            print("Assessment written to", a["path"])
        return 0
    finally:
        svc.close()


if __name__ == "__main__":
    sys.exit(main())
