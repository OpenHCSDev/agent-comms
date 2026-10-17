"""Original installed decoder entrypoint for a reviewed Goal publication."""
import json
import sys
from agent_comms.field_codec import FieldCodec
from goal_format_publication import GoalPublicationReview


def main():
    review = FieldCodec.decode(GoalPublicationReview, json.load(sys.stdin))
    print(json.dumps(FieldCodec.encode(review.run())), flush=True)


if __name__ == '__main__':
    main()
