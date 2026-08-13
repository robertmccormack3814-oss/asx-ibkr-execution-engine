from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
old = '''    status_ok = all(s["status"] in acceptable_statuses for s in snapshots)
    no_hold_reason = all(not s["why_held"] for s in snapshots)
    no_errors = all(
        not s["advanced_error"]
        and all(not row["error_code"] for row in s["log"])
        for s in snapshots
    )
    return structure_ok and status_ok and no_hold_reason and no_errors, snapshots
'''
new = '''    status_ok = all(s["status"] in acceptable_statuses for s in snapshots)

    # IBKR normally marks attached bracket children as held until the parent
    # fills.  These are expected protective states, not acknowledgement
    # failures.  The parent itself should not be held.
    parent_hold_ok = not snapshots[0]["why_held"]
    child_holds_ok = all(
        not s["why_held"]
        or all(part.strip() in {"child", "trigger"} for part in s["why_held"].split(","))
        for s in snapshots[1:]
    )

    no_errors = all(
        not s["advanced_error"]
        and all(not row["error_code"] for row in s["log"])
        for s in snapshots
    )
    return structure_ok and status_ok and parent_hold_ok and child_holds_ok and no_errors, snapshots
'''
if old not in text:
    raise SystemExit("Patch target not found; engine.py may already be patched or changed.")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("engine.py patched successfully")
