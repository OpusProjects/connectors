#!/usr/bin/env bash
# What a public repository of connectors must not carry: Markdown backticks in
# Python (docstrings and comments are read where nothing renders them), and
# addresses or domains outside the ranges reserved for documentation.
set -euo pipefail
fail=0

if grep -rn --include='*.py' '`' . ; then
  echo "::error::backticks in Python files: write names plainly"
  fail=1
fi

# IPv4 addresses other than 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24,
# loopback and 0.0.0.0.
ips=$(grep -rnoE --exclude-dir=.git --exclude-dir=__pycache__ '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' . \
  | grep -vE ':(192\.0\.2|198\.51\.100|203\.0\.113)\.[0-9]+$|:127\.[0-9.]+$|:0\.0\.0\.0$' || true)
if [ -n "$ips" ]; then
  echo "$ips"
  echo "::error::real-looking IP addresses: use 192.0.2.0/24, 198.51.100.0/24 or 203.0.113.0/24"
  fail=1
fi

# Host names in fixtures and examples must sit under example.com/.net/.org.
hosts=$(grep -rnoiE --include='*.json' --include='*.xml' --include='*.yaml' --include='*.yml' \
  '\b[a-z0-9-]+(\.[a-z0-9-]+)+\.(com|net|org|io|local|corp|internal)\b' . \
  | grep -viE ':([a-z0-9.-]+\.)?example\.(com|net|org)$' || true)
if [ -n "$hosts" ]; then
  echo "$hosts"
  echo "::error::real-looking host names: use example.com, example.net or example.org"
  fail=1
fi

exit $fail
