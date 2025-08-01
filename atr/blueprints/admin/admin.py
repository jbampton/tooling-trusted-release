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
import collections
import os
import pathlib
import statistics
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

import aiofiles.os
import aiohttp
import asfquart
import asfquart.base as base
import asfquart.session as session
import quart
import sqlalchemy.orm as orm
import sqlmodel
import werkzeug.wrappers.response as response
import wtforms

import atr.blueprints.admin as admin
import atr.config as config
import atr.datasources.apache as apache
import atr.db as db
import atr.db.interaction as interaction
import atr.ldap as ldap
import atr.log as log
import atr.models.sql as sql
import atr.routes.mapping as mapping
import atr.storage as storage
import atr.storage.types as types
import atr.template as template
import atr.util as util
import atr.validate as validate


class BrowseAsUserForm(util.QuartFormTyped):
    """Form for browsing as another user."""

    uid = wtforms.StringField(
        "ASF UID",
        validators=[wtforms.validators.InputRequired()],
        render_kw={"placeholder": "Enter the ASF UID to browse as"},
    )
    submit = wtforms.SubmitField("Browse as this user")


class DeleteCommitteeKeysForm(util.QuartFormTyped):
    committee_name = wtforms.SelectField("Committee", validators=[wtforms.validators.InputRequired()])
    confirm_delete = wtforms.StringField(
        "Confirmation",
        validators=[wtforms.validators.InputRequired(), wtforms.validators.Regexp("^DELETE KEYS$")],
        render_kw={"placeholder": "DELETE KEYS"},
    )
    submit = wtforms.SubmitField("Delete all keys for selected committee")


class DeleteReleaseForm(util.QuartFormTyped):
    """Form for deleting releases."""

    confirm_delete = wtforms.StringField(
        "Confirmation",
        validators=[
            wtforms.validators.InputRequired("Confirmation is required"),
            wtforms.validators.Regexp("^DELETE$", message="Please type DELETE to confirm"),
        ],
        render_kw={"placeholder": "DELETE"},
        description="Please type DELETE exactly to confirm deletion.",
    )
    submit = wtforms.SubmitField("Delete selected releases permanently")


class LdapLookupForm(util.QuartFormTyped):
    uid = wtforms.StringField(
        "ASF UID (optional)",
        render_kw={"placeholder": "Enter ASF UID, e.g. johnsmith, or * for all"},
    )
    email = wtforms.StringField(
        "Email address (optional)",
        render_kw={"placeholder": "Enter email address, e.g. user@example.org"},
    )
    submit = wtforms.SubmitField("Lookup")


@admin.BLUEPRINT.route("/all-releases")
async def admin_all_releases() -> str:
    """Display a list of all releases across all phases."""
    async with db.session() as data:
        releases = await data.release(_project=True, _committee=True).order_by(sql.Release.name).all()
    return await template.render("all-releases.html", releases=releases, release_as_url=mapping.release_as_url)


@admin.BLUEPRINT.route("/browse-as", methods=["GET", "POST"])
async def admin_browse_as() -> str | response.Response:
    """Allows an admin to browse as another user."""
    # TODO: Enable this in debugging mode only?
    from atr.routes import root

    form = await BrowseAsUserForm.create_form()
    if not (await form.validate_on_submit()):
        return await template.render("browse-as.html", form=form)

    new_uid = str(util.unwrap(form.uid.data))
    if not (current_session := await session.read()):
        raise base.ASFQuartException("Not authenticated", 401)

    bind_dn = quart.current_app.config.get("LDAP_BIND_DN")
    bind_password = quart.current_app.config.get("LDAP_BIND_PASSWORD")
    ldap_params = ldap.SearchParameters(
        uid_query=new_uid,
        bind_dn_from_config=bind_dn,
        bind_password_from_config=bind_password,
    )
    await asyncio.to_thread(ldap.search, ldap_params)

    if not ldap_params.results_list:
        await quart.flash(f"User '{new_uid}' not found in LDAP.", "error")
        return quart.redirect(quart.url_for("admin.admin_browse_as"))

    ldap_projects_data = await apache.get_ldap_projects_data()
    committee_data = await apache.get_active_committee_data()
    ldap_data = ldap_params.results_list[0]
    log.info("Current session data: %s", current_session)
    new_session_data = _session_data(ldap_data, new_uid, current_session, ldap_projects_data, committee_data)
    log.info("New session data: %s", new_session_data)
    session.write(new_session_data)

    await quart.flash(
        f"You are now browsing as '{new_uid}'. To return to your own account, please log out and log back in.",
        "success",
    )
    return quart.redirect(util.as_url(root.index))


