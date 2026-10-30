import time

import pytest
from loguru import logger

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.schedule.schedule import Schedule
from src.autobots.schedule.trigger import CronTriggerModel


class MockAction:
    async def run_async(self, data_block: DataBlock):
        logger.info(data_block.text)
        return "ok"


@pytest.mark.asyncio
async def test_cron_trigger_happy_path(set_test_settings, ctx, db, user):
    Schedule.start_scheduler()
    # ctx = Context()

    try:
        jobs = Schedule().list_schedule()
        for job in jobs:
            Schedule().delete_schedule(job.id)
        jobs = Schedule().list_schedule()
        assert len(jobs) == 0

        data_block = DataBlock(text=f"xyz+{time.time()}")

        job1 = Schedule().create_schedule(  # noqa F841
            ctx=ctx,
            func=MockAction().run_async,
            # trigger=IntervalTrigger(seconds=300),
            trigger=CronTriggerModel(hour=12),
            kwargs={"data_block": data_block.model_dump(exclude_none=True)},
            name="job1",
        )

        job2 = Schedule().create_schedule(
            ctx=ctx,
            func=MockAction().run_async,
            # trigger=IntervalTrigger(seconds=300),
            trigger=CronTriggerModel(hour=12),
            kwargs={"data_block": data_block.model_dump(exclude_none=True)},
            name="job2",
        )
        Schedule().modify_schedule(
            ctx=ctx,
            job_id=job2.id,
            kwargs={"data_block": DataBlock(text="modified job").model_dump(exclude_none=True)},
        )

        jobs = Schedule().list_schedule()
        assert len(jobs) == 2
        jobs_dict = {job.id: job for job in jobs}
        assert jobs_dict.get(job2.id).kwargs.get("data_block").get("text") == "modified job"

        for job in jobs:
            Schedule().delete_schedule(job.id)
        jobs = Schedule().list_schedule()
        assert len(jobs) == 0

    finally:
        # Schedule.stop_scheduler()
        assert True
