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
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import linear  # noqa: E402

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
TEST_LABEL = config.env("ENG_TEST_LABEL", "sr:test")
SETTLE = timedelta(minutes=int(config.env("ENG_SETTLE_MINUTES", "15")))
LOCK = config.STATE_DIR / "eng_pickup.lock"
# Linear is optional. With a team key, the agent opens the Linear issue itself before any code, and
# names its branch from it so Linear's GitHub app attaches the PR and moves the issue. The MCP
# config is passed explicitly because the owner's Linear MCP is registered for their own checkout,
# not for the agent's clone; the OAuth login it already holds is reused.
LINEAR_TEAM = config.env("ENG_LINEAR_TEAM")
MCP_CONFIG = Path(config.env("ENG_MCP_CONFIG", str(config.STATE_DIR / "eng-mcp.json"))).expanduser()

BRIEF = """You are working on GitHub issue #{number} in {repo}: {title}
{url}

{body}

Read the comments too (`gh issue view {number} --comments`): the Slack thread's replies are copied
there, and they usually carry the detail the title leaves out.

You are in a fresh git worktree on branch `{branch}`, cut from the default branch.
{linear}
1. Read the issue and the code it touches. If the request is unclear, too big for one PR, or not a
   code change, do not guess: comment on the issue with what you found and what you need
   (`gh issue comment {number} --body "..."`) and stop there.
2. Otherwise make the smallest change that does what was asked, following the repo's CLAUDE.md /
   AGENTS.md conventions, with tests where the repo has them. Run the tests that cover it.
3. Commit, push the branch (`git push -u origin {branch}`) and open a DRAFT pull request:
     gh pr create --draft --title "<what changed>" --body "Closes #{number}

     <what changed, and how you tested it>"
4. Comment on the issue with the PR link.{linear_after}

Never merge, never push to the default branch, never mark the PR ready for review: a person
reviews and merges. Do not post to Slack — Solo reports your PR in the thread the request came from."""


def pick(issues: list[dict], now: datetime | None = None) -> dict | None:
    """The oldest open issue no agent has touched, once it is ENG_SETTLE_MINUTES old.

    An SR's title is a few words and the detail arrives as thread replies, which Solo copies onto
    the issue. Starting the moment it is filed means reading the title and guessing."""
    now = now or datetime.now(timezone.utc)
    free = [i for i in issues if not {lab["name"] for lab in i.get("labels", [])} & TAKEN
            and now - datetime.fromisoformat(i["createdAt"].replace("Z", "+00:00")) >= SETTLE]
    return min(free, key=lambda i: i["createdAt"]) if free else None


LINEAR_FIRST = """
0. Before anything else, open a Linear issue in team {team} with the Linear MCP: the same title,
   and a description linking {url} and the Slack request quoted in it. Then:
   - comment on the GitHub issue: `gh issue comment {number} --body "Tracked in [KEY](<linear url>)"`
   - rename this branch to the Linear issue's `gitBranchName` (`git branch -m <gitBranchName>`) and
     use that name wherever this brief says `{branch}`. That is what Linear's GitHub app matches to
     attach the PR and move the issue.
   - in the PR body, after the closing line, add `Refs [KEY](<linear url>)`.
   If the Linear MCP is unavailable, say so in a GitHub issue comment and carry on without it.
"""
LINEAR_LINKED = """
This request is tracked in Linear as {identifier} ({url}), and this branch is already named for it,
which is how Linear's GitHub app attaches the PR. Do not open another Linear issue. In the PR body,
after the closing line, add `Refs [{identifier}]({url})`.
"""
LINEAR_AFTER = """
5. Comment on the Linear issue with what you did and the PR link — or, if you stopped at step 1,
   with what you found and what you need."""


def brief(issue: dict, branch: str, linear_team: str = "", linked: dict | None = None) -> str:
    """With `linked`, Solo already opened the Linear issue; the pickup comments on it itself.
    Without, and with a team, the agent opens one first through the Linear MCP."""
    url = issue.get("url", "")
    if linked:
        section, after = LINEAR_LINKED.format(**linked), ""
    elif linear_team:
        section = LINEAR_FIRST.format(team=linear_team, url=url, number=issue["number"], branch=branch)
        after = LINEAR_AFTER
    else:
        section, after = "", ""
    return BRIEF.format(repo=REPO, branch=branch, number=issue["number"], title=issue["title"], url=url,
                        body=(issue.get("body") or "").strip(), linear=section, linear_after=after)


def linked(issue: dict) -> dict | None:
    """The Linear issue and branch Solo wrote into the GitHub issue when it filed both."""
    body = issue.get("body") or ""
    lin = re.search(r"^Linear: \[([A-Z]+-\d+)\]\((\S+?)\)\s*$", body, re.M)
    branch = re.search(r"^Branch: `([^`]+)`\s*$", body, re.M)
    if not lin or not branch:
        return None
    return {"identifier": lin.group(1), "url": lin.group(2), "branch": branch.group(1)}


