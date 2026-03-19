"""Run a demigod from the command line.

Usage:
    python -m gods.roles.run_demigod health_checker
    python -m gods.roles.run_demigod health_checker --provider claude
    python -m gods.roles.run_demigod health_checker --provider ollama --model qwen3:4b
    python -m gods.roles.run_demigod health_checker --gateway http://localhost:5210
"""

import argparse
import asyncio
import json
import logging
import sys

from gods.demigod import run_demigod, LLMConfig, LLM_GATEWAY_URL, HADES_URL
from gods.roles.health_checker import (
    build_health_checker_role,
    build_batch_health_checker_role,
    default_llm,
)

ROLES = {
    "health_checker": (build_health_checker_role, default_llm),
    "batch_health_checker": (build_batch_health_checker_role, default_llm),
}


def main():
    parser = argparse.ArgumentParser(description="Run a jailed demigod")
    parser.add_argument("role", choices=list(ROLES.keys()), help="Role to run")
    parser.add_argument("--provider", default=None,
                        help="LLM provider: claude, gemini, ollama (default: role's default)")
    parser.add_argument("--model", default=None, help="Model override (provider-specific)")
    parser.add_argument("--gateway", default=LLM_GATEWAY_URL, help="LLM Gateway URL")
    parser.add_argument("--hades", default=HADES_URL, help="Hades admin URL")
    parser.add_argument("--params", default="{}", help="Initial params as JSON string")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    builder, llm_factory = ROLES[args.role]
    role = builder()
    llm = llm_factory()

    if args.provider:
        llm.provider = args.provider
    if args.model:
        llm.model = args.model

    try:
        initial_params = json.loads(args.params)
    except json.JSONDecodeError:
        print(f"ERROR: Invalid --params JSON: {args.params}", file=sys.stderr)
        sys.exit(1)

    result = asyncio.run(run_demigod(
        role, llm,
        hades_url=args.hades,
        gateway_url=args.gateway,
        initial_params=initial_params or None,
    ))

    # Output
    print()
    print("=" * 60)
    print(f"  DEMIGOD:  {result.role}")
    print(f"  SUCCESS:  {result.success}")
    print(f"  STEPS:    {result.steps}")
    print(f"  TIME:     {result.duration_ms:.0f}ms")
    print(f"  STATE:    {result.final_state}")
    if result.error:
        print(f"  ERROR:    {result.error}")
    print("=" * 60)

    if result.history:
        print("\nHISTORY:")
        for i, entry in enumerate(result.history, 1):
            state = entry.get("state", "?")
            action = entry.get("action", "-")
            param = entry.get("param")
            terminal = entry.get("terminal", False)
            error = entry.get("error")

            line = f"  {i}. [{state}] -> {action}"
            if param:
                line += f":{param}"
            if terminal:
                line += " (terminal)"
            if error:
                line += f" ERROR: {error}"
            print(line)

            summary = entry.get("result_summary")
            if summary and summary != "(no result)":
                print(f"     {summary}")

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
