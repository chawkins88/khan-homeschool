from __future__ import annotations

import json
from app.services.khan_graphql import KhanGraphQLClient, load_saved_operations


def main() -> None:
    client = KhanGraphQLClient()
    ops = load_saved_operations()
    if not ops:
        raise SystemExit("No saved operations found. Run khan_capture_session.py first.")

    results = []
    for op in ops:
        try:
            payload = client.replay_operation(op)
            results.append({
                "operationName": op.operation_name,
                "ok": True,
                "keys": list(payload.keys()) if isinstance(payload, dict) else [],
            })
        except Exception as exc:
            results.append({
                "operationName": op.operation_name,
                "ok": False,
                "error": str(exc),
            })

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
