"""Print the Jira keys of a KEY=value env file (.jira.env) as JSON.

Blank lines and # comments are skipped; inline "  # ..." comments and surrounding quotes are
stripped. Only the keys the alert bridge needs are printed.
"""
import json
import re
import sys

KEYS = {"JIRA_API_URL", "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"}

out = {}
with open(sys.argv[1]) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key in KEYS:
            out[key] = re.sub(r"\s+#.*$", "", value).strip().strip("'\"")
print(json.dumps(out))