@admin.BLUEPRINT.route("/config")
async def admin_config() -> quart.wrappers.response.Response:
    """Display the current application configuration values."""

    conf = config.get()
    values: list[str] = []
    for name in dir(conf):
        if name.startswith("_"):
            continue
        try:
            val = getattr(conf, name)
        except Exception as exc:
            val = f"<error: {exc}>"
        if name.endswith("_PASSWORD"):
            val = "<redacted>"
        if callable(val):
            continue
        values.append(f"{name}={val}")

    values.sort()
    return quart.Response("\n".join(values), mimetype="text/plain")


@admin.BLUEPRINT.route("/consistency")
async def admin_consistency() -> quart.Response:
    """Check for consistency between the database and the filesystem."""
    # Get all releases from the database
    async with db.session() as data:
        releases = await data.release().all()
    database_dirs = []
    for release in releases:
        path = util.release_directory_version(release)
        database_dirs.append(str(path))
    if len(set(database_dirs)) != len(database_dirs):
        raise base.ASFQuartException("Duplicate release directories in database", errorcode=500)

    # Get all releases from the filesystem
    filesystem_dirs = await _get_filesystem_dirs()

    # Pair them up where possible
    paired_dirs = []
    for database_dir in database_dirs[:]:
        for filesystem_dir in filesystem_dirs[:]:
            if database_dir == filesystem_dir:
                paired_dirs.append(database_dir)
                database_dirs.remove(database_dir)
                filesystem_dirs.remove(filesystem_dir)
                break
    return quart.Response(
        f"""\
=== BROKEN ===

DATABASE ONLY:

{"\n".join(sorted(database_dirs or ["-"]))}

FILESYSTEM ONLY:

{"\n".join(sorted(filesystem_dirs or ["-"]))}


== Okay ==

Paired correctly:

{"\n".join(sorted(paired_dirs or ["-"]))}
""",
        mimetype="text/plain",
    )


@admin.BLUEPRINT.route("/data")
@admin.BLUEPRINT.route("/data/<model>")
async def admin_data(model: str = "Committee") -> str:
    """Browse all records in the database."""
    async with db.session() as data:
        # Map of model names to their classes
        # TODO: Add distribution channel, key link, and any others
        model_methods: dict[str, Callable[[], db.Query[Any]]] = {
            "CheckResult": data.check_result,
            "Committee": data.committee,
            "Project": data.project,
            "PublicSigningKey": data.public_signing_key,
            "Release": data.release,
            "ReleasePolicy": data.release_policy,
            "Revision": data.revision,
            "SSHKey": data.ssh_key,
            "Task": data.task,
            "TextValue": data.text_value,
        }

        if model not in model_methods:
            raise base.ASFQuartException(f"Model type '{model}' not found", 404)

        # Get all records for the selected model
        records = await model_methods[model]().all()

        # Convert records to dictionaries for JSON serialization
        records_dict = []
        for record in records:
            if hasattr(record, "dict"):
                record_dict = record.dict()
            else:
                # Fallback for models without dict() method
                record_dict = {}
                # record_dict = {
                #     "id": getattr(record, "id", None),
                #     "name": getattr(record, "name", None),
                # }
                for key in record.__dict__:
                    if not key.startswith("_"):
                        record_dict[key] = getattr(record, key)
            records_dict.append(record_dict)

        return await template.render(
            "data-browser.html", models=list(model_methods.keys()), model=model, records=records_dict
        )


@admin.BLUEPRINT.route("/delete-committee-keys", methods=["GET", "POST"])
async def admin_delete_committee_keys() -> str | response.Response:
    form = await DeleteCommitteeKeysForm.create_form()
    async with db.session() as data:
        all_committees = await data.committee(_public_signing_keys=True).order_by(sql.Committee.name).all()
        committees_with_keys = [c for c in all_committees if c.public_signing_keys]
    form.committee_name.choices = [(c.name, c.display_name) for c in committees_with_keys]

    if await form.validate_on_submit():
        committee_name = form.committee_name.data
        async with db.session() as data:
            committee_query = data.committee(name=committee_name)
            via = sql.validate_instrumented_attribute
            committee_query.query = committee_query.query.options(
                orm.selectinload(via(sql.Committee.public_signing_keys)).selectinload(
                    via(sql.PublicSigningKey.committees)
                )
            )
            committee = await committee_query.get()

            if not committee:
                await quart.flash(f"Committee '{committee_name}' not found.", "error")
                return quart.redirect(quart.url_for("admin.admin_delete_committee_keys"))

            keys_to_check = list(committee.public_signing_keys)
            if not keys_to_check:
                await quart.flash(f"Committee '{committee_name}' has no keys.", "info")
                return quart.redirect(quart.url_for("admin.admin_delete_committee_keys"))

            num_removed = len(committee.public_signing_keys)
            committee.public_signing_keys.clear()
            await data.flush()

            unused_deleted = 0
            for key_obj in keys_to_check:
                if not key_obj.committees:
                    await data.delete(key_obj)
                    unused_deleted += 1

            await data.commit()
            await quart.flash(
                f"Removed {num_removed} key links for '{committee_name}'. Deleted {unused_deleted} unused keys.",
                "success",
            )
        return quart.redirect(quart.url_for("admin.admin_delete_committee_keys"))

    elif quart.request.method == "POST":
        await quart.flash("Form validation failed. Select committee and type DELETE KEYS.", "warning")

    return await template.render("delete-committee-keys.html", form=form)


