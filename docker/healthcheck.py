"""Docker HEALTHCHECK probe for /healthz.

Liveness contract (L98 decision d): HTTP 200 with ``"status": "ok"`` is
healthy, including while Plex is not configured yet (``ready`` false,
``missing`` ["plex"]). Anything else is unhealthy.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:17540/healthz"


def is_healthy(status_code: int, body: bytes) -> bool:
    if status_code != 200:
        return False
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=4) as response:
            return 0 if is_healthy(response.status, response.read()) else 1
    except urllib.error.HTTPError as error:
        return 0 if is_healthy(error.code, error.read()) else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
