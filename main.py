"""Command line interface for running the INCIDecoder scraper."""
from __future__ import annotations

import argparse
import logging
from inciscraper import INCIScraper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape brand, product and ingredient information from INCIDecoder "
            "and persist the results into a local SQLite database."
        )
    )
    parser.add_argument(
        "--db",
        default="incidecoder.db",
        help="SQLite database path (default: incidecoder.db)",
    )
    parser.add_argument(
        "--images-dir",
        default="images",
        help="Directory where downloaded product images will be stored",
    )
    parser.add_argument(
        "--base-url",
        default="https://incidecoder.com",
        help="Override the INCIDecoder base URL (useful for testing)",
    )
    parser.add_argument(
        "--step",
        choices=["all", "brands", "products", "details"],
        default="all",
        help="Run only a specific pipeline step",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    scraper = INCIScraper(db_path=args.db, image_dir=args.images_dir, base_url=args.base_url)
    try:
        if args.step in {"all", "brands"}:
            scraper.scrape_brands()
        if args.step in {"all", "products"}:
            scraper.scrape_products()
        if args.step in {"all", "details"}:
            scraper.scrape_product_details()
    finally:
        scraper.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