@admin.BLUEPRINT.route("/delete-release", methods=["GET", "POST"])
async def admin_delete_release() -> str | response.Response:
    """Page to delete selected releases and their associated data and files."""
    form = await DeleteReleaseForm.create_form()

    if quart.request.method == "POST":
        if await form.validate_on_submit():
            form_data = await quart.request.form
            releases_to_delete = form_data.getlist("releases_to_delete")

            if not releases_to_delete:
                await quart.flash("No releases selected for deletion.", "warning")
                return quart.redirect(quart.url_for("admin.admin_delete_release"))

            success_count = 0
            fail_count = 0
            error_messages = []

            for release_name in releases_to_delete:
                try:
                    await interaction.release_delete(release_name)
                    success_count += 1
                except base.ASFQuartException as e:
                    log.error("Error deleting release %s: %s", release_name, e)
                    fail_count += 1
                    error_messages.append(f"{release_name}: {e}")
                except Exception as e:
                    log.exception("Unexpected error deleting release %s:", release_name)
                    fail_count += 1
                    error_messages.append(f"{release_name}: Unexpected error ({e})")

            if success_count > 0:
                await quart.flash(f"Successfully deleted {success_count} release(s).", "success")
            if fail_count > 0:
                errors_str = "\n".join(error_messages)
                await quart.flash(f"Failed to delete {fail_count} release(s):\n{errors_str}", "error")

            # Redirecting back to the deletion page will refresh the list of releases too
            return quart.redirect(quart.url_for("admin.admin_delete_release"))

        # It's unlikely that form validation failed due to spurious release names
        # Therefore we assume that the user forgot to type DELETE to confirm
        await quart.flash("Form validation failed. Please type DELETE to confirm.", "warning")
        # Fall through to the combined GET and failed form validation handling below

    # For GET request or failed form validation
    async with db.session() as data:
        releases = await data.release(_project=True).order_by(sql.Release.name).all()
    return await template.render("delete-release.html", form=form, releases=releases, stats=None)


@admin.BLUEPRINT.route("/env")
async def admin_env() -> quart.wrappers.response.Response:
    """Display the environment variables."""
    env_vars = []
    for key, value in os.environ.items():
        env_vars.append(f"{key}={value}")
    return quart.Response("\n".join(env_vars), mimetype="text/plain")


@admin.BLUEPRINT.route("/keys/check", methods=["GET", "POST"])
async def admin_keys_check() -> quart.Response:
    """Check public signing key details."""
    if quart.request.method != "POST":
        empty_form = await util.EmptyForm.create_form()
        return quart.Response(
            f"""
<form method="post">
  <button type="submit">Check public signing key details</button>
  {empty_form.hidden_tag()}
</form>
""",
            mimetype="text/html",
        )

    try:
        result = await _check_keys()
        return quart.Response(result, mimetype="text/plain")
    except Exception as e:
        log.exception("Exception during key check:")
        return quart.Response(f"Exception during key check: {e!s}", mimetype="text/plain")


@admin.BLUEPRINT.route("/keys/regenerate-all", methods=["GET", "POST"])
async def admin_keys_regenerate_all() -> quart.Response:
    """Regenerate the KEYS file for all committees."""
    if quart.request.method != "POST":
        empty_form = await util.EmptyForm.create_form()
        return quart.Response(
            f"""
<form method="post">
  <button type="submit">Regenerate all KEYS files</button>
  {empty_form.hidden_tag()}
</form>
""",
            mimetype="text/html",
        )

    async with db.session() as data:
        committee_names = [c.name for c in await data.committee().all()]

    web_session = await session.read()
    if web_session is None:
        raise base.ASFQuartException("Not authenticated", 401)
    asf_uid = web_session.uid
    if asf_uid is None:
        raise base.ASFQuartException("Invalid session, uid is None", 500)

    outcomes = types.Outcomes[str]()
    async with storage.write(asf_uid) as write:
        for committee_name in committee_names:
            wacm = write.as_committee_member(committee_name)
            if wacm is None:
                continue
            outcomes.append(await wacm.keys.autogenerate_keys_file())

    response_lines = []
    for ocr in outcomes.results():
        response_lines.append(f"Regenerated: {ocr}")
    for oce in outcomes.exceptions():
        response_lines.append(f"Error regenerating: {type(oce).__name__} {oce}")

    return quart.Response("\n".join(response_lines), mimetype="text/plain")


