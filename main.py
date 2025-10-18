"""Command line interface for running the INCIDecoder scraper."""
from __future__ import annotations

import argparse
import logging
from inciscraper import INCIScraper


def build_parser() -> argparse.ArgumentParser:
    """Create and configure the command line argument parser.

    Türkçe: Komut satırı argümanlarını ayrıştıracak `ArgumentParser` nesnesini
    oluşturup tüm seçenekleri tanımlar.
    """
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
        "--alternate-base-url",
        action="append",
        default=None,
        metavar="URL",
        help=(
            "Additional base URLs that will be tried automatically if the primary host "
            "cannot be resolved. The option can be specified multiple times."
        ),
    )
    parser.add_argument(
        "--step",
        choices=["all", "brands", "products", "details"],
        default="all",
        help="Run only a specific pipeline step",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Limit the number of brand listing pages fetched during the brands step. "
            "Ignored for other steps."
        ),
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
    """Initialise the logging configuration for the CLI.

    Türkçe: Komut satırı aracının günlük yapılandırmasını verilen ayrıntı
    seviyesine göre kurar.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point used by both the module and command line execution.

    Türkçe: Hem modül hem de doğrudan komut satırı çalıştırmaları için başlangıç
    noktası olup, argümanları okur, scraper'ı başlatır ve seçilen adımları
    yürütür.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be a positive integer")
    configure_logging(args.log_level)
    scraper = INCIScraper(
        db_path=args.db,
        image_dir=args.images_dir,
        base_url=args.base_url,
        alternate_base_urls=args.alternate_base_url,
    )
    try:
        scraper.resume_incomplete_metadata()
        summary = scraper.get_workload_summary()
        brand_pages_remaining = summary["brand_pages_remaining"]
        brand_pages_text = (
            str(brand_pages_remaining) if brand_pages_remaining is not None else "unknown"
        )
        logging.info(
            "Initial workload – brand pages remaining: %s, brands pending products: %s, products pending details: %s",
            brand_pages_text,
            summary["brands_pending_products"],
            summary["products_pending_details"],
        )
        logging.info(
            "Database snapshot – stored brands: %s, stored products: %s",
            summary["brands_total"],
            summary["products_total"],
        )
        if args.step in {"all", "brands"}:
            if args.step == "brands" or not args.resume or scraper.has_brand_work():
                scraper.scrape_brands(
                    reset_offset=not args.resume,
                    max_pages=args.max_pages,
                )
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
