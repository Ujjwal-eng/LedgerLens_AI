"""
Prints the routing stats — "X% routed to the low-cost path" — from
data/routing_log.jsonl, which accumulates every time extract_invoice()
runs on this machine.

Usage:
    python show_routing_stats.py                    # global stats (all users)
    python show_routing_stats.py --user-id <uid>    # single user only
"""

import argparse
from tools.routing_logger import compute_routing_stats

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Show LLM routing statistics.")
    parser.add_argument(
        "--user-id", dest="user_id", default=None,
        help="Filter stats to a single user_id (omit for global stats across all users)",
    )
    args = parser.parse_args()

    stats = compute_routing_stats(user_id=args.user_id)
    scope = f"user: {args.user_id}" if args.user_id else "all users"

    if stats.get("total", 0) == 0:
        print(f"No routing data yet for {scope} — process some invoices first.")
    else:
        print(f"Routing stats ({scope}):")
        print(f"  Total successful extractions: {stats['total']}")
        print(f"  Groq (low-cost path):         {stats['groq_count']} ({stats['groq_pct']:.1f}%)")
        print(f"  Gemini (vision path):         {stats['gemini_count']} ({stats['gemini_pct']:.1f}%)")
        print(f"  Required a fallback:          {stats['fallback_count']} ({stats['fallback_pct']:.1f}%)")