"""Progress tracking and performance monitoring utilities."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Dict, Optional

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - optional dependency
    tqdm = None

LOGGER = logging.getLogger(__name__)


class MonitoringMixin:
    """Provide progress tracking and performance monitoring capabilities."""

    _performance_metrics: Dict[str, Any]
    _progress_bars: Dict[str, Any]

    def __init__(self) -> None:
        """Initialize monitoring state."""
        self._performance_metrics = defaultdict(list)
        self._progress_bars = {}

    def start_timer(self, operation: str) -> None:
        """Start timing an operation."""
        self._performance_metrics[f"{operation}_start"] = time.time()

    def end_timer(self, operation: str) -> float:
        """End timing an operation and return duration."""
        start_key = f"{operation}_start"
        if start_key not in self._performance_metrics:
            LOGGER.warning("Timer for %s was not started", operation)
            return 0.0
        
        start_time = self._performance_metrics[start_key]
        duration = time.time() - start_time
        self._performance_metrics[f"{operation}_duration"] = duration
        self._performance_metrics[f"{operation}_count"] = self._performance_metrics.get(f"{operation}_count", 0) + 1
        
        # Store in history for averaging
        history_key = f"{operation}_history"
        if history_key not in self._performance_metrics:
            self._performance_metrics[history_key] = []
        self._performance_metrics[history_key].append(duration)
        
        # Keep only last 100 measurements
        if len(self._performance_metrics[history_key]) > 100:
            self._performance_metrics[history_key] = self._performance_metrics[history_key][-100:]
        
        return duration

    def get_average_time(self, operation: str) -> float:
        """Get average time for an operation."""
        history_key = f"{operation}_history"
        history = self._performance_metrics.get(history_key, [])
        return sum(history) / len(history) if history else 0.0

    def get_operation_count(self, operation: str) -> int:
        """Get total count for an operation."""
        return self._performance_metrics.get(f"{operation}_count", 0)

    def create_progress_bar(self, name: str, total: int, desc: str = "") -> None:
        """Create a progress bar for tracking progress."""
        if tqdm is None:
            LOGGER.info("tqdm not available, using basic logging for progress tracking")
            return
        
        if name in self._progress_bars:
            self._progress_bars[name].close()
        
        self._progress_bars[name] = tqdm(
            total=total,
            desc=desc or name,
            unit="item",
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]"
        )

    def update_progress(self, name: str, increment: int = 1) -> None:
        """Update progress bar."""
        if name in self._progress_bars and self._progress_bars[name] is not None:
            self._progress_bars[name].update(increment)

    def close_progress_bar(self, name: str) -> None:
        """Close and cleanup progress bar."""
        if name in self._progress_bars and self._progress_bars[name] is not None:
            self._progress_bars[name].close()
            del self._progress_bars[name]

    def close_all_progress_bars(self) -> None:
        """Close all progress bars."""
        for name in list(self._progress_bars.keys()):
            self.close_progress_bar(name)

    def log_performance_summary(self) -> None:
        """Log a summary of performance metrics."""
        LOGGER.info("Performance Summary:")
        LOGGER.info("-" * 50)
        
        operations = set()
        for key in self._performance_metrics.keys():
            if key.endswith("_duration"):
                operation = key.replace("_duration", "")
                operations.add(operation)
        
        for operation in operations:
            count = self.get_operation_count(operation)
            avg_time = self.get_average_time(operation)
            total_time = self._performance_metrics.get(f"{operation}_duration", 0.0)
            
            LOGGER.info(
                "%s: %d operations, avg %.2fs, total %.2fs",
                operation.title(),
                count,
                avg_time,
                total_time
            )
        
        LOGGER.info("-" * 50)

    def estimate_remaining_time(self, operation: str, completed: int, total: int) -> float:
        """Estimate remaining time for an operation."""
        if completed <= 0 or total <= 0:
            return 0.0
        
        avg_time = self.get_average_time(operation)
        remaining_items = total - completed
        return avg_time * remaining_items

    def get_throughput(self, operation: str) -> float:
        """Get operations per second for an operation."""
        history = self._performance_metrics.get(f"{operation}_history", [])
        if not history:
            return 0.0
        
        avg_time = sum(history) / len(history)
        return 1.0 / avg_time if avg_time > 0 else 0.0
