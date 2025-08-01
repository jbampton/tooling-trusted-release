# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""worker.py - Task worker process for ATR"""

# TODO: If started is older than some threshold and status
# is active but the pid is no longer running, we can revert
# the task to status='QUEUED'. For this to work, ideally we
# need to check wall clock time as well as CPU time.

import asyncio
import datetime
import inspect
import os
import resource
import signal
import traceback
from typing import Any, Final

import sqlmodel

import atr.db as db
import atr.log as log
import atr.models.results as results
import atr.models.sql as sql
import atr.tasks as tasks
import atr.tasks.checks as checks
import atr.tasks.task as task

# Resource limits, 5 minutes and 1GB
# _CPU_LIMIT_SECONDS: Final = 300
_MEMORY_LIMIT_BYTES: Final = 1024 * 1024 * 1024

# # Create tables if they don't exist
# SQLModel.metadata.create_all(engine)


def main() -> None:
    """Main entry point."""
    import atr.config as config

    conf = config.get()
    if os.path.isdir(conf.STATE_DIR):
        os.chdir(conf.STATE_DIR)

    _setup_logging()

    log.info(f"Starting worker process with pid {os.getpid()}")

    tasks: list[asyncio.Task] = []

    async def _handle_signal(signum: int) -> None:
        log.info(f"Received signal {signum}, shutting down...")

        await db.shutdown_database()

        for t in tasks:
            t.cancel()

        log.debug("Cancelled all running tasks")
        asyncio.get_event_loop().stop()
        log.debug("Stopped event loop")

    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda signum, frame: asyncio.create_task(_handle_signal(signum)))

    _worker_resources_limit_set()

    async def _start() -> None:
        await asyncio.create_task(db.init_database_for_worker())
        tasks.append(asyncio.create_task(_worker_loop_run()))
        await asyncio.gather(*tasks)

    asyncio.run(_start())

    # If the worker decides to stop running (see #230 in _worker_loop_run()), shutdown the database gracefully
    asyncio.run(db.shutdown_database())
    log.info("Exiting worker process")


def _setup_logging() -> None:
    import logging

    # Configure logging
    log_format = "[%(asctime)s.%(msecs)03d] [%(process)d] [%(levelname)s] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(filename="atr-worker.log", format=log_format, datefmt=date_format, level=logging.INFO)


# Task functions


async def _task_next_claim() -> tuple[int, str, list[str] | dict[str, Any]] | None:
    """
    Attempt to claim the oldest unclaimed task.
    Returns (task_id, task_type, task_args) if successful.
    Returns None if no tasks are available.
    """
    async with db.session() as data:
        async with data.begin():
            # Get the ID of the oldest queued task
            oldest_queued_task = (
                sqlmodel.select(sql.Task.id)
                .where(sql.Task.status == task.QUEUED)
                .order_by(sql.validate_instrumented_attribute(sql.Task.added).asc())
                .limit(1)
            )

            # Use an UPDATE with a WHERE clause to atomically claim the task
            # This ensures that only one worker can claim a specific task
            now = datetime.datetime.now(datetime.UTC)
            update_stmt = (
                sqlmodel.update(sql.Task)
                .where(sqlmodel.and_(sql.Task.id == oldest_queued_task, sql.Task.status == task.QUEUED))
                .values(status=task.ACTIVE, started=now, pid=os.getpid())
                .returning(
                    sql.validate_instrumented_attribute(sql.Task.id),
                    sql.validate_instrumented_attribute(sql.Task.task_type),
                    sql.validate_instrumented_attribute(sql.Task.task_args),
                )
            )

            result = await data.execute(update_stmt)
            claimed_task = result.first()

            if claimed_task:
                task_id, task_type, task_args = claimed_task
                log.info(f"Claimed task {task_id} ({task_type}) with args {task_args}")
                return task_id, task_type, task_args

            return None