def linear_url(comments: list[dict]) -> str | None:
    """The Linear issue the agent opened, from its comment on the GitHub issue."""
    for c in comments:
        m = re.search(r"https://linear\.app/[^\s)>\]]+/issue/[^\s)>\]]+", c.get("body") or "")
        if m:
            return m.group(0)
    return None


def is_test(issue: dict) -> bool:
    return TEST_LABEL in {lab["name"] for lab in issue.get("labels", [])}


def close_test(number: int, prs: list[dict], linked: dict | None = None) -> None:
    """A test SR has done its job once the agent has reported: close it, its draft PR and its
    Linear issue, so trying the pipeline leaves nothing open. All reopen with one click; the
    branch stays."""
    note = "Test request: closed automatically after the agent reported."
    for pr in prs:
        run([GH, "pr", "close", str(pr["number"]), "--repo", REPO, "--comment", note])
    run([GH, "issue", "close", str(number), "--repo", REPO, "--reason", "not planned", "--comment", note])
    if linked and linear.enabled():
        try:
            linear.cancel(linked["identifier"])
        except (RuntimeError, OSError) as e:
            print(f"could not cancel {linked['identifier']}: {e}")


def linear_note(link: dict | None, text: str) -> None:
    """Tell the Linear issue how it went. Optional: a machine without LINEAR_API_KEY skips it, and
    Linear's GitHub app still attaches the PR by its branch name."""
    if not link or not linear.enabled():
        return
    try:
        linear.comment(link["identifier"], text)
    except (RuntimeError, OSError) as e:
        print(f"could not comment on {link['identifier']}: {e}")


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
    number, link = issue["number"], linked(issue)
    branch = link["branch"] if link else f"agent/issue-{issue['number']}"
    relabel(number, WORKING)
    print(f"#{number}: {issue['title']}")
    try:
        path = worktree(branch, number)
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)   # bill the Claude Code login, not the API key
        team = LINEAR_TEAM if MCP_CONFIG.exists() and not link else ""
        tools = ["Bash", "Read", "Edit", "Write", "Glob", "Grep"] + (["mcp__linear-server__*"] if team else [])
        subprocess.run([CLAUDE, "-p", brief(issue, branch, team, link), "--permission-mode", "acceptEdits",
                        "--allowedTools", *tools,
                        "--disallowedTools", "Bash(gh pr merge:*)", "Bash(gh pr ready:*)",
                        *(["--mcp-config", str(MCP_CONFIG)] if team else []),
                        "--max-turns", MAX_TURNS, "--output-format", "json"],
                       cwd=path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)
        # the agent renames the branch after the Linear issue, so ask the worktree what it is now
        branch = run(["git", "-C", str(path), "branch", "--show-current"]).strip() or branch
        prs = json.loads(run([GH, "pr", "list", "--repo", REPO, "--head", branch, "--state", "open",
                              "--json", "number,url"]) or "[]")
        comments = json.loads(run([GH, "issue", "view", str(number), "--repo", REPO, "--json", "comments"]) or "{}")
        lin_url = link["url"] if link else linear_url(comments.get("comments", []))
    except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as e:
        relabel(number, NEEDS_HUMAN, WORKING)
        report(number, f"The agent couldn't work on #{number}: {str(e)[:300]}\n{issue.get('url', '')}")
        raise
    if prs:
        # the PR link on the issue is what Solo reads when this machine can't post to Slack itself
        run([GH, "issue", "comment", str(number), "--repo", REPO, "--body", f"Draft PR: {prs[0]['url']}"])
        relabel(number, DONE, WORKING)
        linear_note(link, f"Draft PR: {prs[0]['url']} — a person still reviews and merges it.")
        report(number, f"Draft PR for #{number} is up: {prs[0]['url']}" + (f"\nLinear: {lin_url}" if lin_url else "")
               + "\nA person still reviews and merges it.")
    else:
        relabel(number, NEEDS_HUMAN, WORKING)
        linear_note(link, f"The agent stopped without a PR; its comment on {issue.get('url', 'the GitHub issue')} says why.")
        report(number, f"The agent looked at #{number} and didn't open a PR; its comment on the issue says why.\n"
               f"{issue.get('url', '')}" + (f"\nLinear: {lin_url}" if lin_url else ""))
    if is_test(issue):
        close_test(number, prs, link)
        report(number, f"Test request: #{number}" + (" and its draft PR are" if prs else " is") + " now closed.")


def main() -> int:
    print(config.load_secrets())
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
