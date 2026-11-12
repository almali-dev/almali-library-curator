"""Removing a media item through Plex (S15 rules 4 and 7).

Plex is the deletion fallback when the responsible Sonarr or Radarr is
absent or switched off. ``DELETE /library/metadata/{ratingKey}`` removes the
item's file; the caller still verifies the files on disk before it reports a
deletion, so a success answer from Plex is never proof on its own.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from almali_library_curator.plex.client import (
    PlexAuthError,
    PlexClient,
    PlexError,
    PlexNotFoundError,
)

logger = logging.getLogger(__name__)


class PlexDeletionRefused(PlexError):
    """Plex accepted the token but refused to delete media.

    A Plex Media Server answers a delete with 403 while its "Allow media
    deletion" setting is off. This is neither a network failure nor a
    rejected token, so the owner can be told which setting to change.
    """


class PlexMultiVersionError(PlexError):
    """The item carries more than one version, each with its own file.

    ``DELETE /library/metadata/{ratingKey}`` removes the whole item, every
    version included, while the app's marked scope covers one file. The
    payload carries no marker of the primary version (verified read-only on
    a live server, 2026-11-13), so no deletion may depend on the order of
    the ``Media`` entries: the refusal is the owner's decision to make.
    """


def delete_media_item(client: PlexClient, rating_key: str) -> bool:
    """Ask Plex to delete one item; ``False`` when Plex no longer knows it.

    The item is read again before anything is sent (S3): when more than
    one version carries a distinct file the request refuses, sends nothing
    and leaves every file untouched, because the app stores one path and
    one size per item and the marked scope cannot cover the other
    versions. A single-version item keeps today's behaviour.

    The client turns 401 and 403 into ``PlexAuthError``. Only a token that
    still reads the server identity turns that answer into a refusal; a
    token the server rejects stays an authentication error.
    """
    path = f"/library/metadata/{quote(str(rating_key), safe='')}"
    try:
        files = client.media_files(rating_key)
    except PlexNotFoundError:
        logger.info("plex_delete_not_found", extra={"rating_key": str(rating_key)})
        return False
    except PlexAuthError:
        client.server_identity()  # Raises PlexAuthError for a rejected token.
        logger.warning("plex_delete_refused", extra={"rating_key": str(rating_key)})
        raise PlexDeletionRefused(
            'Plex refused to delete the media: turn on "Allow media deletion"'
            " in the Plex server settings"
        ) from None
    if len(files) > 1:
        logger.warning(
            "plex_delete_multi_version",
            extra={"rating_key": str(rating_key), "versions": len(files)},
        )
        raise PlexMultiVersionError(
            "Plex item has several versions with distinct files; only one "
            "file can be verified, so the deletion was refused"
        )
    try:
        client._request(path, method="DELETE", expect_body=False)
    except PlexNotFoundError:
        logger.info("plex_delete_not_found", extra={"rating_key": str(rating_key)})
        return False
    except PlexAuthError:
        client.server_identity()  # Raises PlexAuthError for a rejected token.
        logger.warning("plex_delete_refused", extra={"rating_key": str(rating_key)})
        raise PlexDeletionRefused(
            'Plex refused to delete the media: turn on "Allow media deletion"'
            " in the Plex server settings"
        ) from None
    return True
