#!/usr/bin/env python3
"""Connect an explicitly selected Google account for Quill's large uploads."""
import argparse
import sys

from quill_workflow.google_drive_auth import authorize
from quill_workflow.pipeline import WorkflowError


def main():
    parser = argparse.ArgumentParser(description=(
        "Connect Quill's resumable uploader using your own Google OAuth Desktop client. "
        "Requests full Google Drive access for existing approved folders. "
        "Never borrows another application's token or opens a browser automatically."))
    parser.add_argument("--client", required=True, help="Downloaded Google OAuth Desktop client JSON path")
    parser.add_argument("--credentials", required=True, help="Absolute private output path for Quill's new credentials")
    parser.add_argument("--account", help="Require this exact Google account email after approval")
    parser.add_argument("--replace-account", action="store_true", help="Explicitly allow replacing a different connected Google account")
    parser.add_argument("--no-open", action="store_true", default=True,
                        help="Print the approval URL for you to open (default; browser never opens automatically)")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds to wait for Google approval (default: 300)")
    args = parser.parse_args()
    try:
        result = authorize(args.client, args.credentials, replace_account=args.replace_account,
                           expected_email=args.account, timeout=args.timeout,
                           announce=lambda message: print(message, flush=True))
        print("Connected Quill's large-file access for " + result["account_email"] + ". Credentials saved privately.")
        return 0
    except (WorkflowError, OSError):
        # Network/file error details can contain codes, bearer tokens, or payloads.
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, WorkflowError) else "Could not finish Google setup; nothing was uploaded.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Google setup cancelled; nothing was uploaded.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
