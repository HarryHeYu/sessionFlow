"""Windows console suppression for Voyager's background subprocesses.

Voyager's hooks and watchers run from windowless parents (provider hook
dispatch, daemons, scheduled tasks).  On Windows, every console child of a
windowless parent pops up a new console window, so the background git probes
must be created with ``CREATE_NO_WINDOW``.

Capture, returncode, timeout and text/encoding semantics are unchanged --
this only controls the child's window visibility.  Non-Windows platforms
get no extra kwargs at all.
"""

from __future__ import annotations

import os


def background_subprocess_kwargs() -> dict:
    """Extra kwargs for ``subprocess.run`` of background child processes.

    On Windows returns ``{"creationflags": subprocess.CREATE_NO_WINDOW}``;
    everywhere else returns ``{}``.  Merge it into the call -- never pass it
    positionally.
    """
    if os.name == "nt":
        import subprocess
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
