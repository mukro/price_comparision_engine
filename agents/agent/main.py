"""Entry point for the PCE Data Quality Agent.

Runs as a scheduled service (via cron or systemd) or can be triggered manually.
"""
import os
import sys
import logging
import time
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("pce-agent")

from agent.graph import get_graph
from agent.state import AgentState


def run_entity_resolution(batch_size: int = 50) -> Dict:
    """Run the entity resolution pipeline."""
    logger.info(f"Starting entity resolution batch (size={batch_size})")

    graph = get_graph()

    initial_state: AgentState = {
        "batch_size": batch_size,
        "mode": "entity_resolution",
        "pending_offers": [],
        "candidate_products": [],
        "decisions": [],
        "vendor_health_checks": [],
        "processed_count": 0,
        "escalated_count": 0,
        "errors": [],
        "summary": None,
    }

    start_time = time.time()
    result = graph.invoke(initial_state)
    elapsed = time.time() - start_time

    summary = result.get("summary", "No summary generated")
    logger.info(f"Entity resolution completed in {elapsed:.1f}s: {summary}")

    return {
        "mode": "entity_resolution",
        "summary": summary,
        "processed": result.get("processed_count", 0),
        "escalated": result.get("escalated_count", 0),
        "errors": len(result.get("errors", [])),
        "elapsed_seconds": elapsed,
        "timestamp": datetime.utcnow().isoformat(),
    }


def run_selector_health_check() -> Dict:
    """Run the selector health check pipeline."""
    logger.info("Starting selector health check")

    graph = get_graph()

    initial_state: AgentState = {
        "batch_size": 0,
        "mode": "selector_health",
        "pending_offers": [],
        "candidate_products": [],
        "decisions": [],
        "vendor_health_checks": [],
        "processed_count": 0,
        "escalated_count": 0,
        "errors": [],
        "summary": None,
    }

    start_time = time.time()
    result = graph.invoke(initial_state)
    elapsed = time.time() - start_time

    summary = result.get("summary", "No summary generated")
    logger.info(f"Selector health check completed in {elapsed:.1f}s: {summary}")

    return {
        "mode": "selector_health",
        "summary": summary,
        "vendors_checked": len(result.get("vendor_health_checks", [])),
        "errors": len(result.get("errors", [])),
        "elapsed_seconds": elapsed,
        "timestamp": datetime.utcnow().isoformat(),
    }


def run_full_pipeline(batch_size: int = 50) -> Dict:
    """Run both entity resolution and selector health check."""
    logger.info("Starting full data quality pipeline")

    graph = get_graph()

    initial_state: AgentState = {
        "batch_size": batch_size,
        "mode": "both",
        "pending_offers": [],
        "candidate_products": [],
        "decisions": [],
        "vendor_health_checks": [],
        "processed_count": 0,
        "escalated_count": 0,
        "errors": [],
        "summary": None,
    }

    start_time = time.time()
    result = graph.invoke(initial_state)
    elapsed = time.time() - start_time

    summary = result.get("summary", "No summary generated")
    logger.info(f"Full pipeline completed in {elapsed:.1f}s: {summary}")

    return {
        "mode": "both",
        "summary": summary,
        "processed": result.get("processed_count", 0),
        "escalated": result.get("escalated_count", 0),
        "vendors_checked": len(result.get("vendor_health_checks", [])),
        "errors": len(result.get("errors", [])),
        "elapsed_seconds": elapsed,
        "timestamp": datetime.utcnow().isoformat(),
    }


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="PCE Data Quality Agent")
    parser.add_argument(
        "mode",
        choices=["entity", "selectors", "full"],
        default="full",
        nargs="?",
        help="Pipeline mode to run",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of pending offers to process per run",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run in continuous loop (for systemd service)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Seconds between runs in loop mode (default: 300 = 5 min)",
    )

    args = parser.parse_args()

    logger.info(f"PCE Data Quality Agent starting | mode={args.mode} | batch={args.batch_size}")

    if args.loop:
        logger.info(f"Loop mode: running every {args.interval}s")
        while True:
            try:
                if args.mode == "entity":
                    run_entity_resolution(args.batch_size)
                elif args.mode == "selectors":
                    run_selector_health_check()
                else:
                    run_full_pipeline(args.batch_size)
            except Exception as e:
                logger.error(f"Pipeline error: {e}", exc_info=True)

            logger.info(f"Sleeping for {args.interval}s...")
            time.sleep(args.interval)
    else:
        # Single run
        if args.mode == "entity":
            result = run_entity_resolution(args.batch_size)
        elif args.mode == "selectors":
            result = run_selector_health_check()
        else:
            result = run_full_pipeline(args.batch_size)

        # Print JSON result for external tools
        import json
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
