"""Monitoring and performance tracking utilities for INCIScraper."""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

LOGGER = logging.getLogger(__name__)


class MonitoringMixin:
    """Mixin providing performance monitoring and progress tracking."""
    
    def __init__(self):
        # Performance tracking
        self._start_time: Optional[float] = None
        self._stage_start_time: Optional[float] = None
        self._current_stage: str = ""
        
        # Request metrics
        self._total_requests = 0
        self._successful_requests = 0
        self._failed_requests = 0
        self._request_times: List[float] = []
        
        # Stage metrics
        self._stage_metrics: Dict[str, Dict] = {}
        
        # Progress tracking
        self._total_items = 0
        self._processed_items = 0
        self._last_progress_log_time = 0.0
    
    def _start_monitoring(self) -> None:
        """Start overall monitoring session."""
        self._start_time = time.perf_counter()
        LOGGER.info("Performance monitoring started")
    
    def _start_stage(self, stage_name: str, total_items: int = 0) -> None:
        """Start monitoring a specific stage."""
        if self._stage_start_time is not None:
            self._end_stage()
        
        self._current_stage = stage_name
        self._stage_start_time = time.perf_counter()
        self._total_items = total_items
        self._processed_items = 0
        
        LOGGER.info("Starting stage: %s (total items: %s)", stage_name, total_items or "unknown")
    
    def _end_stage(self) -> Dict:
        """End current stage monitoring and return metrics."""
        if self._stage_start_time is None:
            return {}
        
        elapsed = time.perf_counter() - self._stage_start_time
        items_per_second = self._processed_items / elapsed if elapsed > 0 else 0
        
        stage_metrics = {
            'stage': self._current_stage,
            'elapsed_seconds': elapsed,
            'total_items': self._total_items,
            'processed_items': self._processed_items,
            'items_per_second': items_per_second,
            'remaining_items': max(0, self._total_items - self._processed_items),
            'eta_seconds': (self._total_items - self._processed_items) / items_per_second if items_per_second > 0 else 0,
        }
        
        self._stage_metrics[self._current_stage] = stage_metrics
        
        LOGGER.info(
            "Stage %s completed: %.2fs, %s items (%.2f items/sec), ETA: %.1fs",
            self._current_stage,
            elapsed,
            self._processed_items,
            items_per_second,
            stage_metrics['eta_seconds']
        )
        
        self._stage_start_time = None
        self._current_stage = ""
        return stage_metrics
    
    def _record_request(self, success: bool, request_time: float) -> None:
        """Record a request for performance tracking."""
        self._total_requests += 1
        if success:
            self._successful_requests += 1
        else:
            self._failed_requests += 1
        
        self._request_times.append(request_time)
        
        # Keep only last 1000 request times to avoid memory issues
        if len(self._request_times) > 1000:
            self._request_times = self._request_times[-1000:]
    
    def _update_progress(self, items_processed: int = 1) -> None:
        """Update progress for current stage."""
        self._processed_items += items_processed
        
        # Log progress every 10 seconds or every 10% completion
        current_time = time.perf_counter()
        if (current_time - self._last_progress_log_time > 10.0 or 
            (self._total_items > 0 and self._processed_items % max(1, self._total_items // 10) == 0)):
            
            if self._total_items > 0:
                progress_percent = (self._processed_items / self._total_items) * 100
                eta_seconds = self._get_current_stage_eta()
                LOGGER.info(
                    "Progress: %s/%s (%.1f%%) - ETA: %.1fs",
                    self._processed_items,
                    self._total_items,
                    progress_percent,
                    eta_seconds
                )
            else:
                LOGGER.info("Progress: %s items processed", self._processed_items)
            
            self._last_progress_log_time = current_time
    
    def _get_current_stage_eta(self) -> float:
        """Get estimated time remaining for current stage."""
        if (self._stage_start_time is None or 
            self._total_items == 0 or 
            self._processed_items == 0):
            return 0.0
        
        elapsed = time.perf_counter() - self._stage_start_time
        items_per_second = self._processed_items / elapsed if elapsed > 0 else 0
        remaining_items = max(0, self._total_items - self._processed_items)
        
        return remaining_items / items_per_second if items_per_second > 0 else 0.0
    
    def _get_performance_summary(self) -> Dict:
        """Get comprehensive performance summary."""
        total_elapsed = time.perf_counter() - self._start_time if self._start_time else 0.0
        
        # Calculate request statistics
        avg_request_time = sum(self._request_times) / len(self._request_times) if self._request_times else 0.0
        success_rate = self._successful_requests / self._total_requests if self._total_requests > 0 else 0.0
        
        return {
            'total_elapsed_seconds': total_elapsed,
            'total_requests': self._total_requests,
            'successful_requests': self._successful_requests,
            'failed_requests': self._failed_requests,
            'success_rate': success_rate,
            'average_request_time': avg_request_time,
            'requests_per_second': self._total_requests / total_elapsed if total_elapsed > 0 else 0.0,
            'stage_metrics': self._stage_metrics,
            'current_stage_eta': self._get_current_stage_eta(),
        }
    
    def _log_performance_summary(self) -> None:
        """Log comprehensive performance summary."""
        summary = self._get_performance_summary()
        
        LOGGER.info("=== Performance Summary ===")
        LOGGER.info("Total elapsed time: %.2fs", summary['total_elapsed_seconds'])
        LOGGER.info("Total requests: %s (%.1f%% success rate)", 
                   summary['total_requests'], summary['success_rate'] * 100)
        LOGGER.info("Average request time: %.3fs", summary['average_request_time'])
        LOGGER.info("Requests per second: %.2f", summary['requests_per_second'])
        
        for stage_name, metrics in summary['stage_metrics'].items():
            LOGGER.info("Stage %s: %.2fs, %.2f items/sec", 
                       stage_name, metrics['elapsed_seconds'], metrics['items_per_second'])
        
        if self._current_stage:
            LOGGER.info("Current stage %s ETA: %.1fs", 
                       self._current_stage, summary['current_stage_eta'])
