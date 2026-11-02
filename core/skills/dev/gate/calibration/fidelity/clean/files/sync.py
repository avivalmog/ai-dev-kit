"""Sync local records to the server."""
import argparse

BATCH_SIZE = 500


def run(records: list[dict], dry_run: bool = False) -> int:
    """Send records in batches; with dry_run, only print what would be sent."""
    sent = 0
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        if dry_run:
            for record in batch:
                print(f"would send {record}")
        else:
            send(batch)
        sent += len(batch)
    return sent


def send(batch: list[dict]) -> None:
    """Placeholder transport."""
    raise NotImplementedError


def main() -> None:
    """CLI entry."""
    parser = argparse.ArgumentParser(prog="sync")
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent, write nothing")
    args = parser.parse_args()
    run([], dry_run=args.dry_run)
