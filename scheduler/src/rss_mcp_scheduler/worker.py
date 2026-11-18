from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.worker import Worker

from rss_mcp_scheduler.activities import McpActivities
from rss_mcp_scheduler.config import SchedulerSettings, load_settings
from rss_mcp_scheduler.workflows import RefreshAllWorkflow, RefreshFeedWorkflow

logger = logging.getLogger("rss_mcp_scheduler")

_SCHEDULE_ID = "rss-mcp-refresh-all"


async def _ensure_schedule(client: Client, settings: SchedulerSettings) -> None:
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            RefreshAllWorkflow.run,
            id=_SCHEDULE_ID,
            task_queue=settings.task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(every=timedelta(minutes=settings.refresh_interval_minutes))
            ]
        ),
    )
    try:
        await client.create_schedule(_SCHEDULE_ID, schedule)
        logger.info("created schedule %s (every %sm)", _SCHEDULE_ID, settings.refresh_interval_minutes)
    except ScheduleAlreadyRunningError:
        logger.info("schedule %s already exists; keeping it", _SCHEDULE_ID)


async def _run(settings: SchedulerSettings) -> None:
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await _ensure_schedule(client, settings)
    activities = McpActivities(settings)
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[RefreshAllWorkflow, RefreshFeedWorkflow],
        activities=[activities.list_subscriptions, activities.fetch_feed],
    )
    logger.info("worker polling %s (mcp: %s)", settings.task_queue, settings.mcp_url)
    await worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(_run(load_settings()))


if __name__ == "__main__":
    main()