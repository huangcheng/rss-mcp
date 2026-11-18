from __future__ import annotations

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from rss_mcp_scheduler.workflows import RefreshAllWorkflow, RefreshFeedWorkflow

TASK_QUEUE = "test-rss-refresh"


@activity.defn(name="list_subscriptions")
async def fake_list() -> list[dict]:
    return [
        {"id": 1, "url": "https://ok.xml", "disabled": False},
        {"id": 2, "url": "https://bad.xml", "disabled": False},
        {"id": 3, "url": "https://off.xml", "disabled": True},
    ]


@activity.defn(name="fetch_feed")
async def fake_fetch(subscription_id: int) -> dict:
    if subscription_id == 2:
        raise ApplicationError("HTTP 502")
    return {"subscription_id": subscription_id, "status": "ok", "upserted": 1}


@pytest_asyncio.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        yield environment


@pytest.mark.asyncio
async def test_refresh_all_fans_out_and_isolates_failures(env):
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[RefreshAllWorkflow, RefreshFeedWorkflow],
        activities=[fake_list, fake_fetch],
    ):
        summary = await env.client.execute_workflow(
            RefreshAllWorkflow.run,
            id="test-refresh-all",
            task_queue=TASK_QUEUE,
        )
    assert summary == {"total": 3, "ok": 1, "error": 1, "skipped_disabled": 1}


@pytest.mark.asyncio
async def test_refresh_feed_retries_then_succeeds(env):
    attempts = {"n": 0}

    @activity.defn(name="fetch_feed")
    async def flaky_fetch(subscription_id: int) -> dict:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ApplicationError("transient")
        return {"subscription_id": subscription_id, "status": "ok", "upserted": 2}

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[RefreshFeedWorkflow],
        activities=[flaky_fetch],
    ):
        row = await env.client.execute_workflow(
            RefreshFeedWorkflow.run,
            5,
            id="test-refresh-feed",
            task_queue=TASK_QUEUE,
        )
    assert row["status"] == "ok"
    assert attempts["n"] == 3