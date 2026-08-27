"""
Interactive CLI approval queue — the actual human-facing piece.

This is what test_human_loop.py was standing in for. Run this directly
on a real (or mocked) invoice; when the graph pauses at human_approval,
this script actually prompts YOU in the terminal, reads your answer,
and resumes the graph with it.

Usage:
    python approval_cli.py path/to/invoice.pdf
"""

import sys
import os
from dotenv import load_dotenv
from graph.supervisor_graph import build_graph
from langgraph.types import Command

load_dotenv()  # reads .env into os.environ — without this, SUPABASE_URL
                # etc. stay unset (or worse, hold whatever stale value was
                # in the shell), which is what caused the getaddrinfo error


def _get_supabase_client():
    """Real persistent memory if SUPABASE_URL/SUPABASE_KEY are set (from
    Phase 0); otherwise falls back to no memory at all, with a clear
    one-time warning so this doesn't fail silently. Every invoice will
    look like a first-time vendor in fallback mode — that's expected,
    not a bug, if you haven't set up Supabase yet."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("[warn] SUPABASE_URL/SUPABASE_KEY not set — running WITHOUT persistent "
              "vendor memory. Every invoice will show as first-time. See Phase 0/6.")
        return None
    from supabase import create_client
    return create_client(url, key)


def prompt_human(payload: dict) -> dict:
    print("\n" + "=" * 70)
    print("INVOICE NEEDS REVIEW")
    print("=" * 70)
    print(f"Vendor:   {payload['vendor']}")
    print(f"Invoice#: {payload['invoice_number']}")
    print(f"Amount:   Rs.{payload['amount']:,.2f}")
    print("Reasons flagged:")
    for r in payload["reasons"]:
        print(f"  - {r}")

    while True:
        action = input("\n[a]approve / [r]reject / [e]edit ? ").strip().lower()

        if action in ("a", "approve"):
            return {"action": "approve"}

        if action in ("r", "reject"):
            note = input("Reason for rejection: ").strip()
            return {"action": "reject", "note": note}

        if action in ("e", "edit"):
            print("Enter field=value pairs to correct, one per line. Blank line to finish.")
            print("  Top-level:  due_date=2026-08-16   |   amount=41000")
            print("  Line item:  line_items.0.rate=15000   (index starts at 0 — line #1 is 0)")
            print("  (editing a line's rate/quantity auto-updates that line's amount;")
            print("   the invoice's overall amount is auto-recalculated)")
            fields = {}
            while True:
                line = input("  edit> ").strip()
                if not line:
                    break
                if "=" not in line:
                    print("  (format is field=value, try again)")
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                # numeric top-level fields get cast to float here; line_items.*
                # fields are cast inside edit_invoice_node instead, since it
                # needs to know the specific sub-field (rate/qty vs description)
                if key == "amount":
                    try:
                        value = float(value)
                    except ValueError:
                        pass
                fields[key] = value
            return {"action": "edit", "fields": fields}

        print("Please enter 'a', 'r', or 'e'.")


def run(invoice_path: str, supabase_client=None):
    app = build_graph()
    config = {"configurable": {"thread_id": invoice_path, "supabase_client": supabase_client}}

    state = app.invoke({"invoice_path": invoice_path}, config=config)

    while "__interrupt__" in state:
        payload = state["__interrupt__"][0].value
        human_input = prompt_human(payload)
        state = app.invoke(Command(resume=human_input), config=config)

    decision = state["decision"]
    print("\n" + "=" * 70)
    print(f"FINAL DECISION: {decision['status'].upper()}")
    for r in decision["reasons"]:
        print(f"  - {r}")
    return decision


def run_batch(folder: str, supabase_client=None):
    """Walks every PDF in `folder` (recursively) and runs each one through
    the full graph + approval prompt, one at a time, so you can test your
    whole sample set in one sitting without re-invoking the script."""
    import glob

    pdf_paths = sorted(glob.glob(os.path.join(folder, "**", "*.pdf"), recursive=True))
    if not pdf_paths:
        print(f"No PDFs found under {folder}")
        return

    print(f"Found {len(pdf_paths)} invoice(s) to process.\n")
    results = []

    for i, path in enumerate(pdf_paths, 1):
        print(f"\n\n########## [{i}/{len(pdf_paths)}] {path} ##########")
        try:
            decision = run(path, supabase_client=supabase_client)
            results.append((path, decision["status"]))
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as e:
            print(f"  ERROR processing {path}: {e}")
            results.append((path, f"error: {e}"))

    print("\n\n" + "=" * 70)
    print("BATCH SUMMARY")
    print("=" * 70)
    for path, status in results:
        print(f"  {status.upper():20s} {os.path.basename(path)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python approval_cli.py path/to/invoice.pdf        (single file)")
        print("  python approval_cli.py path/to/folder/            (batch mode — all PDFs inside)")
        sys.exit(1)

    client = _get_supabase_client()

    target = sys.argv[1]
    if os.path.isdir(target):
        run_batch(target, supabase_client=client)
    else:
        run(target, supabase_client=client)