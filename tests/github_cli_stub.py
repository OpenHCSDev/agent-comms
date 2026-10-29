#!/usr/bin/env python3
"""The external gh boundary for local pull-request fixtures; never contacts GitHub."""

import json
import os
import re
import shutil
import sys
from pathlib import Path


def main():
    directory = Path(sys.argv[0]).parent
    arguments = sys.argv[1:]
    if arguments[:2] == ["help", "--"]:
        path = os.pathsep.join(part for part in os.environ["PATH"].split(os.pathsep)
                               if Path(part).resolve() != directory.resolve())
        executable = shutil.which(Path(sys.argv[0]).name, path=path)
        if executable is None:
            raise FileNotFoundError("The actual gh executable is required for its option declarations.")
        os.execv(executable, [executable, *arguments])
    repositories = json.loads((directory / "pull_requests.json").read_text())
    with (directory / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps(arguments) + "\n")
    if arguments[:2] == ["pr", "view"]:
        data = repositories[arguments[arguments.index("-R") + 1]]
        request = data["requests"][arguments[2]]
        if refusal := request.get("read_refusal"):
            print(refusal, file=sys.stderr)
            raise SystemExit(1)
        field = arguments[arguments.index("--json") + 1]
        assert arguments[arguments.index("--jq") + 1] == "." + field
        print({"number": request["listed"]["number"], "body": request["body"]}[field])
    elif len(arguments) > 1 and arguments[0] == "api" and arguments[1].startswith("repos/"):
        _, owner, name, *parts = arguments[1].split("/")
        data = repositories[f"{owner}/{name}"]
        if parts == ["pulls"]:
            for entry in data["requests"].values():
                if entry["listed"]["kind"] in data["open_kinds"]:
                    print(entry["listed"]["number"])
        elif parts[:1] == ["pulls"] and len(parts) == 2:
            assert arguments[arguments.index("--jq") + 1] == (
                '.head.sha + " " + .base.ref + " " + (.draft | tostring) + " " + .state')
            listed = data["requests"][parts[1]]["listed"]
            state = "open" if listed["kind"] in data["open_kinds"] else "closed"
            print(listed["head"], listed["base"], str(listed["kind"] == "draft").lower(), state)
        elif len(parts) == 3 and parts[0] == "commits" and parts[2] == "pulls":
            assert arguments[arguments.index("--jq") + 1] == "map(.number)"
            print(json.dumps([entry["listed"]["number"] for entry in data["requests"].values()
                              if entry["listed"].get("merge", {}).get("oid") == parts[1]
                              or entry["listed"]["head"] == parts[1]]))
        elif parts[:1] == ["statuses"] and len(parts) == 2:
            assert arguments[arguments.index("--method") + 1] == "POST"
            print("{}")
        elif len(parts) == 3 and parts[0] == "commits" and parts[2] == "status":
            query = arguments[arguments.index("--jq") + 1]
            selected = re.search(r'select\(\.context == ("(?:\\.|[^"\\])*")\)', query)
            assert selected, query
            context = json.loads(selected.group(1))
            states = {}
            endpoint = f"repos/{owner}/{name}/statuses/{parts[1]}"
            for line in (directory / "calls.jsonl").read_text().splitlines():
                posted = json.loads(line)
                if posted[:2] == ["api", endpoint] and "--method" in posted:
                    fields = dict(posted[index + 1].split("=", 1)
                                  for index, word in enumerate(posted) if word == "-f")
                    states[fields["context"]] = fields["state"]
            print(json.dumps([states[context]] if context in states else []))
        else:
            raise AssertionError(f"Unexpected repository endpoint: {parts}")
    elif arguments[:2] == ["pr", "merge"]:
        repository, number = arguments[arguments.index("--repo") + 1], arguments[2]
        request = repositories[repository]["requests"][number]
        assert arguments == ["pr", "merge", number, "--repo", repository,
                             "--auto", "--merge", "--match-head-commit", request["listed"]["head"]]
        if refusal := repositories[repository]["requests"][number].get("arm_refusal"):
            sys.exit(refusal)
    elif arguments[:2] == ["api", "graphql"]:
        query = arguments[arguments.index("-f") + 1]
        assert query.startswith("query=")
        variables = dict(arguments[index + 1].split("=", 1)
                         for index, value in enumerate(arguments) if value == "-F")
        inline = re.search(r'repository\(owner: "([^"]+)", name: "([^"]+)"\)', query)
        repository = "/".join(inline.groups()) if inline else f"{variables['owner']}/{variables['name']}"
        data = repositories[repository]
        selections = re.findall(r"p(\d+): pullRequest\(number: (\d+)\)", query)
        if selections:
            assert all(alias == number for alias, number in selections)
            # gh --jq returns the typed projection, not the raw GraphQL envelope.
            print(json.dumps({f"p{number}": data["requests"][number]["listed"]
                              for _, number in selections}))
        elif 'updated: updatedAt }' in query:
            projection = arguments[arguments.index('--jq') + 1]
            since = re.search(r'\.updated >= "([^"]+)"', projection).group(1)
            rows = [{'number': entry['listed']['number'], 'updated': entry['listed']['updated']}
                    for entry in data['requests'].values() if entry['listed']['updated'] >= since]
            print(json.dumps({'rows': rows, 'page_info': {'hasNextPage': False, 'endCursor': None}}))
        elif "pullRequests(" in query:
            print(json.dumps({"data": {"repository": {"pullRequests": {
                "nodes": [{"number": entry["listed"]["number"],
                           "headRefName": entry["listed"]["branch"], "isCrossRepository": False}
                          for entry in data["requests"].values()
                          if entry["listed"]["kind"] in data["open_kinds"]],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}))
        elif "issues(" in query:
            print(json.dumps({"data": {"repository": {"issues": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}))
        else:
            raise AssertionError(f"Unexpected GraphQL query: {query}")
    else:
        raise AssertionError(f"Unexpected gh invocation: {arguments!r}")


if __name__ == "__main__":
    main()
