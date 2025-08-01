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

import asyncio
import pathlib
from typing import Any

import aiofiles.os
import aioshutil

import atr.log as log
import atr.models.results as results
import atr.models.schema as schema
import atr.revision as revision
import atr.tasks.checks as checks


class SvnImport(schema.Strict):
    """Arguments for the task to import files from SVN."""

    svn_url: str
    revision: str
    target_subdirectory: str | None
    project_name: str
    version_name: str
    asf_uid: str


class SvnImportError(Exception):
    """Custom exception for SVN import failures."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


@checks.with_model(SvnImport)
async def import_files(args: SvnImport) -> results.Results | None:
    """Import files from SVN into a draft release candidate revision."""
    try:
        result_message = await _import_files_core(args)
        return results.SvnImportFiles(
            kind="svn_import",
            msg=result_message,
        )
    except SvnImportError as e:
        log.error(f"SVN import failed: {e.details}")
        raise
    except Exception:
        log.exception("Unexpected error during SVN import task")
        raise


async def _import_files_core(args: SvnImport) -> str:
    """Core logic to perform the SVN export."""
    log.info(f"Starting SVN import for {args.project_name}-{args.version_name}")
    # We have to use a temporary directory otherwise SVN thinks it's a pegged revision
    temp_export_dir_name = ".svn-export.tmp"

    description = "Import of files from subversion"
    async with revision.create_and_manage(
        args.project_name, args.version_name, args.asf_uid, description=description
    ) as creating:
        # Uses creating.new after this block
        log.debug(f"Created revision directory: {creating.interim_path}")

        final_target_path = creating.interim_path
        if args.target_subdirectory:
            final_target_path = creating.interim_path / args.target_subdirectory
            # Validate that final_target_path is a subdirectory of new_revision_dir
            if not final_target_path.is_relative_to(creating.interim_path):
                raise SvnImportError(
                    f"Target subdirectory {args.target_subdirectory} is not a subdirectory of {creating.interim_path}"
                )
            await aiofiles.os.makedirs(final_target_path, exist_ok=True)

        temp_export_path = creating.interim_path / temp_export_dir_name

        svn_command = [
            "svn",
            "export",
            "--non-interactive",
            "--trust-server-cert-failures",
            "unknown-ca,cn-mismatch",
            "-r",
            args.revision,
            "--",
            args.svn_url,
            str(temp_export_path),
        ]

        await _import_files_core_run_svn_export(svn_command, temp_export_path)

        # Move files from temp export path to final target path
        # We only have to do this to avoid the SVN pegged revision issue
        log.info(f"Moving exported files from {temp_export_path} to {final_target_path}")
        for item_name in await aiofiles.os.listdir(temp_export_path):
            source_item = temp_export_path / item_name
            destination_item = final_target_path / item_name
            try:
                await aioshutil.move(str(source_item), str(destination_item))
            except FileExistsError:
                log.warning(f"Item {destination_item} already exists, skipping move for {item_name}")
            except Exception as move_err:
                log.error(f"Error moving {source_item} to {destination_item}: {move_err}")
        await aiofiles.os.rmdir(temp_export_path)
        log.info(f"Removed temporary export directory: {temp_export_path}")

    if creating.new is None:
        raise SvnImportError("Internal error: New revision not found")
    return f"Successfully imported files from SVN into revision {creating.new.number}"


async def _import_files_core_run_svn_export(svn_command: list[str], temp_export_path: pathlib.Path) -> None:
    """Execute the svn export command and handle errors."""
    log.info(f"Executing SVN command: {' '.join(svn_command)}")

    timeout_seconds = 600
    try:
        process = await asyncio.create_subprocess_exec(
            *svn_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)

        stdout_str = stdout.decode("utf-8", errors="ignore").strip() if stdout else ""
        stderr_str = stderr.decode("utf-8", errors="ignore").strip() if stderr else ""

        if process.returncode != 0:
            log.error(f"SVN export failed with code {process.returncode}")
            log.error(f"SVN stderr: {stderr_str}")
            log.error(f"SVN stdout: {stdout_str[:1000]}")
            raise SvnImportError(
                f"SVN export failed with code {process.returncode}",
                details={"returncode": process.returncode, "stderr": stderr_str, "stdout": stdout_str[:1000]},
            )

        log.info("SVN export to temporary directory successful")
        if stdout_str:
            log.debug(f"SVN stdout: {stdout_str}")
        if stderr_str:
            log.warning(f"SVN stderr: {stderr_str}")

    except TimeoutError:
        log.error("SVN export command timed out after %d seconds", timeout_seconds)
        raise SvnImportError("SVN export command timed out")
    except FileNotFoundError:
        log.error("svn command not found. Is it installed and in PATH?")
        raise SvnImportError("svn command not found")
    except Exception as e:
        log.exception("Unexpected error during SVN export subprocess execution")
        raise SvnImportError(f"Unexpected error during SVN export: {e}")
