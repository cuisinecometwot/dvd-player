"""Shared HTTP GET for the online lookups.

Distinguishes "the service says there is nothing" (None, safe to remember) from "couldn't ask"
(NetworkError, must not be remembered as a miss, or an offline evening would hide results for a week).
"""

import urllib.error
import urllib.request

USER_AGENT = "dvd-player/1.0 ( personal desktop tool )"


class NetworkError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def get(url: str, *, timeout: float, max_bytes: int = 4 * 2**20, accept: str = "") -> bytes | None:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise NetworkError(f"HTTP {e.code}", e.code) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NetworkError(str(getattr(e, "reason", e))) from e
    if len(data) > max_bytes:
        raise NetworkError("response too large")
    return data
