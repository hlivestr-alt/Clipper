from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _request(
    base_url: str,
    token: str,
    method: str,
    path: str,
    payload: dict | None = None,
) -> dict:
    body = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Clipper API returned HTTP {exc.code}: {detail}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Administer WhatsApp delivery through the authenticated Clipper API."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("CLIPPER_API_URL", "http://127.0.0.1:8765"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")

    claim = subparsers.add_parser("claim")
    claim.add_argument("--affiliate-name", required=True)
    claim.add_argument("--affiliate-identifier", required=True)
    claim.add_argument("--idempotency-key", required=True)
    claim.add_argument("--sheet-row")
    claim.add_argument("--batch", type=int)

    for action in ("start", "sent", "fail", "cancel", "release", "retry"):
        transition = subparsers.add_parser(action)
        transition.add_argument("assignment_id")
        transition.add_argument("--version", type=int, required=True)
        transition.add_argument("--idempotency-key", required=True)
        transition.add_argument("--error")
        transition.add_argument("--reason")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.getenv("CLIPPER_CONTROL_TOKEN", "").strip()
    if not token:
        print("CLIPPER_CONTROL_TOKEN is required", file=sys.stderr)
        return 2
    if args.command == "status":
        response = _request(
            args.base_url, token, "GET", "/api/whatsapp-delivery/status"
        )
    elif args.command == "claim":
        response = _request(
            args.base_url,
            token,
            "POST",
            "/api/whatsapp-delivery/claims",
            {
                "affiliate_name": args.affiliate_name,
                "affiliate_identifier": args.affiliate_identifier,
                "idempotency_key": args.idempotency_key,
                "campaign_or_sheet_row_identifier": args.sheet_row,
                "requested_batch": args.batch,
            },
        )
    else:
        response = _request(
            args.base_url,
            token,
            "POST",
            f"/api/whatsapp-delivery/assignments/{args.assignment_id}/{args.command}",
            {
                "expected_version": args.version,
                "idempotency_key": args.idempotency_key,
                "error": args.error,
                "operator_reason": args.reason,
            },
        )
    print(json.dumps(response, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
