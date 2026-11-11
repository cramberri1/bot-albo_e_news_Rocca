"""Run the Actions worker with bounded, POSIX signal-based shutdown."""

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import signal
import sys

from runtime_policy import SHUTDOWN_GRACE_SECONDS
from workflow_recovery import worker_seconds


async def supervise(command, *, duration, grace=SHUTDOWN_GRACE_SECONDS):
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def request_stop(signum):
        if not stop.done():
            stop.set_result(signum)

    signals = (signal.SIGINT, signal.SIGTERM)
    for signum in signals:
        loop.add_signal_handler(signum, request_stop, signum)
    try:
        worker = await asyncio.create_subprocess_exec(*command, start_new_session=True)
        exited = asyncio.create_task(worker.wait())
        try:
            await asyncio.wait((exited, stop), timeout=duration,
                               return_when=asyncio.FIRST_COMPLETED)
            if worker.returncode is None:
                signum = stop.result() if stop.done() else signal.SIGINT
                print(f"Arresto worker: {signum.name}, grazia {grace}s.", flush=True)
                with suppress(ProcessLookupError):
                    worker.send_signal(signum)
                await asyncio.wait((exited,), timeout=grace)
        finally:
            if worker.returncode is None:
                print("Arresto forzato del gruppo worker.", flush=True)
                with suppress(ProcessLookupError):
                    os.killpg(worker.pid, signal.SIGKILL)
            code = await exited
        return 128 - code if code < 0 else code
    finally:
        stop.cancel()
        for signum in signals:
            loop.remove_signal_handler(signum)


def main():
    try:
        duration = worker_seconds(os.environ.get("BOT_SECONDS_INPUT"))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    bot = Path(__file__).with_name("bot.py")
    return asyncio.run(supervise([sys.executable, str(bot)], duration=duration))


if __name__ == "__main__":
    raise SystemExit(main())