@admin.BLUEPRINT.route("/keys/update", methods=["GET", "POST"])
async def admin_keys_update() -> str | response.Response | tuple[Mapping[str, Any], int]:
    """Update keys from remote data."""
    if quart.request.method != "POST":
        empty_form = await util.EmptyForm.create_form()
        # Get the previous output from the log file
        log_path = pathlib.Path("keys_import.log")
        if not await aiofiles.os.path.exists(log_path):
            previous_output = None
        else:
            async with aiofiles.open(log_path) as f:
                previous_output = await f.read()
        return await template.render("update-keys.html", empty_form=empty_form, previous_output=previous_output)

    try:
        web_session = await session.read()
        if web_session is None:
            raise base.ASFQuartException("Not authenticated", 401)
        asf_uid = web_session.uid
        if asf_uid is None:
            raise base.ASFQuartException("Invalid session, uid is None", 500)
        pid = await _update_keys(asf_uid)
        return {
            "message": f"Successfully started key update process with PID {pid}",
            "category": "success",
        }, 200
    except Exception as e:
        log.exception("Failed to start key update process")
        return {
            "message": f"Failed to update keys: {e!s}",
            "category": "error",
        }, 200


@admin.BLUEPRINT.route("/ldap/", methods=["GET"])
async def admin_ldap() -> str:
    form = await LdapLookupForm.create_form(data=quart.request.args)
    asf_id_for_template: str | None = None

    web_session = await session.read()
    if web_session and web_session.uid:
        asf_id_for_template = web_session.uid

    uid_query = form.uid.data
    email_query = form.email.data

    ldap_params: ldap.SearchParameters | None = None
    if (quart.request.method == "GET") and (uid_query or email_query):
        bind_dn = quart.current_app.config.get("LDAP_BIND_DN")
        bind_password = quart.current_app.config.get("LDAP_BIND_PASSWORD")

        start = time.perf_counter_ns()
        ldap_params = ldap.SearchParameters(
            uid_query=uid_query,
            email_query=email_query,
            bind_dn_from_config=bind_dn,
            bind_password_from_config=bind_password,
            email_only=True,
        )
        await asyncio.to_thread(ldap.search, ldap_params)
        end = time.perf_counter_ns()
        log.info("LDAP search took %d ms", (end - start) / 1000000)

    return await template.render(
        "ldap-lookup.html",
        form=form,
        ldap_params=ldap_params,
        asf_id=asf_id_for_template,
        ldap_query_performed=ldap_params is not None,
        uid_query=uid_query,
    )


@admin.BLUEPRINT.route("/ongoing-tasks/<project_name>/<version_name>/<revision>")
async def admin_ongoing_tasks(project_name: str, version_name: str, revision: str) -> quart.wrappers.response.Response:
    try:
        ongoing = await interaction.tasks_ongoing(project_name, version_name, revision)
        return quart.Response(str(ongoing), mimetype="text/plain")
    except Exception:
        log.exception(f"Error fetching ongoing task count for {project_name} {version_name} rev {revision}:")
        return quart.Response("", mimetype="text/plain")


@admin.BLUEPRINT.route("/performance")
async def admin_performance() -> str:
    """Display performance statistics for all routes."""
    app = asfquart.APP

    if app is ...:
        raise base.ASFQuartException("APP is not set", errorcode=500)

    # Read and parse the performance log file
    log_path = pathlib.Path("route-performance.log")
    # # Show current working directory and its files
    # cwd = await asyncio.to_thread(Path.cwd)
    # await asyncio.to_thread(APP.logger.info, "Current working directory: %s", cwd)
    # iterable = await asyncio.to_thread(cwd.iterdir)
    # files = list(iterable)
    # await asyncio.to_thread(APP.logger.info, "Files in current directory: %s", files)
    if not await aiofiles.os.path.exists(log_path):
        await quart.flash("No performance data currently available", "error")
        return await template.render("performance.html", stats=None)

    # Parse the log file and collect statistics
    stats = collections.defaultdict(list)
    async with aiofiles.open(log_path) as f:
        async for line in f:
            try:
                _, _, _, methods, path, func, _, sync_ms, async_ms, total_ms = line.strip().split(" ")
                stats[path].append(
                    {
                        "methods": methods,
                        "function": func,
                        "sync_ms": int(sync_ms),
                        "async_ms": int(async_ms),
                        "total_ms": int(total_ms),
                        "timestamp": line.split(" - ")[0],
                    }
                )
            except (ValueError, IndexError):
                log.error("Error parsing line: %s", line)
                continue

    # Calculate summary statistics for each route
    summary = {}
    for path, timings in stats.items():
        total_times = [int(str(t["total_ms"])) for t in timings]
        sync_times = [int(str(t["sync_ms"])) for t in timings]
        async_times = [int(str(t["async_ms"])) for t in timings]

        summary[path] = {
            "count": len(timings),
            "methods": timings[0]["methods"],
            "function": timings[0]["function"],
            "total": {
                "mean": statistics.mean(total_times),
                "median": statistics.median(total_times),
                "min": min(total_times),
                "max": max(total_times),
                "stdev": statistics.stdev(total_times) if len(total_times) > 1 else 0,
            },
            "sync": {
                "mean": statistics.mean(sync_times),
                "median": statistics.median(sync_times),
                "min": min(sync_times),
                "max": max(sync_times),
            },
            "async": {
                "mean": statistics.mean(async_times),
                "median": statistics.median(async_times),
                "min": min(async_times),
                "max": max(async_times),
            },
            "last_timestamp": timings[-1]["timestamp"],
        }

    # Sort routes by average total time, descending
    def one_total_mean(x: tuple[str, dict]) -> float:
        return x[1]["total"]["mean"]

    sorted_summary = dict(sorted(summary.items(), key=one_total_mean, reverse=True))
    return await template.render("performance.html", stats=sorted_summary)


