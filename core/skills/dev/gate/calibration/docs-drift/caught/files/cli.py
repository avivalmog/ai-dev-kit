"""Command line for the exporter."""
import argparse


def build_parser() -> argparse.ArgumentParser:
    """Arguments of the export command."""
    parser = argparse.ArgumentParser(prog="export")
    parser.add_argument("--max-rows", type=int, default=100, help="stop after this many rows")
    parser.add_argument("--out", required=True, help="output file")
    return parser
