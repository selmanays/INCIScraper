"""Command line interface for running the INCIDecoder scraper."""

import argparse
import logging
import sys
from pathlib import Path

# Add src to Python path for imports
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

from inciscraper import INCIScraper


def setup_logging(log_level: str = "INFO"):
    """Set up logging configuration."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Create logs directory if it doesn't exist
    logs_dir = Path("data/logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    
    # Configure logging
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(logs_dir / "inciscraper.log"),
            logging.StreamHandler(sys.stdout)
        ]
    )


def main():
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="INCIDecoder scraper for collecting cosmetic product data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --sample-data                    # Run with sample data
  %(prog)s --max-workers 4                  # Use 4 parallel workers
  %(prog)s --batch-size 50                  # Process 50 items per batch
  %(prog)s --skip-images                    # Skip image downloading
  %(prog)s --log-level DEBUG                # Enable debug logging
        """
    )
    
    # Data source options
    parser.add_argument(
        "--sample-data",
        action="store_true",
        help="Use sample data for testing (default: False)"
    )
    
    # Performance options
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Maximum number of parallel workers for HTTP requests (default: 1)"
    )
    
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of items to process in each batch (default: 50)"
    )
    
    parser.add_argument(
        "--image-workers",
        type=int,
        default=4,
        help="Number of parallel workers for image processing (default: 4)"
    )
    
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Skip downloading and processing images (default: False)"
    )
    
    # Logging options
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set the logging level (default: INFO)"
    )
    
    # Database options
    parser.add_argument(
        "--db-path",
        type=str,
        default="data/incidecoder.db",
        help="Path to SQLite database file (default: data/incidecoder.db)"
    )
    
    args = parser.parse_args()
    
    # Set up logging
    setup_logging(args.log_level)
    
    # Initialize scraper
    try:
        scraper = INCIScraper(
            db_path=args.db_path,
            max_workers=args.max_workers,
            batch_size=args.batch_size,
            image_workers=args.image_workers,
            skip_images=args.skip_images
        )
        
        # Run scraper
        if args.sample_data:
            print("🚀 Starting INCIScraper with sample data...")
            scraper.run_sample_data()
        else:
            print("🚀 Starting INCIScraper...")
            scraper.run()
            
        print("✅ Scraping completed successfully!")
        
    except KeyboardInterrupt:
        print("\n⚠️  Scraping interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        logging.exception("Unexpected error occurred")
        sys.exit(1)
    finally:
        # Ensure scraper is properly closed
        if 'scraper' in locals():
            scraper.close()


if __name__ == "__main__":
    main()