@admin.BLUEPRINT.route("/projects/update", methods=["GET", "POST"])
async def admin_projects_update() -> str | response.Response | tuple[Mapping[str, Any], int]:
    """Update projects from remote data."""
    if quart.request.method == "POST":
        try:
            added_count, updated_count = await _update_metadata()
            return {
                "message": f"Successfully added {added_count} and updated {updated_count} committees and projects "
                f"(PMCs and PPMCs) with membership data",
                "category": "success",
            }, 200
        except aiohttp.ClientError as e:
            return {
                "message": f"Failed to fetch data: {e!s}",
                "category": "error",
            }, 200
        except Exception as e:
            return {
                "message": f"Failed to update projects: {e!s}",
                "category": "error",
            }, 200

    # For GET requests, show the update form
    empty_form = await util.EmptyForm.create_form()
    return await template.render("update-projects.html", empty_form=empty_form)


@admin.BLUEPRINT.route("/tasks")
async def admin_tasks() -> str:
    return await template.render("tasks.html")


@admin.BLUEPRINT.route("/task-times/<project_name>/<version_name>/<revision_number>")
async def admin_task_times(
    project_name: str, version_name: str, revision_number: str
) -> quart.wrappers.response.Response:
    values = []
    async with db.session() as data:
        tasks = await data.task(
            project_name=project_name, version_name=version_name, revision_number=revision_number
        ).all()  # type: ignore[reportOptionalMemberAccess]
        for task in tasks:
            if (task.started is None) or (task.completed is None):
                continue
            ms_elapsed = (task.completed - task.started).total_seconds() * 1000
            values.append(f"{task.task_type} {ms_elapsed:.2f}ms")

    return quart.Response("\n".join(values), mimetype="text/plain")


@admin.BLUEPRINT.route("/test", methods=["GET"])
async def admin_test() -> quart.wrappers.response.Response:
    """Test the storage layer."""
    import atr.storage as storage

    async with aiohttp.ClientSession() as aiohttp_client_session:
        url = "https://downloads.apache.org/zeppelin/KEYS"
        async with aiohttp_client_session.get(url) as response:
            keys_file_text = await response.text()

    web_session = await session.read()
    if web_session is None:
        raise base.ASFQuartException("Not authenticated", 401)
    asf_uid = web_session.uid
    if asf_uid is None:
        raise base.ASFQuartException("Invalid session, uid is None", 500)
    async with storage.write(asf_uid) as write:
        wacm = write.as_committee_member("tooling")
        start = time.perf_counter_ns()
        outcomes: types.Outcomes[types.Key] = await wacm.keys.ensure_stored(keys_file_text)
        end = time.perf_counter_ns()
        log.info(f"Upload of {outcomes.result_count} keys took {end - start} ns")
    for ocr in outcomes.results():
        log.info(f"Uploaded key: {type(ocr)} {ocr.key_model.fingerprint}")
    for oce in outcomes.exceptions():
        log.error(f"Error uploading key: {type(oce)} {oce}")
    parsed_count = outcomes.result_predicate_count(lambda k: k.status == types.KeyStatus.PARSED)
    inserted_count = outcomes.result_predicate_count(lambda k: k.status == types.KeyStatus.INSERTED)
    linked_count = outcomes.result_predicate_count(lambda k: k.status == types.KeyStatus.LINKED)
    inserted_and_linked_count = outcomes.result_predicate_count(
        lambda k: k.status == types.KeyStatus.INSERTED_AND_LINKED
    )
    log.info(f"Parsed: {parsed_count}")
    log.info(f"Inserted: {inserted_count}")
    log.info(f"Linked: {linked_count}")
    log.info(f"InsertedAndLinked: {inserted_and_linked_count}")
    return quart.Response(str(wacm), mimetype="text/plain")


