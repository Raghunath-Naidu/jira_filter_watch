#!/usr/bin/env python3
"""
Jira Filter Watch
------------------
Polls a Jira filter every run (triggered externally every 10 min via
cron-job.org -> GitHub repository_dispatch) and posts any ticket that
hasn't been notified before to a Microsoft Teams channel via webhook.

State (which tickets have already been notified) is kept in
state/notified_tickets.json and committed back to the repo by the
GitHub Actions workflow after each run.
"""

import json
import os
import sys
from pathlib import Path

import requests

# ---- Config (from environment / GitHub Actions secrets) ------------------

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://axso-tim.atlassian.net").rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_FILTER_ID = os.environ.get("JIRA_FILTER_ID", "38843")
TEAMS_WEBHOOK_URL = os.environ["JIRA_FILTER_WATCH_TEAMS_WEBHOOK_URL"]

# JQL for the "Process CMDP (CMDS)" subtasks under the Employee Mutation
# (EM) project. Matches subtasks whose summary mentions both "Process CMDP"
# and "Employee Mutation", in any status that isn't Done - broader than a
# fixed status list so it doesn't miss tickets sitting in an unanticipated
# status name (e.g. "To Do" vs "Open" vs "New").
JIRA_JQL = os.environ.get(
    "JIRA_JQL",
    'project = EM AND issuetype = Sub-task '
    'AND summary ~ "Process CMDP" AND summary ~ "Employee Mutation" '
    "AND statusCategory != Done "
    "ORDER BY created ASC",
)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "notified_tickets.json"

# ---- Jira -----------------------------------------------------------------

def fetch_filter_issues():
    """Fetch all issues currently matching the filter's JQL.

    Uses the current Jira Cloud search endpoint (POST /rest/api/3/search/jql).
    The older GET /rest/api/2/search endpoint was deprecated by Atlassian
    and now returns 410 Gone, so we use the replacement here, which also
    uses token-based (not offset-based) pagination.
    """
    issues = []
    next_page_token = None
    while True:
        search_url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
        body = {
            "jql": JIRA_JQL,
            "maxResults": 50,
            "fields": ["summary", "status", "priority", "assignee", "issuetype"],
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        resp = requests.post(search_url, json=body, auth=(JIRA_EMAIL, JIRA_API_TOKEN), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data.get("issues", []))

        next_page_token = data.get("nextPageToken")
        if not next_page_token or data.get("isLast", True):
            break

    return issues


# ---- State ------------------------------------------------------------

def load_notified():
    if not STATE_PATH.exists():
        return set()
    with open(STATE_PATH, "r") as f:
        return set(json.load(f))


def save_notified(notified_keys):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(sorted(notified_keys), f, indent=2)


# ---- Teams --------------------------------------------------------------

def post_to_teams(issue):
    key = issue["key"]
    fields = issue["fields"]
    summary = fields.get("summary", "(no summary)")
    status = fields.get("status", {}).get("name", "Unknown")
    priority = (fields.get("priority") or {}).get("name", "None")
    assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
    issue_type = (fields.get("issuetype") or {}).get("name", "Issue")
    issue_url = f"{JIRA_BASE_URL}/browse/{key}"

    # Power Automate "Post to a channel when a webhook request is received"
    # flows accept whatever schema was set on the manual trigger - the most
    # common default is a simple {"text": "..."} body. If your flow was
    # configured with a different schema, adjust this payload to match it
    # (check the flow's trigger step in Power Automate for the expected
    # JSON schema).
    message = (
        f"🆕 New ticket: {key} — {summary}\n\n"
        f"Type: {issue_type}\n"
        f"Status: {status}\n"
        f"Priority: {priority}\n"
        f"Assignee: {assignee}\n"
        f"Link: {issue_url}"
    )
    payload = {"text": message}

    resp = requests.post(TEAMS_WEBHOOK_URL, json=payload, timeout=30)
    resp.raise_for_status()


# ---- Main -----------------------------------------------------------------

def main():
    notified = load_notified()
    issues = fetch_filter_issues()
    current_keys = {issue["key"] for issue in issues}

    new_issues = [issue for issue in issues if issue["key"] not in notified]

    if not new_issues:
        print(f"No new tickets. {len(current_keys)} tickets currently match filter {JIRA_FILTER_ID}.")
        return

    print(f"Found {len(new_issues)} new ticket(s): {[i['key'] for i in new_issues]}")

    failures = []
    for issue in new_issues:
        try:
            post_to_teams(issue)
            notified.add(issue["key"])
        except Exception as e:
            # Don't mark as notified if the Teams post failed - retry next run.
            failures.append((issue["key"], str(e)))
            print(f"Failed to notify {issue['key']}: {e}", file=sys.stderr)

    save_notified(notified)

    if failures:
        print(f"{len(failures)} notification(s) failed and will be retried next run.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
