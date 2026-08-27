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
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ---- Config (from environment / GitHub Actions secrets) ------------------

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://axso-tim.atlassian.net").rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_FILTER_ID = os.environ.get("JIRA_FILTER_ID", "38843")
TEAMS_WEBHOOK_URL = os.environ["JIRA_FILTER_WATCH_TEAMS_WEBHOOK_URL"]

# Email alert config - same Gmail SMTP approach as sla-watch. Sends only
# after a successful Teams post (additional channel, not a failure fallback).
GMAIL_SENDER_EMAIL = os.environ.get("GMAIL_SENDER_EMAIL")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "Vanshika.Srivastava@axpo.com")

# JQL for the "Process CMDP (CMDS)" subtasks under the Employee Mutation
# (EM) project. Matches on "Process CMDP" alone - earlier tickets end their
# summary with "Employee Mutation" but others use different suffixes (e.g.
# "EM: Leave", "EM: Joiner"), so matching only on the consistent "Process
# CMDP" prefix catches all of them. Any status that isn't Done - broader
# than a fixed status list so it doesn't miss tickets sitting in an
# unanticipated status name (e.g. "To Do" vs "Open" vs "New").
_DEFAULT_JQL = (
    'project = EM AND issuetype = Sub-task '
    'AND summary ~ "Process CMDP" '
    "AND statusCategory != Done "
    "ORDER BY created ASC"
)
JIRA_JQL = os.environ.get("JIRA_JQL") or _DEFAULT_JQL

# When true (set via the Actions "dry_run" input), print matching tickets
# instead of posting to Teams or updating the state file. Useful for
# testing a JQL override (e.g. statusCategory = Done) without spamming the
# channel or marking real tickets as "already notified".
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

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
        if not resp.ok:
            # Jira puts the actual reason in the response body (e.g. bad
            # JQL syntax) - print it before raising, since raise_for_status()
            # alone only shows the status code, not why it failed.
            print(f"Jira API error {resp.status_code}: {resp.text}", file=sys.stderr)
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

    # This Power Automate flow uses the "Post card in a chat or channel"
    # action, which requires a full Adaptive Card JSON body (confirmed via
    # the flow's InvalidBotAdaptiveCard error - it rejects plain {"text":...}
    # payloads). See https://adaptivecards.io/explorer/ for the schema.
    adaptive_card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"🆕 New ticket: {key} — {summary}",
                "wrap": True,
                "weight": "Bolder",
                "size": "Medium",
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Type", "value": issue_type},
                    {"title": "Status", "value": status},
                    {"title": "Priority", "value": priority},
                    {"title": "Assignee", "value": assignee},
                ],
            },
        ],
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Open in Jira",
                "url": issue_url,
            }
        ],
    }
    payload = adaptive_card

    resp = requests.post(TEAMS_WEBHOOK_URL, json=payload, timeout=30)
    resp.raise_for_status()


# ---- Email ------------------------------------------------------------

def send_email_alert(issue):
    """Send an email alert for this ticket via Gmail SMTP.

    Only called after a successful Teams post (see main()) - this is an
    additional notification channel, not a failure fallback. If email
    sending fails, that failure is logged but does NOT affect whether the
    ticket is marked as notified (Teams already succeeded).
    """
    if not GMAIL_SENDER_EMAIL or not GMAIL_APP_PASSWORD:
        print(
            f"Skipping email for {issue['key']}: GMAIL_SENDER_EMAIL / "
            "GMAIL_APP_PASSWORD not configured.",
            file=sys.stderr,
        )
        return

    key = issue["key"]
    fields = issue["fields"]
    summary = fields.get("summary", "(no summary)")
    status = fields.get("status", {}).get("name", "Unknown")
    priority = (fields.get("priority") or {}).get("name", "None")
    assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
    issue_type = (fields.get("issuetype") or {}).get("name", "Issue")
    issue_url = f"{JIRA_BASE_URL}/browse/{key}"

    body = (
        f"New ticket: {key} — {summary}\n\n"
        f"Type: {issue_type}\n"
        f"Status: {status}\n"
        f"Priority: {priority}\n"
        f"Assignee: {assignee}\n\n"
        f"Link: {issue_url}\n"
    )

    msg = MIMEText(body)
    msg["Subject"] = f"[Jira Filter Watch] New ticket: {key}"
    msg["From"] = GMAIL_SENDER_EMAIL
    msg["To"] = ALERT_EMAIL_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(GMAIL_SENDER_EMAIL, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_SENDER_EMAIL, [ALERT_EMAIL_TO], msg.as_string())


# ---- Main -----------------------------------------------------------------

def main():
    print(f"JQL: {JIRA_JQL}")
    if DRY_RUN:
        print("DRY RUN: will not post to Teams or update state file.")

    issues = fetch_filter_issues()
    current_keys = {issue["key"] for issue in issues}
    print(f"{len(current_keys)} ticket(s) currently match: {sorted(current_keys)}")

    if DRY_RUN:
        return

    notified = load_notified()
    new_issues = [issue for issue in issues if issue["key"] not in notified]

    if not new_issues:
        print("No new tickets.")
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
            continue

        # Teams post succeeded - also send the email alert. Email failures
        # are logged only; they don't affect the "notified" state, since
        # Teams (the primary channel) already succeeded.
        try:
            send_email_alert(issue)
        except Exception as e:
            print(f"Teams succeeded but email failed for {issue['key']}: {e}", file=sys.stderr)

    save_notified(notified)

    if failures:
        print(f"{len(failures)} notification(s) failed and will be retried next run.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