@admin.BLUEPRINT.route("/toggle-view", methods=["GET"])
async def admin_toggle_admin_view_page() -> str:
    """Display the page with a button to toggle between admin and user views."""
    empty_form = await util.EmptyForm.create_form()
    return await template.render("toggle-admin-view.html", empty_form=empty_form)


@admin.BLUEPRINT.route("/toggle-admin-view", methods=["POST"])
async def admin_toggle_view() -> response.Response:
    await util.validate_empty_form()

    web_session = await session.read()
    if web_session is None:
        # For the type checker
        # We should pass this as an argument, then it's guaranteed
        raise base.ASFQuartException("Not authenticated", 401)
    user_uid = web_session.uid
    if user_uid is None:
        raise base.ASFQuartException("Invalid session, uid is None", 500)

    app = asfquart.APP
    if not hasattr(app, "app_id") or not isinstance(app.app_id, str):
        raise TypeError("Internal error: APP has no valid app_id")

    cookie_id = app.app_id
    session_dict = quart.session.get(cookie_id, {})
    downgrade = not session_dict.get("downgrade_admin_to_user", False)
    session_dict["downgrade_admin_to_user"] = downgrade

    message = "Viewing as regular user" if downgrade else "Viewing as admin"
    await quart.flash(message, "success")
    referrer = quart.request.referrer
    return quart.redirect(referrer or quart.url_for("admin.admin_data"))


@admin.BLUEPRINT.route("/validate")
async def admin_validate() -> str:
    """Run validators and display any divergences."""

    async with db.session() as data:
        divergences = [d async for d in validate.everything(data)]

    return await template.render(
        "validation.html",
        divergences=divergences,
    )


async def _check_keys(fix: bool = False) -> str:
    email_to_uid = await util.email_to_uid_map()
    bad_keys = []
    async with db.session() as data:
        keys = await data.public_signing_key().all()
        for key in keys:
            uids = []
            if key.primary_declared_uid:
                uids.append(key.primary_declared_uid)
            if key.secondary_declared_uids:
                uids.extend(key.secondary_declared_uids)
            asf_uid = await util.asf_uid_from_uids(uids, ldap_data=email_to_uid)
            if asf_uid != key.apache_uid:
                bad_keys.append(f"{key.fingerprint} detected: {asf_uid}, key: {key.apache_uid}")
            if fix:
                key.apache_uid = asf_uid
                await data.commit()
    message = f"Checked {len(keys)} keys"
    if bad_keys:
        message += f"\nFound {len(bad_keys)} bad keys:\n{'\n'.join(bad_keys)}"
    return message


async def _get_filesystem_dirs() -> list[str]:
    filesystem_dirs = []
    await _get_filesystem_dirs_finished(filesystem_dirs)
    await _get_filesystem_dirs_unfinished(filesystem_dirs)
    return filesystem_dirs


async def _get_filesystem_dirs_finished(filesystem_dirs: list[str]) -> None:
    finished_dir = util.get_finished_dir()
    finished_dir_contents = await aiofiles.os.listdir(finished_dir)
    for project_dir in finished_dir_contents:
        project_dir_path = os.path.join(finished_dir, project_dir)
        if await aiofiles.os.path.isdir(project_dir_path):
            for version_dir in await aiofiles.os.listdir(project_dir_path):
                if await aiofiles.os.path.isdir(os.path.join(project_dir_path, version_dir)):
                    version_dir_path = os.path.join(project_dir_path, version_dir)
                    if await aiofiles.os.path.isdir(version_dir_path):
                        filesystem_dirs.append(version_dir_path)


async def _get_filesystem_dirs_unfinished(filesystem_dirs: list[str]) -> None:
    unfinished_dir = util.get_unfinished_dir()
    unfinished_dir_contents = await aiofiles.os.listdir(unfinished_dir)
    for project_dir in unfinished_dir_contents:
        project_dir_path = os.path.join(unfinished_dir, project_dir)
        if await aiofiles.os.path.isdir(project_dir_path):
            for version_dir in await aiofiles.os.listdir(project_dir_path):
                if await aiofiles.os.path.isdir(os.path.join(project_dir_path, version_dir)):
                    version_dir_path = os.path.join(project_dir_path, version_dir)
                    if await aiofiles.os.path.isdir(version_dir_path):
                        filesystem_dirs.append(version_dir_path)


