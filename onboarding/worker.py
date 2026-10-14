"""Separate resumable worker. Crash injection exits after an actual app write."""
import argparse
import json
from .engine import run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--world", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--crash-after-write", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.data, args.world, args.case, args.crash_after_write)))


if __name__ == "__main__":
    main()
