from dataclasses import dataclass
from typing import Dict, List, Optional

from dataclasses_json import DataClassJsonMixin

from anipy_cli.anime import Anime
from anipy_cli.cli.util import get_prefered_providers
from anipy_cli.config import Config
from anipy_cli.mal import (
    MALAnime,
    MALMyListStatus,
    MALMyListStatusEnum,
    MyAnimeList,
    MyAnimeListAdapter,
)
from anipy_cli.provider.providers import list_providers


@dataclass
class ProviderMapping(DataClassJsonMixin):
    provider: str
    name: str
    identifier: str
    has_dub: bool


@dataclass
class MALProviderMapping(DataClassJsonMixin):
    mal_anime: MALAnime
    mappings: Dict[str, ProviderMapping]


@dataclass
class MALLocalList(DataClassJsonMixin):
    mappings: Dict[int, MALProviderMapping]

    def write(self):
        local_list = Config()._mal_local_user_list_path
        local_list.write_text(self.to_json())

    @staticmethod
    def read() -> "MALLocalList":
        local_list = Config()._mal_local_user_list_path

        if not local_list.is_file():
            local_list.parent.mkdir(exist_ok=True, parents=True)
            return MALLocalList({})

        try:
            mylist: MALLocalList = MALLocalList.from_json(local_list.read_text())
        except KeyError:
            raise
            ...

        return mylist


class MyAnimeListProxy:
    def __init__(self, mal: MyAnimeList):
        self.mal = mal
        self.local_list = MALLocalList.read()

    def _cache_list(self, mylist: List[MALAnime]):
        for e in mylist:
            if self.local_list.mappings.get(e.id, None):
                self.local_list.mappings[e.id].mal_anime = e
            else:
                self.local_list.mappings[e.id] = MALProviderMapping(e, {})

        self.local_list.write()

    def _write_mapping(self, mal_anime: MALAnime, mapping: Anime):
        self._cache_list([mal_anime])

        self.local_list.mappings[mal_anime.id].mappings[
            f"{mapping.provider.NAME}:{mapping.identifier}"
        ] = ProviderMapping(
            mapping.provider.NAME, mapping.name, mapping.identifier, mapping.has_dub
        )

        self.local_list.write()

    def get_list(self) -> List[MALAnime]:
        config = Config()

        mylist = []
        for s in config.mal_status_categories:
            mylist.extend(
                filter(
                    lambda e: (
                        config.mal_ignore_tag not in e.my_list_status.tags
                        if e.my_list_status
                        else True
                    ),
                    self.mal.get_anime_list(MALMyListStatusEnum[s.upper()]),
                )
            )

        self._cache_list(mylist)
        return mylist

    def update_show(
        self,
        anime: MALAnime,
        status: Optional[MALMyListStatusEnum] = None,
        episode: Optional[int] = None,
    ) -> MALMyListStatus:
        config = Config()

        if anime.my_list_status:
            if status:
                anime.my_list_status.status = status
            if episode:
                anime.my_list_status.num_episodes_watched = episode

        self._cache_list([anime])
        return self.mal.update_anime_list(
            anime.id, status=status, watched_episodes=episode, tags=config.mal_tags
        )

    def delete_show(self, anime: MALAnime) -> None:
        self.local_list.mappings.pop(anime.id)
        self.local_list.write()

        self.mal.delete_from_anime_list(anime.id)

    def map_from_mal(
        self, anime: MALAnime, mapping: Optional[Anime] = None
    ) -> Optional[Anime]:
        if mapping is not None:
            self._write_mapping(anime, mapping)
            return mapping

        if self.local_list.mappings[anime.id].mappings:
            map = list(self.local_list.mappings[anime.id].mappings.values())[0]
            provider = next(filter(lambda x: x.NAME == map.provider, list_providers()))
            return Anime(provider(), map.name, map.identifier, map.has_dub)

        config = Config()
        result = None
        for p in get_prefered_providers():
            adapter = MyAnimeListAdapter(self.mal, p)
            result = adapter.from_myanimelist(
                anime,
                config.mal_mapping_min_similarity,
                config.mal_mapping_use_filters,
                config.mal_mapping_use_alternatives,
            )

            if result is not None:
                break

        if result:
            self._write_mapping(anime, result)

        return result

    def map_from_provider(
        self, anime: Anime, mapping: Optional[MALAnime]
    ) -> Optional[MALAnime]:
        if mapping is not None:
            self._write_mapping(mapping, anime)
            return mapping

        for _, m in self.local_list.mappings.items():
            existing = m.mappings.get(f"{anime.provider.NAME}:{anime.identifier}", None)
            if existing:
                return m.mal_anime

        config = Config()
        adapter = MyAnimeListAdapter(self.mal, anime.provider)
        result = adapter.from_provider(
            anime,
            config.mal_mapping_min_similarity,
            config.mal_mapping_use_alternatives,
        )

        if result:
            self._write_mapping(result, anime)

        return result
