"""Removing a media item through Plex (S15 rules 4 and 7).

Plex is the deletion fallback when the responsible Sonarr or Radarr is
absent or switched off. ``DELETE /library/metadata/{ratingKey}`` removes the
item's file; the caller still verifies the files on disk before it reports a
deletion, so a success answer from Plex is never proof on its own.
``preflight_media_item`` shares the read-only existence and multi-version
checks with dry-run previews; only ``delete_media_item`` sends a mutation.
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


class PlexVersionChanged(PlexError):
    """The version to remove is no longer what the owner marked (#133).

    Its media id is gone or now points at another file, or no other
    version with a file of its own would remain. Nothing is sent.
    """


class PlexVersionParts(PlexError):
    """The version to remove is stacked over several files (#133).

    The app stores one file per version, so it could not confirm the other
    parts are gone; the removal is refused and nothing is sent.
    """


def _refused() -> PlexDeletionRefused:
    return PlexDeletionRefused(
        'Plex refused to delete the media: turn on "Allow media deletion"'
        " in the Plex server settings"
    )


def preflight_media_item(client: PlexClient, rating_key: str) -> bool:
    """Check one item read-only; ``False`` when Plex no longer knows it.

    The item is read again before anything is sent (S3): when more than
    one version carries a distinct file the request refuses, sends nothing
    and leaves every file untouched, because the app stores one path and
    one size per item and the marked scope cannot cover the other
    versions. A single-version item keeps today's behaviour.

    The client turns 401 and 403 into ``PlexAuthError``. Only a token that
    still reads the server identity turns that answer into a refusal; a
    token the server rejects stays an authentication error.
    """
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
    return True


def delete_media_item(client: PlexClient, rating_key: str) -> bool:
    """Delete only after the shared read-only proof; a missing item is ``False``.

    A race after preflight can still yield a 404 or an authentication error
    on DELETE; those keep the same interpretation as the read response.
    The caller must verify the files on disk before claiming deletion.
    """
    if not preflight_media_item(client, rating_key):
        return False
    path = f"/library/metadata/{quote(str(rating_key), safe='')}"
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


def preflight_media_version(
    client: PlexClient, rating_key: str, media_id: int, expected_path: str,
) -> bool:
    """Check one version read-only; ``False`` when Plex no longer knows the item.

    #133: the item is read again before anything is sent. The version must
    still exist under ``media_id``, be one single part whose mapped file is
    ``expected_path`` (the file the owner marked), and another version with
    a different file must remain, so the item itself always stays. Any
    doubt raises and nothing is sent. 401/403 read like the whole-item
    preflight: a refusal when the token still reads the server identity.
    """
    try:
        item = client.metadata(rating_key)
    except PlexNotFoundError:
        logger.info("plex_delete_not_found", extra={"rating_key": str(rating_key)})
        return False
    except PlexAuthError:
        client.server_identity()  # Raises PlexAuthError for a rejected token.
        logger.warning("plex_delete_refused", extra={"rating_key": str(rating_key)})
        raise _refused() from None
    target = [media for media in item.Media if media.id == media_id]
    if len(target) != 1:
        raise PlexVersionChanged("the version to remove is no longer on the item")
    parts = [part for part in target[0].Part if part.file]
    if len(parts) != 1 or len(target[0].Part) != 1:
        raise PlexVersionParts("the version to remove is stacked over several files")
    if client.map_path(parts[0].file) != expected_path:
        raise PlexVersionChanged("the version to remove now holds another file")
    kept = {
        client.map_path(part.file)
        for media in item.Media if media.id != media_id
        for part in media.Part if part.file
    }
    if not kept - {expected_path}:
        raise PlexVersionChanged("no other version with its own file would remain")
    return True


def delete_media_version(
    client: PlexClient, rating_key: str, media_id: int, expected_path: str,
) -> bool:
    """Delete one version after the read-only proof; a missing one is ``False``.

    Sends ``DELETE /library/metadata/{ratingKey}/media/{mediaId}``, the
    request Plex Web sends for one version. As with a whole item, the
    caller must verify on disk that this file is gone and that the kept
    files are still there before claiming anything.
    """
    if not preflight_media_version(client, rating_key, media_id, expected_path):
        return False
    path = (f"/library/metadata/{quote(str(rating_key), safe='')}"
            f"/media/{quote(str(media_id), safe='')}")
    try:
        client._request(path, method="DELETE", expect_body=False)
    except PlexNotFoundError:
        logger.info("plex_delete_version_not_found",
                    extra={"rating_key": str(rating_key), "media_id": media_id})
        return False
    except PlexAuthError:
        client.server_identity()  # Raises PlexAuthError for a rejected token.
        logger.warning("plex_delete_refused", extra={"rating_key": str(rating_key)})
        raise _refused() from None
    return True
