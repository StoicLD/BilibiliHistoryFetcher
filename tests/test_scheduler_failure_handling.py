import asyncio
import unittest
from unittest.mock import AsyncMock

from scripts.scheduler_manager import SchedulerManager


class FakeSchedulerDB:
    def get_main_task_by_id(self, task_id):
        return {"task_id": task_id, "enabled": True}

    def is_main_task(self, task_id):
        return task_id == "fetch_history"

    def get_sub_tasks(self, task_id):
        return [
            {"task_id": "import_data", "enabled": True, "sequence_number": 1},
            {"task_id": "send_daily_report", "enabled": True, "sequence_number": 2},
        ]


class SchedulerFailureHandlingTests(unittest.TestCase):
    def make_manager(self, stop_on_failure):
        manager = SchedulerManager.__new__(SchedulerManager)
        manager.tasks = {"fetch_history": {"name": "获取历史记录"}}
        manager.db = FakeSchedulerDB()
        manager.stop_on_failure = stop_on_failure
        manager.current_chain = None
        manager._build_chain_from_task = lambda task_id: [task_id]
        return manager

    def test_stops_before_report_when_subtask_fails(self):
        manager = self.make_manager(stop_on_failure=True)
        manager._execute_single_task = AsyncMock(side_effect=[True, False])

        result = asyncio.run(manager.execute_task_chain("fetch_history"))

        self.assertFalse(result)
        self.assertEqual(manager._execute_single_task.await_count, 2)

    def test_can_continue_when_stop_on_failure_is_disabled(self):
        manager = self.make_manager(stop_on_failure=False)
        manager._execute_single_task = AsyncMock(side_effect=[True, False, True])

        result = asyncio.run(manager.execute_task_chain("fetch_history"))

        self.assertTrue(result)
        self.assertEqual(manager._execute_single_task.await_count, 3)


if __name__ == "__main__":
    unittest.main()
