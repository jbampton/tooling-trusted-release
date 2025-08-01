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

"""draft.py"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import pathlib
from typing import TYPE_CHECKING, Protocol, TypeVar

import aiofiles.os
import aioshutil
import asfquart.base as base
import quart
import wtforms

import atr.analysis as analysis
import atr.construct as construct
import atr.db as db
import atr.db.interaction as interaction
import atr.log as log
import atr.models.sql as sql
import atr.revision as revision
import atr.routes as routes
import atr.routes.compose as compose
import atr.routes.root as root
import atr.routes.upload as upload
import atr.tasks.sbom as sbom
import atr.template as template
import atr.util as util

if TYPE_CHECKING:
    import werkzeug.wrappers.response as response


T = TypeVar("T")


class AddProtocol(Protocol):
    """Protocol for forms that create release candidate drafts."""

    version_name: wtforms.StringField
    project_name: wtforms.SelectField


class DeleteFileForm(util.QuartFormTyped):
    """Form for deleting a file."""

    file_path = wtforms.StringField("File path", validators=[wtforms.validators.InputRequired("File path is required")])
    submit = wtforms.SubmitField("Delete file")


class DeleteForm(util.QuartFormTyped):
    """Form for deleting a candidate draft."""

    release_name = wtforms.HiddenField(validators=[wtforms.validators.InputRequired()])
    project_name = wtforms.HiddenField(validators=[wtforms.validators.InputRequired()])
    version_name = wtforms.HiddenField(validators=[wtforms.validators.InputRequired()])
    confirm_delete = wtforms.StringField(
        "Confirmation",
        validators=[
            wtforms.validators.InputRequired("Confirmation is required"),
            wtforms.validators.Regexp("^DELETE$", message="Please type DELETE to confirm"),
        ],
    )
    submit = wtforms.SubmitField("Delete candidate draft")


@routes.committer("/draft/delete", methods=["POST"])
async def delete(session: routes.CommitterSession) -> response.Response:
    """Delete a candidate draft and all its associated files."""
    form = await DeleteForm.create_form(data=await quart.request.form)
    if not await form.validate_on_submit():
        for _field, errors in form.errors.items():
            for error in errors:
                await quart.flash(f"{error}", "error")
        return await session.redirect(root.index)

    release_name = form.release_name.data
    if not release_name:
        return await session.redirect(root.index, error="Missing required parameters")

    project_name = form.project_name.data
    if not project_name:
        return await session.redirect(root.index, error="Missing required parameters")

    version_name = form.version_name.data
    if not version_name:
        return await session.redirect(root.index, error="Missing required parameters")

    await session.check_access(project_name)

    # Delete the metadata from the database
    async with db.session() as data:
        async with data.begin():
            try:
                await interaction.release_delete(
                    release_name, phase=sql.ReleasePhase.RELEASE_CANDIDATE_DRAFT, include_downloads=False
                )
            except Exception as e:
                log.exception("Error deleting candidate draft:")
                return await session.redirect(root.index, error=f"Error deleting candidate draft: {e!s}")

    # Delete the files on disk, including all revisions
    # We can't use util.release_directory_base here because we don't have the release object
    draft_dir = util.get_unfinished_dir() / project_name / version_name
    if await aiofiles.os.path.exists(draft_dir):
        # Believe this to be another bug in mypy Protocol handling
        # TODO: Confirm that this is a bug, and report upstream
        # Changing it to str(...) doesn't work either
        # Yet it works in preview.py
        await aioshutil.rmtree(draft_dir)  # type: ignore[call-arg]

    return await session.redirect(root.index, success="Candidate draft deleted successfully")


@routes.committer("/draft/delete-file/<project_name>/<version_name>", methods=["POST"])
async def delete_file(session: routes.CommitterSession, project_name: str, version_name: str) -> response.Response:
    """Delete a specific file from the release candidate, creating a new revision."""
    await session.check_access(project_name)

    form = await DeleteFileForm.create_form(data=await quart.request.form)
    if not await form.validate_on_submit():
        return await session.redirect(compose.selected, project_name=project_name, version_name=version_name)

    rel_path_to_delete = pathlib.Path(str(form.file_path.data))
    metadata_files_deleted = 0

    try:
        description = "File deletion through web interface"
        async with revision.create_and_manage(
            project_name, version_name, session.uid, description=description
        ) as creating:
            # Uses new_revision_number for logging only
            # Path to delete within the new revision directory
            path_in_new_revision = creating.interim_path / rel_path_to_delete

            # Check that the file exists in the new revision
            if not await aiofiles.os.path.exists(path_in_new_revision):
                # This indicates a potential severe issue with hard linking or logic
                log.error(f"SEVERE ERROR! File {rel_path_to_delete} not found in new revision before deletion")
                raise routes.FlashError("File to delete was not found in the new revision")

            # Check whether the file is an artifact
            if analysis.is_artifact(path_in_new_revision):
                # If so, delete all associated metadata files in the new revision
                async for p in util.paths_recursive(path_in_new_revision.parent):
                    # Construct full path within the new revision
                    metadata_path_obj = creating.interim_path / p
                    if p.name.startswith(rel_path_to_delete.name + "."):
                        await aiofiles.os.remove(metadata_path_obj)
                        metadata_files_deleted += 1

            # Delete the file
            await aiofiles.os.remove(path_in_new_revision)

    except Exception as e:
        log.exception("Error deleting file:")
        await quart.flash(f"Error deleting file: {e!s}", "error")
        return await session.redirect(compose.selected, project_name=project_name, version_name=version_name)

    success_message = f"File '{rel_path_to_delete.name}' deleted successfully"
    if metadata_files_deleted:
        success_message += (
            f", and {metadata_files_deleted} associated metadata "
            f"file{'' if metadata_files_deleted == 1 else 's'} deleted"
        )
    return await session.redirect(
        compose.selected, success=success_message, project_name=project_name, version_name=version_name
    )


@routes.committer("/draft/fresh/<project_name>/<version_name>", methods=["POST"])
async def fresh(session: routes.CommitterSession, project_name: str, version_name: str) -> response.Response:
    """Restart all checks for a whole release candidate draft."""
    # Admin only button, but it's okay if users find and use this manually
    await session.check_access(project_name)

    await util.validate_empty_form()
    # Restart checks by creating a new identical draft revision
    # This doesn't make sense unless the checks themselves have been updated
    # Therefore we only show the button for this to admins
    description = "Empty revision to restart all checks for the whole release candidate draft"
    async with revision.create_and_manage(
        project_name, version_name, session.uid, description=description
    ) as _creating:
        pass

    return await session.redirect(
        compose.selected,
        project_name=project_name,
        version_name=version_name,
        success="All checks restarted",
    )


@routes.committer("/draft/hashgen/<project_name>/<version_name>/<path:file_path>", methods=["POST"])
async def hashgen(
    session: routes.CommitterSession, project_name: str, version_name: str, file_path: str
) -> response.Response:
    """Generate an sha256 or sha512 hash file for a candidate draft file, creating a new revision."""
    await session.check_access(project_name)

    # Get the hash type from the form data
    # TODO: This is not truly empty, so make a form object for this
    await util.validate_empty_form()
    form = await quart.request.form
    hash_type = form.get("hash_type")
    if hash_type not in {"sha256", "sha512"}:
        raise base.ASFQuartException("Invalid hash type", errorcode=400)

    rel_path = pathlib.Path(file_path)

    try:
        description = "Hash generation through web interface"
        async with revision.create_and_manage(
            project_name, version_name, session.uid, description=description
        ) as creating:
            # Uses new_revision_number for logging only
            path_in_new_revision = creating.interim_path / rel_path
            hash_path_rel = rel_path.name + f".{hash_type}"
            hash_path_in_new_revision = creating.interim_path / rel_path.parent / hash_path_rel

            # Check that the source file exists in the new revision
            if not await aiofiles.os.path.exists(path_in_new_revision):
                log.error(f"Source file {rel_path} not found in new revision for hash generation.")
                raise routes.FlashError("Source file not found in the new revision.")

            # Check that the hash file does not already exist in the new revision
            if await aiofiles.os.path.exists(hash_path_in_new_revision):
                raise base.ASFQuartException(f"{hash_type} file already exists", errorcode=400)

            # Read the source file from the new revision and compute the hash
            hash_obj = hashlib.sha256() if hash_type == "sha256" else hashlib.sha512()
            async with aiofiles.open(path_in_new_revision, "rb") as f:
                while chunk := await f.read(8192):
                    hash_obj.update(chunk)

            # Write the hash file into the new revision
            hash_value = hash_obj.hexdigest()
            async with aiofiles.open(hash_path_in_new_revision, "w") as f:
                await f.write(f"{hash_value}  {rel_path.name}\n")

    except Exception as e:
        log.exception("Error generating hash file:")
        await quart.flash(f"Error generating hash file: {e!s}", "error")
        return await session.redirect(compose.selected, project_name=project_name, version_name=version_name)

    return await session.redirect(
        compose.selected,
        success=f"{hash_type} file generated successfully",
        project_name=project_name,
        version_name=version_name,
    )


@routes.committer("/draft/sbomgen/<project_name>/<version_name>/<path:file_path>", methods=["POST"])
async def sbomgen(
    session: routes.CommitterSession, project_name: str, version_name: str, file_path: str
) -> response.Response:
    """Generate a CycloneDX SBOM file for a candidate draft file, creating a new revision."""
    await session.check_access(project_name)

    await util.validate_empty_form()
    rel_path = pathlib.Path(file_path)

    # Check that the file is a .tar.gz archive before creating a revision
    if not (file_path.endswith(".tar.gz") or file_path.endswith(".tgz")):
        raise base.ASFQuartException("SBOM generation is only supported for .tar.gz files", errorcode=400)

    try:
        description = "SBOM generation through web interface"
        async with revision.create_and_manage(
            project_name, version_name, session.uid, description=description
        ) as creating:
            # Uses new_revision_number in a functional way
            path_in_new_revision = creating.interim_path / rel_path
            sbom_path_rel = rel_path.with_suffix(rel_path.suffix + ".cdx.json").name
            sbom_path_in_new_revision = creating.interim_path / rel_path.parent / sbom_path_rel

            # Check that the source file exists in the new revision
            if not await aiofiles.os.path.exists(path_in_new_revision):
                log.error(f"Source file {rel_path} not found in new revision for SBOM generation.")
                raise routes.FlashError("Source artifact file not found in the new revision.")

            # Check that the SBOM file does not already exist in the new revision
            if await aiofiles.os.path.exists(sbom_path_in_new_revision):
                raise base.ASFQuartException("SBOM file already exists", errorcode=400)

        if creating.new is None:
            raise routes.FlashError("Internal error: New revision not found")

        # Create and queue the task, using paths within the new revision
        async with db.session() as data:
            # We still need release.name for the task metadata
            sbom_task = sql.Task(
                task_type=sql.TaskType.SBOM_GENERATE_CYCLONEDX,
                task_args=sbom.GenerateCycloneDX(
                    artifact_path=str(path_in_new_revision.resolve()),
                    output_path=str(sbom_path_in_new_revision.resolve()),
                ).model_dump(),
                asf_uid=util.unwrap(session.uid),
                added=datetime.datetime.now(datetime.UTC),
                status=sql.TaskStatus.QUEUED,
                project_name=project_name,
                version_name=version_name,
                revision_number=creating.new.number,
            )
            data.add(sbom_task)
            await data.commit()

            # We must wait until the sbom_task is complete before we can queue checks
            # Maximum wait time is 60 * 100ms = 6000ms
            for _attempt in range(60):
                await data.refresh(sbom_task)
                if sbom_task.status != sql.TaskStatus.QUEUED:
                    break
                # Wait 100ms before checking again
                await asyncio.sleep(0.1)

    except Exception as e:
        log.exception("Error generating SBOM:")
        await quart.flash(f"Error generating SBOM: {e!s}", "error")
        return await session.redirect(compose.selected, project_name=project_name, version_name=version_name)

    return await session.redirect(
        compose.selected,
        success=f"SBOM generation task queued for {rel_path.name}",
        project_name=project_name,
        version_name=version_name,
    )


@routes.committer("/draft/svnload/<project_name>/<version_name>", methods=["POST"])
async def svnload(session: routes.CommitterSession, project_name: str, version_name: str) -> response.Response | str:
    """Import files from SVN into a draft."""
    await session.check_access(project_name)

    form = await upload.SvnImportForm.create_form()
    if not await form.validate_on_submit():
        for _field, errors in form.errors.items():
            for error in errors:
                await quart.flash(f"{error}", "error")
        return await session.redirect(
            upload.selected,
            project_name=project_name,
            version_name=version_name,
        )

    try:
        task_args = {
            "svn_url": str(form.svn_url.data),
            "revision": str(form.revision.data),
            "target_subdirectory": str(form.target_subdirectory.data) if form.target_subdirectory.data else None,
            "project_name": project_name,
            "version_name": version_name,
            "asf_uid": session.uid,
        }
        async with db.session() as data:
            svn_import_task = sql.Task(
                task_type=sql.TaskType.SVN_IMPORT_FILES,
                task_args=task_args,
                asf_uid=util.unwrap(session.uid),
                added=datetime.datetime.now(datetime.UTC),
                status=sql.TaskStatus.QUEUED,
                project_name=project_name,
                version_name=version_name,
            )
            data.add(svn_import_task)
            await data.commit()

    except Exception:
        log.exception("Error queueing SVN import task:")
        return await session.redirect(
            upload.selected,
            error="Error queueing SVN import task",
            project_name=project_name,
            version_name=version_name,
        )

    return await session.redirect(
        compose.selected,
        success="SVN import task queued successfully",
        project_name=project_name,
        version_name=version_name,
    )


@routes.committer("/draft/tools/<project_name>/<version_name>/<path:file_path>")
async def tools(session: routes.CommitterSession, project_name: str, version_name: str, file_path: str) -> str:
    """Show the tools for a specific file."""
    await session.check_access(project_name)

    release = await session.release(project_name, version_name)
    full_path = str(util.release_directory(release) / file_path)

    # Check that the file exists
    if not await aiofiles.os.path.exists(full_path):
        raise base.ASFQuartException("File does not exist", errorcode=404)

    modified = int(await aiofiles.os.path.getmtime(full_path))
    file_size = await aiofiles.os.path.getsize(full_path)

    file_data = {
        "filename": pathlib.Path(file_path).name,
        "bytes_size": file_size,
        "uploaded": datetime.datetime.fromtimestamp(modified, tz=datetime.UTC),
    }

    return await template.render(
        "draft-tools.html",
        asf_id=session.uid,
        project_name=project_name,
        version_name=version_name,
        file_path=file_path,
        file_data=file_data,
        release=release,
        format_file_size=util.format_file_size,
        empty_form=await util.EmptyForm.create_form(),
    )


# TODO: Should we deprecate this and ensure compose covers it all?
# If we did that, we'd lose the exhaustive use of the abstraction
@routes.committer("/draft/view/<project_name>/<version_name>")
async def view(session: routes.CommitterSession, project_name: str, version_name: str) -> response.Response | str:
    """View all the files in the rsync upload directory for a release."""
    await session.check_access(project_name)

    release = await session.release(project_name, version_name)

    # Convert async generator to list
    revision_number = release.latest_revision_number
    file_stats = []
    if revision_number is not None:
        file_stats = [
            stat
            async for stat in util.content_list(util.get_unfinished_dir(), project_name, version_name, revision_number)
        ]
    # Sort the files by FileStat.path
    file_stats.sort(key=lambda fs: fs.path)

    return await template.render(
        # TODO: Move to somewhere appropriate
        "phase-view.html",
        file_stats=file_stats,
        release=release,
        format_datetime=util.format_datetime,
        format_file_size=util.format_file_size,
        format_permissions=util.format_permissions,
        phase="release candidate draft",
        phase_key="draft",
    )


@routes.committer("/draft/vote/preview/<project_name>/<version_name>", methods=["POST"])
async def vote_preview(
    session: routes.CommitterSession, project_name: str, version_name: str
) -> quart.wrappers.response.Response | response.Response | str:
    """Show the vote email preview for a release."""

    class VotePreviewForm(util.QuartFormTyped):
        body = wtforms.TextAreaField("Body", validators=[wtforms.validators.InputRequired("Body is required")])
        # TODO: Validate the vote duration again? Probably not necessary in a preview
        # Note that tasks/vote.py does not use this form
        vote_duration = wtforms.IntegerField(
            "Vote duration", validators=[wtforms.validators.InputRequired("Vote duration is required")]
        )

    form = await VotePreviewForm.create_form(data=await quart.request.form)
    if not await form.validate_on_submit():
        return await session.redirect(root.index, error="Invalid form data")

    release = await session.release(project_name, version_name)
    if release.committee is None:
        raise routes.FlashError("Release has no associated committee")

    form_body: str = util.unwrap(form.body.data)
    asfuid = session.uid
    project_name = release.project.name
    version_name = release.version
    vote_duration: int = util.unwrap(form.vote_duration.data)

    body = await construct.start_vote_body(
        form_body,
        construct.StartVoteOptions(
            asfuid=asfuid,
            fullname=session.fullname,
            project_name=project_name,
            version_name=version_name,
            vote_duration=vote_duration,
        ),
    )
    return quart.Response(body, mimetype="text/plain")
