"""The `Datasette(...)` factory the `ui` CLI command and test_observatory.py
both share -- one place that wires cfg.db_path + cfg.ui_token + the plugin
module together, so the two never drift.

Read-only by construction, not just by convention: `files=[cfg.db_path]`
WITHOUT `immutables=` is what makes Datasette open its own read connections
as `file:...?mode=ro` (see Database.connect() in the installed datasette
package) while still tailing a live-changing discovery.db -- `immutable=1`
would instead snapshot the file's schema/stats once at startup, which is
wrong for a db this step's own ops (`web-tick` etc.) keep writing to while
the UI is open. observatory/db.py's own queries additionally open their own
independent `mode=ro` connection per request, so the read-only guarantee
does not depend on Datasette's internal connection handling alone.
"""
import os
from datasette.app import Datasette
from datasette.plugins import pm

from . import plugin


def _ensure_plugin_registered():
    if not pm.is_registered(plugin):
        pm.register(plugin, name="observatory")


def build_datasette(cfg, public=False):
    """`public=True` requires cfg.ui_token to be set -- callers (the `ui`
    CLI command) are expected to have already refused to start otherwise;
    this factory itself just asserts it, so a test can't accidentally build
    a public-but-unauthenticated instance either."""
    if public and not cfg.ui_token:
        raise ValueError("public mode requires cfg.ui_token")
    _ensure_plugin_registered()
    # discovery.db first so it stays the default database in the UI and in
    # every existing row/table URL; extras are appended, never inserted.
    # A configured path that isn't there is skipped -- a sibling project
    # being absent or mid-clone must not stop the Observatory booting.
    files = [cfg.db_path]
    for extra in getattr(cfg, "ui_extra_dbs", ()) or ():
        if extra and os.path.exists(extra) and extra not in files:
            files.append(extra)
    ds = Datasette(files=files, settings={"default_allow_sql": True})
    ds._observatory_db_path = cfg.db_path
    # The write API needs more than the db path (interests.json, the
    # candidates artifact), so the whole cfg rides along.
    ds._observatory_cfg = cfg
    ds._observatory_public = public
    ds._observatory_token = cfg.ui_token
    return ds
