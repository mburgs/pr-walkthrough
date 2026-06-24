"""CLI entry point for the PR I/O stream.

Usage:
  python -m pr_walkthrough.pr.cli fetch <pr-url>
  python -m pr_walkthrough.pr.cli comment <pr-url> --body TEXT
      [--file PATH --line N [--end-line N]] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from contracts.schemas import CodeAnchor

from .gh_source import GhPRSource


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pr_walkthrough.pr.cli",
        description="PR I/O: fetch diff or post comments via the gh CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- fetch ---
    fetch_p = sub.add_parser("fetch", help="Fetch PR metadata + diff hunks.")
    fetch_p.add_argument("pr_url", help="GitHub PR URL")

    # --- comment ---
    comment_p = sub.add_parser("comment", help="Post a comment on a PR.")
    comment_p.add_argument("pr_url", help="GitHub PR URL")
    comment_p.add_argument("--body", required=True, help="Comment text")
    comment_p.add_argument(
        "--file", dest="file", default=None,
        help="File path for inline comment anchor"
    )
    comment_p.add_argument(
        "--line", type=int, default=None,
        help="Line number for inline comment (start line)"
    )
    comment_p.add_argument(
        "--end-line", type=int, default=None,
        help="End line number for multi-line inline comment (defaults to --line)"
    )
    comment_p.add_argument(
        "--dry-run", action="store_true",
        help="Print the gh args that would be used without executing"
    )

    return parser


def _build_dry_run_description(
    pr_url: str,
    body: str,
    anchor: CodeAnchor | None,
) -> str:
    if anchor is None:
        return (
            "Would run: gh pr comment {url} --body {body!r}".format(
                url=pr_url, body=body
            )
        )
    start, end = anchor.line_range
    lines_info = f"--line {end}" if start == end else f"--start-line {start} --line {end}"
    return (
        f"Would run: gh api repos/{{owner}}/{{repo}}/pulls/{{number}}/comments "
        f"[POST] path={anchor.file!r} {lines_info} body={body!r}"
    )


async def _cmd_fetch(args: argparse.Namespace) -> None:
    source = GhPRSource()
    metadata, hunks = await source.fetch(args.pr_url)
    output = {
        "metadata": metadata.model_dump(),
        "diff": [h.model_dump() for h in hunks],
    }
    print(json.dumps(output, indent=2))


async def _cmd_comment(args: argparse.Namespace) -> None:
    anchor: CodeAnchor | None = None
    if args.file and args.line is not None:
        end = args.end_line if args.end_line is not None else args.line
        anchor = CodeAnchor(file=args.file, line_range=(args.line, end))

    if args.dry_run:
        print(_build_dry_run_description(args.pr_url, args.body, anchor))
        return

    source = GhPRSource()
    url = await source.post_comment(args.pr_url, args.body, anchor)
    print(url)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.command == "fetch":
            asyncio.run(_cmd_fetch(args))
        elif args.command == "comment":
            asyncio.run(_cmd_comment(args))
        else:
            parser.print_help()
            sys.exit(1)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
