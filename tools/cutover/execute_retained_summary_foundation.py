"""Parent-only one-use foundation publication; no changed-task wire carry.

The review plan is the original typed ReviewedRetainedSummaryCohort, including
the exact approved package and actual journey artifact hashes. Foundation's
NoDecision omission/private attestations must already have been reviewed. A
future task-wire conversion uses its own carry member, never this entrypoint.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.owner_cutover import PreserveOwnerRuntime
from publish_retained_summary import ReviewedRetainedSummaryCohort, publish
from runtime_installation import RuntimeInstallation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review-plan', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--runtime-installation', type=Path, required=True,
                        help='Reviewed RuntimeInstallation member JSON')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        parser.error('Parent execution approval is required; --help has no public effects')
    cohort = FieldCodec.decode(ReviewedRetainedSummaryCohort,
                               json.loads(args.review_plan.read_text()))
    runtime = FieldCodec.decode(RuntimeInstallation,
                                json.loads(args.runtime_installation.read_text()))
    results = publish(cohort, PreserveOwnerRuntime(), runtime, args.receipt)
    print(json.dumps({'state':'retained-foundation-published-and-launched',
                      'owners':len(results)}), flush=True)


if __name__ == '__main__':
    main()
