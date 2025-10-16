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
        "--resume/--no-resume",
        dest="resume",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Skip completed steps when running the full pipeline",
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
            if args.step == "brands" or not args.resume or scraper.has_brand_work():
                scraper.scrape_brands(reset_offset=not args.resume)
            else:
                logging.info("Skipping brand collection – already complete")
        if args.step in {"all", "products"}:
            if args.step == "products" or not args.resume or scraper.has_product_work():
                scraper.scrape_products()
            else:
                logging.info("Skipping product collection – nothing left to do")
        if args.step in {"all", "details"}:
            if (
                args.step == "details"
                or not args.resume
                or scraper.has_product_detail_work()
            ):
                scraper.scrape_product_details()
            else:
                logging.info("Skipping product detail collection – nothing left to do")
    finally:
        scraper.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