async def _process_undiscovered(data: db.Session) -> tuple[int, int]:
    added_count = 0
    updated_count = 0

    via = sql.validate_instrumented_attribute
    committees_without_projects = await db.Query(
        data, sqlmodel.select(sql.Committee).where(~via(sql.Committee.projects).any())
    ).all()
    # For all committees that have no associated projects
    for committee in committees_without_projects:
        if committee.name == "incubator":
            continue
        log.warning(f"Missing top level project for committee {committee.name}")
        # If a committee is missing, the following code can be activated to fix it
        # But ideally the fix should be in the upstream data source
        automatically_fix = False
        if automatically_fix is True:
            project = sql.Project(name=committee.name, full_name=committee.full_name, committee=committee)
            data.add(project)
            added_count += 1

    return added_count, updated_count


def _project_status(pmc: sql.Committee, project_name: str, project_status: apache.ProjectStatus) -> sql.ProjectStatus:
    if pmc.name == "attic":
        # This must come first, because attic is also a standing committee
        return sql.ProjectStatus.RETIRED
    elif ("_dormant_" in project_name) or project_status.name.endswith("(Dormant)"):
        return sql.ProjectStatus.DORMANT
    elif util.committee_is_standing(pmc.name):
        return sql.ProjectStatus.STANDING
    return sql.ProjectStatus.ACTIVE


def _session_data(
    ldap_data: dict[str, Any],
    new_uid: str,
    current_session: session.ClientSession,
    ldap_projects: apache.LDAPProjectsData,
    committee_data: apache.CommitteeData,
) -> dict[str, Any]:
    # This is not quite accurate
    # For example, this misses "tooling" for tooling members
    projects = {p.name for p in ldap_projects.projects if (new_uid in p.members) or (new_uid in p.owners)}
    # And this adds "incubator", which is not in the OAuth data
    committees = set()
    for c in committee_data.committees:
        for user in c.roster:
            if user.id == new_uid:
                committees.add(c.name)

    # Or asf-member-status?
    is_member = bool(projects or committees)
    is_root = False
    is_chair = any(new_uid in (user.id for user in c.chair) for c in committee_data.committees)

    return {
        "uid": ldap_data.get("uid", [new_uid])[0],
        "dn": None,
        "fullname": ldap_data.get("cn", [new_uid])[0],
        # "email": ldap_user.get("mail", [""])[0],
        # Or asf-committer-email?
        "email": f"{new_uid}@apache.org",
        "isMember": is_member,
        "isChair": is_chair,
        "isRoot": is_root,
        "pmcs": sorted(list(committees)),
        "projects": sorted(list(projects)),
        "mfa": current_session.mfa,
        "isRole": False,
        "metadata": {},
    }


async def _update_committees(
    data: db.Session, ldap_projects: apache.LDAPProjectsData, committees_by_name: Mapping[str, apache.Committee]
) -> tuple[int, int]:
    added_count = 0
    updated_count = 0

    # First create PMC committees
    for project in ldap_projects.projects:
        name = project.name
        # Skip non-PMC committees
        if project.pmc is not True:
            continue

        # Get or create PMC
        committee = await data.committee(name=name).get()
        if not committee:
            committee = sql.Committee(name=name)
            data.add(committee)
            added_count += 1
        else:
            updated_count += 1

        committee.committee_members = project.owners
        committee.committers = project.members
        # We create PMCs for now
        committee.is_podling = False
        committee_info = committees_by_name.get(name)
        if committee_info:
            committee.full_name = committee_info.display_name

        updated_count += 1

    return added_count, updated_count


async def _update_keys(asf_uid: str) -> int:
    async def _log_process(process: asyncio.subprocess.Process) -> None:
        try:
            stdout, stderr = await process.communicate()
            if stdout:
                log.info(f"keys_import.py stdout:\n{stdout.decode('utf-8')[:1000]}")
            if stderr:
                log.error(f"keys_import.py stderr:\n{stderr.decode('utf-8')[:1000]}")
        except Exception:
            log.exception("Error reading from subprocess for keys_import.py")

    app = asfquart.APP
    if not hasattr(app, "background_tasks"):
        app.background_tasks = set()

    if await aiofiles.os.path.exists("../Dockerfile.alpine"):
        # Not in a container, developing locally
        command = ["poetry", "run", "python3", "scripts/keys_import.py", asf_uid]
    else:
        # In a container
        command = [sys.executable, "scripts/keys_import.py", asf_uid]

    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=".."
    )

    task = asyncio.create_task(_log_process(process))
    app.background_tasks.add(task)
    task.add_done_callback(app.background_tasks.discard)

    return process.pid


