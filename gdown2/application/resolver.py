"""Target resolution service (URL/ID -> DriveItem)."""

from __future__ import annotations

from gdown2.application.ports import DriveSource, MetadataCache
from gdown2.domain.models import DriveItem
from gdown2.domain.url_parser import ParsedTarget, TargetKind, parse_target


class Resolver:
    """Resolves user-supplied URLs or IDs into :class:`DriveItem` metadata."""

    def __init__(
        self,
        source: DriveSource,
        cache: MetadataCache | None = None,
        follow_shortcuts: bool = True,
    ) -> None:
        self._source = source
        self._cache = cache
        self._follow_shortcuts = follow_shortcuts

    async def resolve(self, url_or_id: str | ParsedTarget) -> DriveItem:
        """Resolve ``url_or_id`` to its remote :class:`DriveItem`."""
        target = url_or_id if isinstance(url_or_id, ParsedTarget) else parse_target(url_or_id)

        if self._cache is not None:
            cached = self._cache.get(target.id)
            if cached is not None:
                return cached

        item = await self._source.fetch_metadata(target)
        if target.resource_key and not item.resource_key:
            item.resource_key = target.resource_key

        if self._follow_shortcuts and item.is_shortcut and item.shortcut_target_id:
            shortcut_name = item.name
            target_info = ParsedTarget(
                id=item.shortcut_target_id,
                kind=TargetKind.UNKNOWN,
            )
            item = await self._source.fetch_metadata(target_info)
            if shortcut_name:
                item.name = shortcut_name

        if self._cache is not None:
            self._cache.put(item)

        return item

