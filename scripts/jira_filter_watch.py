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

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "notified_tickets.json"

# ---- Jira -----------------------------------------------------------------

def fetch_filter_issues():
    """Fetch all issues currently matching the given Jira filter ID."""
    # Resolve the filter's JQL first (filters can be edited over time,
    # this keeps us in sync with whatever the filter currently means).
    filter_url = f"{JIRA_BASE_URL}/rest/api/2/filter/{JIRA_FILTER_ID}"
    resp = requests.get(filter_url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), timeout=30)
    resp.raise_for_status()
    jql = resp.json()["jql"]

    issues = []
    start_at = 0
    page_size = 50
    while True:
        search_url = f"{JIRA_BASE_URL}/rest/api/2/search"
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": page_size,
            "fields": "summary,status,priority,assignee,issuetype",
        }
        resp = requests.get(search_url, params=params, auth=(JIRA_EMAIL, JIRA_API_TOKEN), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data["issues"])
        start_at += page_size
        if start_at >= data["total"]:
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

    card = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "0076D7",
        "summary": f"New Jira ticket: {key}",
        "title": f"🆕 New ticket: {key} — {summary}",
        "sections": [
            {
                "facts": [
                    {"name": "Type", "value": issue_type},
                    {"name": "Status", "value": status},
                    {"name": "Priority", "value": priority},
                    {"name": "Assignee", "value": assignee},
                ],
                "markdown": True,
            }
        ],
        "potentialAction": [
            {
                "@type": "OpenUri",
                "name": "Open in Jira",
                "targets": [{"os": "default", "uri": issue_url}],
            }
        ],
    }

    resp = requests.post(TEAMS_WEBHOOK_URL, json=card, timeout=30)
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
