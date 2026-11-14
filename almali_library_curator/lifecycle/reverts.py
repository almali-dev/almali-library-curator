"""Reverting the arr side effects a failed removal left behind (#69).

A removal that fails after the arr was mutated leaves the title
unmonitored and on the import exclusion list. The executor records every
effect it applies (``removal_requests.applied_effects``), and every
cancellation reverts that record through the arr clients before it
becomes durable — so a cancelled removal truly takes the title back.

This module stays arr-agnostic, like the rest of the lifecycle package:
each client is injected under the service name an applied effect
records, and knows its own restore calls. A resource the service no
longer holds counts as already reverted. Any other refusal raises
:class:`SideEffectRevertError` carrying the effect, so the caller keeps
it in the record of what is still applied and never claims a revert
that did not happen.
"""

from __future__ import annotations

import logging

from almali_library_curator.lifecycle.models import AppliedEffect, SideEffectRevertError

logger = logging.getLogger(__name__)


class ArrReverter:
    """Undoes recorded arr side effects through the injected arr clients.

    The clients are passed one per arr service, keyworded by the service
    name an applied effect records; either may be absent, exactly as in
    the executor. A service without its client cannot have its effects
    undone, and the reverter says so instead of silently skipping them.
    """

    def __init__(self, **arrs) -> None:
        self._arrs = dict(arrs)

    def __repr__(self) -> str:
        configured = ", ".join(sorted(self._arrs)) or "none"
        return f"ArrReverter({configured})"

    def revert(self, effect: AppliedEffect) -> None:
        """Undo one applied effect.

        Restores monitoring for an ``unmonitor`` (the resource, or exactly
        the seasons it stopped monitoring) and deletes the stored import
        exclusion for an ``exclusion``. A resource the service no longer
        holds counts as already reverted; every other refusal raises
        :class:`SideEffectRevertError` with this effect still applied.
        """
        client = self._arrs.get(effect.arr)
        if client is None:
            raise SideEffectRevertError(
                (effect,),
                f"no {effect.arr} client is configured to revert {effect.kind}",
            )
        try:
            client.revert(effect)
        except Exception as error:
            if getattr(error, "resource_gone", False):
                # The effect no longer exists on the service: the arr
                # already reverted it, whatever removed the resource.
                logger.info(
                    "removal_effect_already_reverted",
                    extra={"arr": effect.arr, "kind": effect.kind},
                )
                return
            logger.warning(
                "removal_effect_revert_failed",
                extra={
                    "arr": effect.arr,
                    "kind": effect.kind,
                    "error": type(error).__name__,
                },
            )
            raise SideEffectRevertError(
                (effect,), f"reverting the {effect.arr} {effect.kind} failed: {error}"
            ) from error
