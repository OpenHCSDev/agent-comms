#!/usr/bin/env python3
"""The external gh boundary for local pull-request fixtures; never contacts GitHub."""

import json
import re
import sys
from pathlib import Path


def main():
    directory = Path(sys.argv[0]).parent
    data = json.loads((directory / "pull_requests.json").read_text())
    arguments = sys.argv[1:]
    with (directory / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps(arguments) + "\n")
    if arguments[:2] == ["pr", "view"]:
        assert arguments[arguments.index("-R") + 1] == data["repo"]
        request = data["requests"][arguments[2]]
        field = arguments[arguments.index("--json") + 1]
        assert arguments[arguments.index("--jq") + 1] == "." + field
        print({"number": request["listed"]["number"], "body": request["body"]}[field])
    elif arguments[:2] == ["api", "graphql"]:
        query = arguments[arguments.index("-f") + 1]
        assert query.startswith("query=")
        owner, name = data["repo"].split("/")
        assert f'repository(owner: "{owner}", name: "{name}")' in query
        selections = re.findall(r"p(\d+): pullRequest\(number: (\d+)\)", query)
        assert selections, "Only explicit recorded pull-request selections are supported"
        assert all(alias == number for alias, number in selections)
        # gh --jq returns the typed projection, not the raw GraphQL envelope.
        print(json.dumps({f"p{number}": data["requests"][number]["listed"]
                          for _, number in selections}))
    else:
        raise AssertionError(f"Unexpected gh invocation: {arguments!r}")


if __name__ == "__main__":
    main()
