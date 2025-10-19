"""Command line interface for running the INCIDecoder scraper."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

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
    default_data_dir = Path("data")
    parser.add_argument(
        "--db",
        default=str(default_data_dir / "incidecoder.db"),
        help="SQLite database path (default: data/incidecoder.db)",
    )
    parser.add_argument(
        "--images-dir",
        default=str(default_data_dir / "images"),
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
        default=False,
        action=argparse.BooleanOptionalAction,
        help="Skip completed steps when running the full pipeline",
    )
    parser.add_argument(
        "--log-level",
        default="ERROR",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help=(
            "Logging verbosity (default: ERROR). Use INFO or DEBUG to capture the full pipeline."
        ),
    )
    parser.add_argument(
        "--log-output",
        action="store_true",
        help=(
            "Write log output to data/logs/inciscraper.log in addition to the console"
        ),
    )
    parser.add_argument(
        "--sample-data",
        action="store_true",
        help=(
            "Generate a small verification dataset instead of running the full pipeline. "
            "The sample database will contain three brands with a single fully scraped "
            "product each."
        ),
    )
    return parser


def configure_logging(level: str, *, log_to_file: bool = False) -> Optional[Path]:
    """Initialise the logging configuration for the CLI.

    Türkçe: Komut satırı aracının günlük yapılandırmasını verilen ayrıntı
    seviyesine göre kurar.
    """
    log_level = getattr(logging, level.upper(), logging.ERROR)
    handlers = [logging.StreamHandler()]
    log_file_path: Optional[Path] = None
    if log_to_file:
        logs_dir = Path("data") / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = logs_dir / "inciscraper.log"
        handlers.append(logging.FileHandler(log_file_path, encoding="utf-8"))
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_file_path


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
    log_file = configure_logging(args.log_level, log_to_file=args.log_output)
    if log_file:
        print(f"Logging to {log_file}")
    db_path = args.db
    images_dir = Path(args.images_dir)
    if args.sample_data:
        images_dir = Path("data") / "sample_images"
    images_dir.mkdir(parents=True, exist_ok=True)
    if args.sample_data:
        db_path_obj = Path(db_path)
        if not db_path_obj.name.startswith("sample_"):
            db_path_obj = db_path_obj.with_name(f"sample_{db_path_obj.name}")
        db_path = str(db_path_obj)
    db_path_obj = Path(db_path)
    db_path_obj.parent.mkdir(parents=True, exist_ok=True)
    scraper = INCIScraper(
        db_path=db_path,
        image_dir=str(images_dir),
        base_url=args.base_url,
        alternate_base_urls=args.alternate_base_url,
    )
    try:
        if args.sample_data:
            logging.info("Generating sample dataset (3 brands × 1 product each)")
            scraper.generate_sample_dataset(brand_count=3, products_per_brand=1)
        else:
            if args.resume:
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
                    scraper.scrape_products(rescan_all=not args.resume)
                else:
                    logging.info("Skipping product collection – nothing left to do")
            if args.step in {"all", "details"}:
                if (
                    args.step == "details"
                    or not args.resume
                    or scraper.has_product_detail_work()
                ):
                    scraper.scrape_product_details(rescan_all=not args.resume)
                else:
                    logging.info("Skipping product detail collection – nothing left to do")
    finally:
        scraper.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
