#!/usr/bin/env python3

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

import httpx

# Define the API endpoint
API_URL = "http://localhost:5200/api/sentinel/status"


def resolve_token(args: argparse.Namespace) -> str:
    """Resolves the authentication token from command-line arguments or environment variables."""
    if args.token:
        return args.token
    token = os.environ.get("ORCH_TOKEN")
    if not token:
        print("Error: ORCH_TOKEN environment variable not set and --token flag not provided.", file=sys.stderr)
        sys.exit(1)
    return token


async def get_sentinel_status(token: str) -> Optional[Dict[str, Any]]:
    """
    Makes an authenticated GET request to the sentinel status endpoint.
    Handles connection errors gracefully.
    """
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(API_URL, headers=headers)
            response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
            return response.json()
    except httpx.ConnectError as exc:
        print(f"Connection error: Unable to connect to {exc.request.url!r}. Please ensure the orchestrator is running.", file=sys.stderr)
        return None
    except httpx.RequestError as exc:
        print(f"An error occurred while requesting {exc.request.url!r}: {exc}", file=sys.stderr)
        return None
    except httpx.HTTPStatusError as exc:
        print(f"Error response {exc.response.status_code} while requesting {exc.request.url!r}: {exc.response.text}", file=sys.stderr)
        return None


def pretty_print_status(status_data: Dict[str, Any]):
    """Formats and prints the sentinel status data in a human-readable format."""
    print("Sentinel Health Status")
    print("======================")

    trends: List[Dict[str, Any]] = status_data.get("health_trends", [])
    if not trends:
        print("No health trend data available.")
    else:
        # Determine column widths
        max_service_len = max(len(t.get("service_name", "N/A")) for t in trends) if trends else 12
        
        # Header
        print(f"{'Service':<{max_service_len}} | {'State':<10} | {'Latency (ms)':>12}")
        print(f"{'-' * max_service_len}-|------------|---------------")

        # Rows
        for trend in trends:
            service = trend.get("service_name", "N/A")
            state = trend.get("state", "UNKNOWN")
            latency = trend.get("latency_ms", -1)
            latency_str = f"{latency:.2f}" if latency != -1 else "N/A"
            print(f"{service:<{max_service_len}} | {state:<10} | {latency_str:>12}")

    print("\n" + "="*22)
    active_plans = status_data.get("active_plan_sentinels", 0)
    print(f"Active Plan Sentinels: {active_plans}")
    print("="*22)


def determine_exit_code(status_data: Dict[str, Any]) -> int:
    """
    Determines the exit code based on sentinel health.
    Returns 0 if all services are healthy, 1 otherwise.
    """
    trends: List[Dict[str, Any]] = status_data.get("health_trends", [])
    if not trends:
        return 1  # No data is considered a failure state

    for trend in trends:
        if trend.get("state", "UNKNOWN").lower() != "healthy":
            return 1
    return 0


def main():
    """Main function to parse arguments, fetch and display sentinel health."""
    parser = argparse.ArgumentParser(description="Check sentinel health status.")
    parser.add_argument(
        "--json", action="store_true", help="Output health status in JSON format."
    )
    parser.add_argument(
        "--token", help="Authentication token for the orchestration API. Overrides ORCH_TOKEN env var."
    )

    args = parser.parse_args()
    token = resolve_token(args)

    status_data = asyncio.run(get_sentinel_status(token))

    if status_data is None:
        sys.exit(1)

    if args.json:
        print(json.dumps(status_data, indent=2))
    else:
        pretty_print_status(status_data)

    exit_code = determine_exit_code(status_data)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
