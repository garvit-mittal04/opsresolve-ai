"""Command-line entry point: ``python -m opsresolve`` or ``opsresolve``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from opsresolve import investigation
from opsresolve.config import MatchConfig
from opsresolve.pipeline import investigate_files
from opsresolve.profiles import ProfileError, load_profile
from opsresolve.reporting import INVESTIGATIONS_CSV, render_console, write_investigations_csv, write_reports

DEFAULT_DATA_DIR = Path("data/sample")
INVESTIGATIONS_UNAVAILABLE = ("Investigation checks could not be prepared, so investigations.csv was not written. "
                              "The other report files are complete.")
EXIT_INVESTIGATIONS_UNAVAILABLE = 3
STALE_NOT_REMOVED = "An earlier investigations.csv in the output directory could not be removed; do not rely on it."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opsresolve",
        description="Three-way match purchase orders, invoices and goods receipts and report exceptions.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="Directory containing purchase_orders.csv, invoices.csv and goods_received.csv "
                             "(default: data/sample).")
    parser.add_argument("--purchase-orders", type=Path, help="Override path to the purchase orders CSV.")
    parser.add_argument("--invoices", type=Path, help="Override path to the invoices CSV.")
    parser.add_argument("--goods-received", type=Path, help="Override path to the goods received CSV.")
    parser.add_argument("--uom-conversions", type=Path,
                        help="Per-SKU unit-of-measure conversions CSV. Optional: defaults to "
                             "uom_conversions.csv in --data-dir when that file exists.")
    parser.add_argument("--profile", type=Path,
                        help="Import profile (JSON) declaring the CSV format of every input file, including "
                             "uom_conversions.csv. Without it, the built-in format is used.")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"),
                        help="Where to write the report files (default: reports).")
    parser.add_argument("--fail-on-exceptions", action="store_true",
                        help="Exit with status 1 if any exception or validation issue is found.")
    parser.add_argument("--with-investigations", action="store_true",
                        help="Also write investigations.csv: candidate explanations for each exception, checked "
                             "deterministically against the loaded records (never confirmed causes). Off by "
                             "default; the other report files are the same either way.")
    return parser


def _optional(path: Path) -> Path | None:
    return path if path.exists() else None


def _remove_stale_investigations(output_dir: Path) -> None:
    """Remove an earlier run's investigations.csv (a symlink: only the link). A directory of that name is not a
    file this program wrote and is left alone; a file that cannot be removed gets a fixed warning."""
    path = output_dir / INVESTIGATIONS_CSV
    if path.is_dir() and not path.is_symlink():
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:  # a fixed message, never the error's text
        print(STALE_NOT_REMOVED, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = MatchConfig.from_env()
    except ValidationError as exc:
        print(f"Invalid OPSRESOLVE_* configuration:\n{exc}", file=sys.stderr)
        return 2
    profile = None
    if args.profile is not None:
        try:
            profile = load_profile(args.profile)
        except ProfileError as exc:
            print(f"{args.profile}: {exc}", file=sys.stderr)
            return 2

    result = investigate_files(
        args.purchase_orders or args.data_dir / "purchase_orders.csv",
        args.invoices or args.data_dir / "invoices.csv",
        args.goods_received or args.data_dir / "goods_received.csv",
        config,
        uom_conversions_path=args.uom_conversions or _optional(args.data_dir / "uom_conversions.csv"),
        profile=profile,
    )
    print(render_console(result))
    paths = write_reports(result, args.output_dir)
    investigations_unavailable = False
    if args.with_investigations:
        try:
            found = investigation.investigate_all(result)
        except Exception:  # an error while indexing the run: a fixed message, never the error's text
            investigations_unavailable = True
            print(INVESTIGATIONS_UNAVAILABLE, file=sys.stderr)
            _remove_stale_investigations(args.output_dir)  # never leave a previous run's file
        else:
            paths["investigations_csv"] = write_investigations_csv(result, found, args.output_dir)
            failed = sum(len(item.failures) for item in found.values())
            if failed:
                print(f"{failed} investigation check(s) could not be completed; investigations.csv shows them as "
                      f"'Check failed — nothing concluded'.", file=sys.stderr)
    print()
    for name, path in paths.items():
        print(f"wrote {name}: {path}")

    if investigations_unavailable:
        return EXIT_INVESTIGATIONS_UNAVAILABLE
    if args.fail_on_exceptions and (result.exceptions or result.dataset.issues):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
