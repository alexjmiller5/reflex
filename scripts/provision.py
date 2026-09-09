# /// script
# requires-python = ">=3.12"
# dependencies = ["modal>=1.0,<2"]
# ///
"""Mint a dedicated Modal token pair in memory for op-project-bootstrap.

The operator opens the stderr URL in the configured remote browser session
and approves the displayed code. Only the verified pair goes to stdout;
bootstrap saves both fields to 1Password together through JSON stdin.
"""

import json
import sys

PROJECT = "notion-automations"
FIELDS = ("token-id", "token-secret")
MAX_ATTEMPTS = 15


def mint() -> dict[str, str]:
    from modal.client import Client
    from modal.config import DEFAULT_SERVER_URL
    from modal.token_flow import TokenFlow

    with Client.anonymous(DEFAULT_SERVER_URL) as client:
        flow = TokenFlow(client)
        with flow.start() as (_, url, code):
            print(
                f"Approve a dedicated {PROJECT} CI token in the remote browser:\n{url}",
                file=sys.stderr,
            )
            print(f"Verification code: {code}", file=sys.stderr)
            for _ in range(MAX_ATTEMPTS):
                result = flow.finish(timeout=40)
                if result is not None:
                    break
            else:
                raise RuntimeError("Modal approval timed out")
    if not result.token_id.strip() or not result.token_secret.strip():
        raise RuntimeError("Modal returned an incomplete token pair")
    Client.verify(DEFAULT_SERVER_URL, (result.token_id, result.token_secret))
    return dict(zip(FIELDS, (result.token_id, result.token_secret), strict=True))


def main() -> None:
    match sys.argv[1:]:
        case ["--list"]:
            print("\n".join(FIELDS))
        case ["--batches"]:
            print(json.dumps({"modal-token": FIELDS}))
        case ["--batch", "modal-token"]:
            try:
                pair = mint()
            except Exception:
                # Provider errors can contain credentials. Never echo their payload.
                sys.exit("Modal token mint or verification failed; no credentials emitted")
            print(json.dumps(pair))
        case _:
            sys.exit(
                "usage: provision.py --list | --batches | --batch modal-token (pair fields are atomic)"
            )


if __name__ == "__main__":
    main()