async def _task_process(task_id: int, task_type: str, task_args: list[str] | dict[str, Any]) -> None:
    """Process a claimed task."""
    log.info(f"Processing task {task_id} ({task_type}) with raw args {task_args}")
    try:
        task_type_member = sql.TaskType(task_type)
    except ValueError as e:
        log.error(f"Invalid task type: {task_type}")
        await _task_result_process(task_id, None, task.FAILED, str(e))
        return

    task_results: results.Results | None
    try:
        handler = tasks.resolve(task_type_member)
        sig = inspect.signature(handler)
        params = list(sig.parameters.values())

        # Check whether the handler is a check handler
        if (len(params) == 1) and (params[0].annotation == checks.FunctionArguments):
            log.debug(f"Handler {handler.__name__} expects checks.FunctionArguments, fetching full task details")
            async with db.session() as data:
                task_obj = await data.task(id=task_id).demand(
                    ValueError(f"Task {task_id} disappeared during processing")
                )

            # Validate required fields from the Task object itself
            if task_obj.project_name is None:
                raise ValueError(f"Task {task_id} is missing required project_name")
            if task_obj.version_name is None:
                raise ValueError(f"Task {task_id} is missing required version_name")
            if task_obj.revision_number is None:
                raise ValueError(f"Task {task_id} is missing required revision_number")

            if not isinstance(task_args, dict):
                raise TypeError(
                    f"Task {task_id} ({task_type}) has non-dict raw args"
                    f" {task_args} which should represent keyword_args"
                )

            async def recorder_factory() -> checks.Recorder:
                return await checks.Recorder.create(
                    checker=handler,
                    project_name=task_obj.project_name or "",
                    version_name=task_obj.version_name or "",
                    revision_number=task_obj.revision_number or "",
                    primary_rel_path=task_obj.primary_rel_path,
                )

            function_arguments = checks.FunctionArguments(
                recorder=recorder_factory,
                asf_uid=task_obj.asf_uid,
                project_name=task_obj.project_name or "",
                version_name=task_obj.version_name or "",
                revision_number=task_obj.revision_number,
                primary_rel_path=task_obj.primary_rel_path,
                extra_args=task_args,
            )
            log.debug(f"Calling {handler.__name__} with structured arguments: {function_arguments}")
            handler_result = await handler(function_arguments)
        else:
            # Otherwise, it's not a check handler
            handler_result = await handler(task_args)

        task_results = handler_result
        status = task.COMPLETED
        error = None
    except Exception as e:
        task_results = None
        status = task.FAILED
        error_details = traceback.format_exc()
        log.error(f"Task {task_id} failed processing: {error_details}")
        error = str(e)
    await _task_result_process(task_id, task_results, status, error)


async def _task_result_process(
    task_id: int, task_results: results.Results | None, status: sql.TaskStatus, error: str | None = None
) -> None:
    """Process and store task results in the database."""
    async with db.session() as data:
        async with data.begin():
            # Find the task by ID
            task_obj = await data.task(id=task_id).get()
            if task_obj:
                # Update task properties
                task_obj.status = status
                task_obj.completed = datetime.datetime.now(datetime.UTC)
                task_obj.result = task_results

                if (status == task.FAILED) and error:
                    task_obj.error = error


# Worker functions


async def _worker_loop_run() -> None:
    """Main worker loop."""
    processed = 0
    max_to_process = 10
    while True:
        try:
            task = await _task_next_claim()
            if task:
                task_id, task_type, task_args = task
                await _task_process(task_id, task_type, task_args)
                processed += 1
                # Only process max_to_process tasks and then exit
                # This prevents memory leaks from accumulating
                # Another worker will be started automatically when one exits
                if processed >= max_to_process:
                    break
            else:
                # No tasks available, wait 100ms before checking again
                await asyncio.sleep(0.1)
        except Exception:
            # TODO: Should probably be more robust about this
            log.exception("Worker loop error")
            await asyncio.sleep(1)


def _worker_resources_limit_set() -> None:
    """Set CPU and memory limits for this process."""
    # # Set CPU time limit
    # try:
    #     resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS))
    #     log.info(f"Set CPU time limit to {CPU_LIMIT_SECONDS} seconds")
    # except ValueError as e:
    #     log.warning(f"Could not set CPU time limit: {e}")

    # Set memory limit
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES))
        log.info(f"Set memory limit to {_MEMORY_LIMIT_BYTES} bytes")
    except ValueError as e:
        log.warning(f"Could not set memory limit: {e}")


if __name__ == "__main__":
    log.info("Starting ATR worker...")
    try:
        main()
    except Exception as e:
        with open("atr-worker-error.log", "a") as f:
            f.write(f"{datetime.datetime.now(datetime.UTC)}: {e}\n")
            f.flush()