async def _update_metadata() -> tuple[int, int]:
    ldap_projects = await apache.get_ldap_projects_data()
    projects = await apache.get_projects_data()
    podlings_data = await apache.get_current_podlings_data()
    committees = await apache.get_active_committee_data()

    ldap_projects_by_name: Mapping[str, apache.LDAPProject] = {p.name: p for p in ldap_projects.projects}
    committees_by_name: Mapping[str, apache.Committee] = {c.name: c for c in committees.committees}

    added_count = 0
    updated_count = 0

    async with db.session() as data:
        async with data.begin():
            added, updated = await _update_committees(data, ldap_projects, committees_by_name)
            added_count += added
            updated_count += updated

            added, updated = await _update_podlings(data, podlings_data, ldap_projects_by_name)
            added_count += added
            updated_count += updated

            added, updated = await _update_projects(data, projects)
            added_count += added
            updated_count += updated

            added, updated = await _update_tooling(data)
            added_count += added
            updated_count += updated

            added, updated = await _process_undiscovered(data)
            added_count += added
            updated_count += updated

    return added_count, updated_count


async def _update_podlings(
    data: db.Session, podlings_data: apache.PodlingsData, ldap_projects_by_name: Mapping[str, apache.LDAPProject]
) -> tuple[int, int]:
    added_count = 0
    updated_count = 0

    # Then add PPMCs and their associated project (podlings)
    for podling_name, podling_data in podlings_data:
        # Get or create PPMC
        ppmc = await data.committee(name=podling_name).get()
        if not ppmc:
            ppmc = sql.Committee(name=podling_name, is_podling=True)
            data.add(ppmc)
            added_count += 1
        else:
            updated_count += 1

        # We create a PPMC
        ppmc.is_podling = True
        ppmc.full_name = podling_data.name.removesuffix("(Incubating)").removeprefix("Apache").strip()
        podling_project = ldap_projects_by_name.get(podling_name)
        if podling_project is not None:
            ppmc.committee_members = podling_project.owners
            ppmc.committers = podling_project.members
        else:
            log.warning(f"could not find ldap data for podling {podling_name}")

        podling = await data.project(name=podling_name).get()
        if not podling:
            # Create the associated podling project
            podling = sql.Project(name=podling_name, full_name=podling_data.name, committee=ppmc)
            data.add(podling)
            added_count += 1
        else:
            updated_count += 1

        podling.full_name = podling_data.name.removesuffix(" (Incubating)")
        podling.committee = ppmc
        # TODO: Why did the type checkers not detect this?
        # podling.is_podling = True

    return added_count, updated_count


async def _update_projects(data: db.Session, projects: apache.ProjectsData) -> tuple[int, int]:
    added_count = 0
    updated_count = 0

    # Add projects and associate them with the right PMC
    for project_name, project_status in projects.items():
        # FIXME: this is a quick workaround for inconsistent data wrt webservices PMC / projects
        #        the PMC seems to be identified by the key ws, but the associated projects use webservices
        if project_name.startswith("webservices-"):
            project_name = project_name.replace("webservices-", "ws-")
            project_status.pmc = "ws"

        # TODO: Annotator is in both projects and ldap_projects
        # The projects version is called "incubator-annotator", with "incubator" as its pmc
        # This is not detected by us as incubating, because we create those above
        # ("Create the associated podling project")
        # Since the Annotator project is in ldap_projects, we can just skip it here
        # Originally reported in https://github.com/apache/tooling-trusted-release/issues/35
        # Ideally it would be removed from the upstream data source, which is:
        # https://projects.apache.org/json/foundation/projects.json
        if project_name == "incubator-annotator":
            continue

        pmc = await data.committee(name=project_status.pmc).get()
        if not pmc:
            log.warning(f"could not find PMC for project {project_name}: {project_status.pmc}")
            continue

        project_model = await data.project(name=project_name).get()
        # Check whether the project is retired, whether temporarily or otherwise
        status = _project_status(pmc, project_name, project_status)
        if not project_model:
            project_model = sql.Project(name=project_name, committee=pmc, status=status)
            data.add(project_model)
            added_count += 1
        else:
            project_model.status = status
            updated_count += 1

        project_model.full_name = project_status.name
        project_model.category = project_status.category
        project_model.description = project_status.description
        project_model.programming_languages = project_status.programming_language

    return added_count, updated_count


async def _update_tooling(data: db.Session) -> tuple[int, int]:
    added_count = 0
    updated_count = 0

    # Tooling is not a committee
    # We add a special entry for Tooling, pretending to be a PMC, for debugging and testing
    tooling_committee = await data.committee(name="tooling").get()
    if not tooling_committee:
        tooling_committee = sql.Committee(name="tooling", full_name="Tooling")
        data.add(tooling_committee)
        tooling_project = sql.Project(name="tooling", full_name="Apache Tooling", committee=tooling_committee)
        data.add(tooling_project)
        added_count += 1
    else:
        updated_count += 1

    # Update Tooling PMC data
    # Could put this in the "if not tooling_committee" block, perhaps
    tooling_committee.committee_members = ["wave", "tn", "sbp"]
    tooling_committee.committers = ["wave", "tn", "sbp"]
    tooling_committee.release_managers = ["wave"]
    tooling_committee.is_podling = False

    return added_count, updated_count
