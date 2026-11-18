from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from rss_mcp_scheduler.activities import McpActivities  # noqa: F401

_FEED_RETRY = RetryPolicy(
    initial_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(hours=1),
    maximum_attempts=5,
)


@workflow.defn
class RefreshFeedWorkflow:
    """One feed, one durable retry history."""

    @workflow.run
    async def run(self, subscription_id: int) -> dict[str, Any]:
        return await workflow.execute_activity(
            "fetch_feed",
            subscription_id,
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=_FEED_RETRY,
        )


@workflow.defn
class RefreshAllWorkflow:
    """Fan out over subscriptions; a failing feed never fails the batch."""

    @workflow.run
    async def run(self) -> dict[str, Any]:
        subs = await workflow.execute_activity(
            "list_subscriptions",
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        parent_id = workflow.info().workflow_id
        task_queue = workflow.info().task_queue
        handles = []
        skipped_disabled = 0
        for sub in subs:
            if sub.get("disabled"):
                skipped_disabled += 1
                continue
            handle = await workflow.start_child_workflow(
                RefreshFeedWorkflow.run,
                sub["id"],
                id=f"{parent_id}-feed-{sub['id']}",
                task_queue=task_queue,
            )
            handles.append(handle)

        ok = 0
        error = 0
        for handle in handles:
            try:
                row = await handle
            except ChildWorkflowError:
                error += 1
                continue
            if row.get("status") in ("ok", "not_modified"):
                ok += 1
            else:
                error += 1
        return {
            "total": len(subs),
            "ok": ok,
            "error": error,
            "skipped_disabled": skipped_disabled,
        }