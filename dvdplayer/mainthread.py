"""Hands work from background threads (lookups, rips, VLC frames) to the UI thread, whatever the toolkit.

The UI installs its own way of doing that at startup (GLib.idle_add for GTK, a queued signal for Qt).
"""

from collections.abc import Callable

_post: Callable[[Callable[[], None]], None] | None = None


def install(post: Callable[[Callable[[], None]], None]) -> None:
    global _post
    _post = post


def call(fn: Callable, *args) -> None:
    """Run fn(*args) soon on the UI thread; safe from any thread. Its return value is ignored."""
    if _post is None:
        raise RuntimeError("mainthread.install() has not been called")
    _post(lambda: fn(*args))
