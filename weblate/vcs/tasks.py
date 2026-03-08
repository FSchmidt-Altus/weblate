# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from weblate.utils.celery import app


@app.task(trail=False)
def refresh_github_app_token() -> None:
    from weblate.vcs.github_app import refresh_github_app_token as do_refresh

    do_refresh()


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs) -> None:
    sender.add_periodic_task(
        3300,
        refresh_github_app_token.s(),
        name="refresh-github-app-token",
    )
