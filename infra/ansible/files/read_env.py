"""Print selected keys of a KEY=value env file (.jira.env, .agent.env) as JSON.

  python3 read_env.py <file> KEY [KEY ...]

Blank lines and # comments are skipped; inline "  # ..." comments and surrounding quotes are
stripped. Only the named keys are printed, so nothing else in the file reaches Ansible.
"""
import json
import re
import sys

path, keys = sys.argv[1], set(sys.argv[2:])
out = {}
with open(path) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key in keys:
            out[key] = re.sub(r"\s+#.*$", "", value).strip().strip("'\"")
print(json.dumps(out))
