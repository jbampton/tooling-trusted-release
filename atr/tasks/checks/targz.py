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
import tarfile
from typing import Final

import atr.archives as archives
import atr.log as log
import atr.models.results as results
import atr.tasks.checks as checks


class RootDirectoryError(Exception):
    """Exception raised when a root directory is not found in an archive."""

    ...


async def integrity(args: checks.FunctionArguments) -> results.Results | None:
    """Check the integrity of a .tar.gz file."""
    recorder = await args.recorder()
    if not (artifact_abs_path := await recorder.abs_path()):
        return None

    log.info(f"Checking integrity for {artifact_abs_path} (rel: {args.primary_rel_path})")

    chunk_size = 4096
    try:
        size = await asyncio.to_thread(archives.total_size, str(artifact_abs_path), chunk_size)
        await recorder.success("Able to read all entries of the archive using tarfile", {"size": size})
    except Exception as e:
        await recorder.failure("Unable to read all entries of the archive using tarfile", {"error": str(e)})
    return None


def root_directory(tgz_path: str) -> str:
    """Find the root directory in a tar archive and validate that it has only one root dir."""
    root = None

    with tarfile.open(tgz_path, mode="r|gz") as tf:
        for member in tf:
            if member.name and member.name.split("/")[-1].startswith("._"):
                # Metadata convention
                continue

            parts = member.name.split("/", 1)
            if len(parts) >= 1:
                if not root:
                    root = parts[0]
                elif parts[0] != root:
                    raise RootDirectoryError(f"Multiple root directories found: {root}, {parts[0]}")

    if not root:
        raise RootDirectoryError("No root directory found in archive")

    return root


async def structure(args: checks.FunctionArguments) -> results.Results | None:
    """Check the structure of a .tar.gz file."""
    recorder = await args.recorder()
    if not (artifact_abs_path := await recorder.abs_path()):
        return None
    if await recorder.primary_path_is_binary():
        return None

    filename = artifact_abs_path.name
    expected_root: Final[str] = (
        filename.removesuffix(".tar.gz") if filename.endswith(".tar.gz") else filename.removesuffix(".tgz")
    )
    log.info(
        f"Checking structure for {artifact_abs_path} (expected root: {expected_root}) (rel: {args.primary_rel_path})"
    )

    try:
        root = await asyncio.to_thread(root_directory, str(artifact_abs_path))
        if root == expected_root:
            await recorder.success(
                "Archive contains exactly one root directory matching the expected name",
                {"root": root, "expected": expected_root},
            )
        else:
            await recorder.warning(
                f"Root directory '{root}' does not match expected name '{expected_root}'",
                {"root": root, "expected": expected_root},
            )
    except RootDirectoryError as e:
        await recorder.warning("Could not get the root directory of the archive", {"error": str(e)})
    except Exception as e:
        await recorder.failure("Unable to verify archive structure", {"error": str(e)})
    return None
