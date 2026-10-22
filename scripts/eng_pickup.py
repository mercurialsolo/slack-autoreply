#!/usr/bin/env python3
"""Take one engineering issue Solo filed and hand it to Claude Code, which answers with a draft PR.

Runs on the node from cron, one issue per run, oldest first:
  */10 * * * * ~/.slack-autoreply/venv/bin/python3 ~/.claude/skills/slack-autoreply/scripts/eng_pickup.py >> ~/.slack-autoreply/eng_pickup.log 2>&1

The queue is the ENG_LABEL label on ENG_REPO. While an agent has an issue it carries
`agent:working`; afterwards `agent:done` (a draft PR exists) or `agent:needs-human` (it came back
without one — its comment on the issue says why). Either way Solo's bot says so in the Slack
thread the request came from.

The agent works in its own clone (ENG_REPO_DIR), never in a checkout a person uses, and in a
fresh worktree per issue. It never merges: the brief says so, and the default branch must be
protected so that it cannot — a prompt is not a boundary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

WORKING, DONE, NEEDS_HUMAN = "agent:working", "agent:done", "agent:needs-human"
TAKEN = {WORKING, DONE, NEEDS_HUMAN}

REPO = config.env("ENG_REPO")
LABEL = config.env("ENG_LABEL", "agent")
GH = config.env("GH_BIN", "gh")
CLAUDE = config.env("CLAUDE_BIN", "claude")
REPO_DIR = Path(config.env("ENG_REPO_DIR", f"~/agent-work/{REPO.rsplit('/', 1)[-1]}")).expanduser()
WORK_DIR = Path(config.env("ENG_WORK_DIR", "~/agent-work/worktrees")).expanduser()
MAX_TURNS = config.env("ENG_MAX_TURNS", "150")
TIMEOUT = int(config.env("ENG_TIMEOUT", "5400"))
LOCK = config.STATE_DIR / "eng_pickup.lock"

BRIEF = """You are working on GitHub issue #{number} in {repo}: {title}
{url}

{body}

You are in a fresh git worktree on branch `{branch}`, cut from the default branch.

1. Read the issue and the code it touches. If the request is unclear, too big for one PR, or not a
   code change, do not guess: comment on the issue with what you found and what you need
   (`gh issue comment {number} --body "..."`) and stop there.
2. Otherwise make the smallest change that does what was asked, following the repo's CLAUDE.md /
   AGENTS.md conventions, with tests where the repo has them. Run the tests that cover it.
3. Commit, push the branch (`git push -u origin {branch}`) and open a DRAFT pull request:
     gh pr create --draft --title "<what changed>" --body "Closes #{number}

     <what changed, and how you tested it>"
4. Comment on the issue with the PR link.

Never merge, never push to the default branch, never mark the PR ready for review: a person
reviews and merges. Do not post to Slack — Solo reports your PR in the thread the request came from."""


def pick(issues: list[dict]) -> dict | None:
    """The oldest open issue no agent has touched yet."""
    free = [i for i in issues if not {lab["name"] for lab in i.get("labels", [])} & TAKEN]
    return min(free, key=lambda i: i["createdAt"]) if free else None


def brief(issue: dict, branch: str) -> str:
    return BRIEF.format(repo=REPO, branch=branch, number=issue["number"], title=issue["title"],
                        url=issue.get("url", ""), body=(issue.get("body") or "").strip())


def run(cmd: list[str], **kw) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(out.stderr or out.stdout).strip()[:300]}")
    return out.stdout


def relabel(number: int, add: str, remove: str | None = None) -> None:
    cmd = [GH, "issue", "edit", str(number), "--repo", REPO, "--add-label", add]
    run(cmd + (["--remove-label", remove] if remove else []))


def report(number: int, text: str) -> None:
    """Answer in the Slack thread, as Solo's bot. A failed report is logged, never fatal."""
    out = subprocess.run([sys.executable, str(Path(__file__).with_name("slackctl.py")), "eng-report", str(number), text],
                         capture_output=True, text=True)
    print(out.stdout.strip() or out.stderr.strip())


def worktree(branch: str, number: int) -> Path:
    if not (REPO_DIR / ".git").exists():
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        run([GH, "repo", "clone", REPO, str(REPO_DIR)])
    git = ["git", "-C", str(REPO_DIR)]
    run(git + ["fetch", "origin", "--prune"])
    base = run(git + ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]).strip() or "origin/main"
    path = WORK_DIR / f"issue-{number}"
    if path.exists():
        run(git + ["worktree", "remove", "--force", str(path)])
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    run(git + ["worktree", "add", "-B", branch, str(path), base])
    return path


def work(issue: dict) -> None:
    number, branch = issue["number"], f"agent/issue-{issue['number']}"
    relabel(number, WORKING)
    print(f"#{number}: {issue['title']}")
    try:
        path = worktree(branch, number)
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)   # bill the Claude Code login, not the API key
        subprocess.run([CLAUDE, "-p", brief(issue, branch), "--permission-mode", "acceptEdits",
                        "--allowedTools", "Bash", "Read", "Edit", "Write", "Glob", "Grep",
                        "--disallowedTools", "Bash(gh pr merge:*)", "Bash(gh pr ready:*)",
                        "--max-turns", MAX_TURNS, "--output-format", "json"],
                       cwd=path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)
        prs = json.loads(run([GH, "pr", "list", "--repo", REPO, "--head", branch, "--state", "open",
                              "--json", "number,url"]) or "[]")
    except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as e:
        relabel(number, NEEDS_HUMAN, WORKING)
        report(number, f"The agent couldn't work on #{number}: {str(e)[:300]}\n{issue.get('url', '')}")
        raise
    if prs:
        relabel(number, DONE, WORKING)
        report(number, f"Draft PR for #{number} is up: {prs[0]['url']}\nA person still reviews and merges it.")
    else:
        relabel(number, NEEDS_HUMAN, WORKING)
        report(number, f"The agent looked at #{number} and didn't open a PR; its comment on the issue says why.\n{issue.get('url', '')}")


def main() -> int:
    if not REPO:
        sys.exit("ENG_REPO is not set in ~/.slack-autoreply/.env")
    try:
        LOCK.mkdir()   # no flock on macOS; a run still going holds the directory
    except FileExistsError:
        print("another pickup is running")
        return 0
    try:
        for name in TAKEN:
            run([GH, "label", "create", name, "--repo", REPO, "--force", "--color", "BFD4F2"])
        issues = json.loads(run([GH, "issue", "list", "--repo", REPO, "--label", LABEL, "--state", "open",
                                 "--limit", "50", "--json", "number,title,body,url,createdAt,labels"]) or "[]")
        issue = pick(issues)
        if issue:
            work(issue)
        return 0
    finally:
        LOCK.rmdir()


if __name__ == "__main__":
    sys.exit(main())
