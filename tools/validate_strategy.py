"""Validate the strategy shipped with the next package without launching a game."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo" / "CoreGeek" / "src"))
from agent.strategy_config import load


def main():
    parser = argparse.ArgumentParser(description="Validate tunable strategy (JSON, no third-party dependencies)")
    parser.add_argument("path", nargs="?", default=str(ROOT / "strategy.json"))
    args = parser.parse_args()
    try:
        config, identity = load(args.path)
    except (ValueError, OSError) as error:
        print(f"INVALID: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"valid": True, "identity": identity, "effective_config": config},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
