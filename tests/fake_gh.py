#!/usr/bin/env python3
"""Stand-in for `gh` in the merge-when-clean tests.

Serves fixture JSON from $FAKE_GH_ROUTES and logs every call to $FAKE_GH_LOG.
Routes are keyed by API path without the query string, `graphql:<pr>` for the
review query, and `merge:<pr>` for `gh pr merge`. A route is one of:
  {"json": obj}, {"pages": [obj, ...]}, {"raw": "text"}, {"error": "message"}.
`--jq` runs the real jq on each page, like gh does. A missing route fails loudly.
"""
import json
import os
import subprocess
import sys

VALUE_FLAGS = {"--jq", "-H", "-f", "-F", "--repo", "-R", "--match-head-commit"}


def parse(argv):
    pos, opts = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in VALUE_FLAGS:
            opts.setdefault(a, []).append(argv[i + 1])
            i += 2
        elif a.startswith("-"):
            opts.setdefault(a, []).append(True)
            i += 1
        else:
            pos.append(a)
            i += 1
    return pos, opts


def main():
    argv = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a") as log:
        log.write(json.dumps(argv) + "\n")
    with open(os.environ["FAKE_GH_ROUTES"]) as f:
        routes = json.load(f)
    pos, opts = parse(argv)

    if pos[:2] == ["pr", "merge"]:
        spec = routes.get(f"merge:{pos[2]}", {})
        if "error" in spec:
            print(spec["error"], file=sys.stderr)
            return 1
        return 0
    if pos[:1] != ["api"]:
        print(f"fake gh: unsupported command {argv}", file=sys.stderr)
        return 2

    endpoint = pos[1].split("?", 1)[0]
    if endpoint == "graphql":
        pr = next(v.split("=", 1)[1] for v in opts.get("-F", []) if v.startswith("pr="))
        endpoint = f"graphql:{pr}"
    spec = routes.get(endpoint)
    if spec is None:
        print(f"fake gh: no route for {endpoint}", file=sys.stderr)
        return 1
    if "error" in spec:
        print(spec["error"], file=sys.stderr)
        return 1
    if "raw" in spec:
        sys.stdout.write(spec["raw"])
        return 0
    pages = spec["pages"] if "pages" in spec else [spec["json"]]
    jq = opts.get("--jq")
    for page in pages:
        if jq:
            r = subprocess.run(["jq", "-r", "-c", jq[0]], input=json.dumps(page),
                               capture_output=True, text=True)
            if r.returncode:
                sys.stderr.write(r.stderr)
                return 1
            sys.stdout.write(r.stdout)
        else:
            sys.stdout.write(json.dumps(page) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
