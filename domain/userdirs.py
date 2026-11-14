"""The XDG user-dirs file (~/.config/user-dirs.dirs) that says where Music, Videos and Downloads are on Linux."""

import re

_LINE = re.compile(r'^\s*XDG_([A-Z]+)_DIR\s*=\s*"(.*)"\s*$')


def parse_user_dirs(text: str, home: str) -> dict[str, str]:
    """{"MUSIC": "/home/me/Music", ...}; "$HOME/x" is expanded, and a dir equal to $HOME means "not set"."""
    dirs: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        name, value = match.groups()
        value = value.replace('\\"', '"')
        if value.startswith("$HOME"):
            value = home + value[len("$HOME") :]
        elif not value.startswith("/"):
            continue  # only absolute paths or $HOME-relative ones are valid
        if value.rstrip("/") != home.rstrip("/"):
            dirs[name] = value
    return dirs
