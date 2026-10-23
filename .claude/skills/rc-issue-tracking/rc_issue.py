"""Create a RevenueChaser issue, put it on board #1 with a Status, and attach it as a sub-issue.

Usage:
    py -3 rc_issue.py --title "..." --body-file body.md [--label bug --label "priority: high"]
                      [--parent 36] [--status Todo] [--area classification]
    py -3 rc_issue.py --link 38 --parent 36          # attach an existing issue
    py -3 rc_issue.py --move 37 --status "In Progress"  # change board status
    py -3 rc_issue.py --move 37 --review "Approved to build"  # record the user's review

Prints the issue number. Needs `gh` authenticated as alsanvolkan with the `project` scope.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

REPO = "alsanvolkan/RevenueChaser-public"
OWNER = "alsanvolkan"
PROJECT = "1"


def sh(*args: str, input: str | None = None) -> str:
    r = subprocess.run(args, capture_output=True, text=True, input=input, encoding="utf-8")
    if r.returncode:
        sys.exit(f"FAILED {' '.join(args[:4])}: {r.stderr.strip()}")
    return r.stdout.strip()


def node_id(number: int) -> str:
    return sh("gh", "issue", "view", str(number), "-R", REPO, "--json", "id", "-q", ".id")


def project_item(number: int) -> str:
    """Board item id for an issue, adding it to the board when missing (idempotent)."""
    url = f"https://github.com/{REPO}/issues/{number}"
    out = sh("gh", "project", "item-add", PROJECT, "--owner", OWNER, "--url", url, "--format", "json")
    return json.loads(out)["id"]


def set_status(number: int, status: str, field_name: str = "Status") -> None:
    """Set a single-select board field (Status by default, or Area) on an issue."""
    pid = json.loads(sh("gh", "project", "view", PROJECT, "--owner", OWNER, "--format", "json"))["id"]
    fields = json.loads(sh("gh", "project", "field-list", PROJECT, "--owner", OWNER,
                           "--format", "json"))["fields"]
    field = next(f for f in fields if f["name"] == field_name)
    option = next((o["id"] for o in field["options"] if o["name"] == status), None)
    if option is None:
        sys.exit(f"unknown {field_name} {status!r}; options: {[o['name'] for o in field['options']]}")
    sh("gh", "api", "graphql", "-f",
       "query=mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue("
       "input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}",
       "-f", f"p={pid}", "-f", f"i={project_item(number)}", "-f", f"f={field['id']}", "-f", f"o={option}")


def attach(child: int, parent: int) -> None:
    sh("gh", "api", "graphql", "-H", "GraphQL-Features: sub_issues", "-f",
       "query=mutation($a:ID!,$b:ID!){addSubIssue(input:{issueId:$a,subIssueId:$b}){subIssue{number}}}",
       "-f", f"a={node_id(parent)}", "-f", f"b={node_id(child)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title")
    ap.add_argument("--body-file")
    ap.add_argument("--label", action="append", default=[])
    ap.add_argument("--parent", type=int)
    ap.add_argument("--status")
    ap.add_argument("--link", type=int, help="attach an existing issue to --parent")
    ap.add_argument("--move", type=int, help="set --status / --area on an existing issue")
    ap.add_argument("--review", help='board Review field: "Not reviewed", "Reviewed", '
                                     '"Approved to build" or "Accepted"')
    ap.add_argument("--area", help="area key (e.g. classification); sets the board Area field "
                                   "and adds the area:<key> label")
    a = ap.parse_args()

    if a.move:
        if not (a.status or a.area or a.review):
            sys.exit("--move needs --status, --area and/or --review")
        if a.review:
            set_status(a.move, a.review, "Review")
        if a.status:
            set_status(a.move, a.status)
        if a.area:
            set_status(a.move, a.area, "Area")
            sh("gh", "issue", "edit", str(a.move), "-R", REPO, "--add-label", f"area:{a.area}")
        print(a.move)
        return
    if a.link:
        if not a.parent:
            sys.exit("--link needs --parent")
        attach(a.link, a.parent)
        print(a.link)
        return
    if not (a.title and a.body_file):
        sys.exit("--title and --body-file are required to create an issue")
    body = open(a.body_file, encoding="utf-8").read()
    args = ["gh", "issue", "create", "-R", REPO, "--title", a.title, "--body-file", "-"]
    for label in a.label + ([f"area:{a.area}"] if a.area else []):
        args += ["--label", label]
    number = int(sh(*args, input=body).rsplit("/", 1)[1])
    set_status(number, a.status or "Todo")
    set_status(number, a.review or "Not reviewed", "Review")
    if a.area:
        set_status(number, a.area, "Area")
    if a.parent:
        attach(number, a.parent)
    print(number)


if __name__ == "__main__":
    main()
