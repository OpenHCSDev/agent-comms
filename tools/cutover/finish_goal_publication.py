"""Target installed decoder accepts the original stopped Goal handoff once."""
import json
import sys
from agent_comms.field_codec import FieldCodec
from goal_format_publication import StoppedGoalPublication


def main():
    wire_descriptor, route_descriptor = map(int, sys.argv[1:])
    stopped = FieldCodec.decode(StoppedGoalPublication, json.load(sys.stdin))
    print(json.dumps(FieldCodec.encode(stopped.finish(wire_descriptor, route_descriptor))), flush=True)


if __name__ == '__main__':
    main()
