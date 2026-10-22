"""The few Linear calls Solo makes for engineering requests, over the GraphQL API.

LINEAR_API_KEY comes from Infisical or .env (config.load_secrets). LINEAR_API_URL points the
offline tests at a fake. Every call raises RuntimeError with Linear's own words on failure.
"""
from __future__ import annotations

import re

import config

ISSUE_FIELDS = "id identifier url branchName"
_teams: dict[str, dict] = {}


def enabled() -> bool:
    return bool(config.env("LINEAR_API_KEY"))


def gql(query: str, variables: dict | None = None) -> dict:
    import httpx
    resp = httpx.post(config.env("LINEAR_API_URL", "https://api.linear.app/graphql"), headers={"Authorization": config.env("LINEAR_API_KEY")},
                      json={"query": query, "variables": variables or {}}, timeout=20)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code >= 300 or data.get("errors") or "data" not in data:
        msg = "; ".join(e.get("message", "") for e in data.get("errors", [])) or resp.text[:200]
        raise RuntimeError(f"Linear {resp.status_code}: {msg}")
    return data["data"]


def team(key: str) -> dict:
    """{id, states: {type: id}, labels: {name: id}} for a team key such as ENG. Cached."""
    if key not in _teams:
        nodes = gql("query($k: String!) { teams(filter: {key: {eq: $k}}) { nodes { id "
                    "states { nodes { id type } } labels(first: 250) { nodes { id name } } } } }", {"k": key})["teams"]["nodes"]
        if not nodes:
            raise RuntimeError(f"Linear has no team {key}")
        t = nodes[0]
        _teams[key] = {"id": t["id"],
                       "states": {s["type"]: s["id"] for s in t["states"]["nodes"]},
                       "labels": {lab["name"]: lab["id"] for lab in t["labels"]["nodes"]}}
    return _teams[key]


def find_sr(key: str, sr: str) -> dict | None:
    """The open-or-closed issue in team `key` whose title already names this SR, e.g. "… (SR-49)".

    Linear's filter is a substring match, so SR-7 also finds SR-72; the id is checked again here
    with a boundary on both sides."""
    nodes = gql("query($t: String!, $s: String!) { issues(first: 20, filter: {team: {key: {eq: $t}}, "
                "title: {containsIgnoreCase: $s}}) { nodes { " + ISSUE_FIELDS + " title } } }",
                {"t": key, "s": sr})["issues"]["nodes"]
    exact = re.compile(rf"(?<![\w-]){re.escape(sr)}(?!\d)", re.I)
    return next((n for n in nodes if exact.search(n["title"])), None)


def label_id(key: str, name: str) -> str:
    t = team(key)
    if name not in t["labels"]:
        made = gql("mutation($i: IssueLabelCreateInput!) { issueLabelCreate(input: $i) { issueLabel { id } } }",
                   {"i": {"name": name, "teamId": t["id"]}})
        t["labels"][name] = made["issueLabelCreate"]["issueLabel"]["id"]
    return t["labels"][name]


def create_issue(key: str, title: str, description: str, assignee: str | None, labels: list[str]) -> dict:
    """{id, identifier, url, branchName}. `assignee` is a Linear user id, or None for unassigned."""
    t = team(key)
    data = {"teamId": t["id"], "title": title, "description": description,
            "labelIds": [label_id(key, name) for name in labels]}
    if assignee:
        data["assigneeId"] = assignee
    if "unstarted" in t["states"]:
        data["stateId"] = t["states"]["unstarted"]   # Todo, not Backlog: someone is meant to pick it up
    return gql("mutation($i: IssueCreateInput!) { issueCreate(input: $i) { issue { " + ISSUE_FIELDS + " } } }",
               {"i": data})["issueCreate"]["issue"]


def comment(issue_id: str, body: str) -> None:
    gql("mutation($i: CommentCreateInput!) { commentCreate(input: $i) { success } }",
        {"i": {"issueId": issue_id, "body": body}})


def cancel(issue: str) -> None:
    """Move an issue (id or identifier such as ENG-45) to its team's canceled state. Asks the issue
    for its team, so a machine that only has the GitHub issue's `Linear:` line can do it."""
    states = gql("query($id: String!) { issue(id: $id) { team { states { nodes { id type } } } } }",
                 {"id": issue})["issue"]["team"]["states"]["nodes"]
    canceled = next((st["id"] for st in states if st["type"] == "canceled"), None)
    if not canceled:
        raise RuntimeError(f"{issue}'s team has no canceled state")
    gql("mutation($id: String!, $i: IssueUpdateInput!) { issueUpdate(id: $id, input: $i) { success } }",
        {"id": issue, "i": {"stateId": canceled}})
