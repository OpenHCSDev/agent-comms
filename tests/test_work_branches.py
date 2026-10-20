import json
import subprocess

from agent_comms.field_codec import FieldCodec
from agent_comms.work_branches import (
    IssueBranchLink,
    PullRequestBranchLink,
    ThreadBranchCheckout,
    UnregisteredBranchCheckout,
    WorkBranches,
)


def _page(field, nodes, after=None):
    return json.dumps(
        {
            "data": {
                "repository": {
                    field: {
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": after is not None, "endCursor": after},
                    }
                }
            }
        }
    )


