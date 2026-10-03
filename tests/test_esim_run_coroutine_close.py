import unittest
from unittest.mock import AsyncMock, patch

from control.app import main


class EsimRunCoroutineCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_by_engine_gate_closes_the_coroutine(self):
        async def work():
            return 42  # pragma: no cover - never awaited on the rejection path

        coro = work()
        with patch.object(main.asyncio, "to_thread",
                          side_effect=main.HTTPException(409, "busy")):
            with self.assertRaises(main.HTTPException):
                await main._esim_run("Reader-1", 0, coro)
        # close() ran: awaiting the coroutine again raises RuntimeError instead of
        # executing the body (and CPython's "never awaited" warning stays silent).
        with self.assertRaises(RuntimeError):
            await coro

    async def test_successful_run_awaits_normally(self):
        async def work():
            return "done"

        with patch.object(main.asyncio, "to_thread", new=AsyncMock()), \
             patch.object(main.hub, "reader_lock"):
            result = await main._esim_run("Reader-1", 0, work())
        self.assertEqual(result, "done")


if __name__ == "__main__":
    unittest.main()
