import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import urljoin

import m3u8
import requests
from better_ffmpeg_progress import FfmpegProcess
from requests.adapters import HTTPAdapter, Retry

from anipy_cli.anime import Anime
from anipy_cli.colors import color, colors
from anipy_cli.config import Config
from anipy_cli.error import DownloadError, RequestError
from anipy_cli.provider import ProviderStream


class ProgressCallback(Protocol):
    def __call__(self, percentage: float): ...


class InfoCallback(Protocol):
    def __call__(self, message: str): ...


class Downloader:
    def __init__(
        self, progress_callback: ProgressCallback, info_callback: InfoCallback
    ):
        self.progress_callback = progress_callback
        self.info_callback = info_callback

        self._session = requests.Session()

        adapter = HTTPAdapter(max_retries=Retry(connect=3, backoff_factor=0.5))
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    @staticmethod
    def _get_valid_pathname(name: str):
        if sys.platform == "win32":
            WIN_INVALID_CHARS = ["\\", "/", ":", "*", "?", "<", ">", "|", '"']
            name = "".join(["" if x in WIN_INVALID_CHARS else x for x in name])

        name = "".join(
            [i for i in name if i.isascii()]
        )  # Verify all chars are ascii (eject if not)
        name = "-".join(name.split())  # Clean all white spaces, including tabs and such

        return name

    @staticmethod
    def get_download_path(
        anime: Anime,
        stream: ProviderStream,
        parent_directory: Optional[Path] = None,
    ) -> Path:
        download_folder = parent_directory or Config().download_folder_path

        anime_name = Downloader._get_valid_pathname(anime.name)

        if Config().download_remove_dub_from_folder_name:
            if anime_name.endswith(" (Dub)"):
                anime_name = f"{anime_name[:-6]}"

        return download_folder / anime_name / Config().download_name_format.format(
            show_name=anime_name,
            episode_number=stream.episode,
            quality=stream.resolution,
        )

    def m3u8_download(self, stream: ProviderStream, download_path: Path) -> Path:
        self.info_callback(str(download_path))
        temp_folder = download_path.parent / "temp"
        temp_folder.mkdir(exist_ok=True)

        res = self._session.get(stream.url)
        if not res.ok:
            raise RequestError(res.url, res.status_code)

        m3u8_content = m3u8.M3U8(res.text, base_uri=urljoin(res.url, "."))

        assert m3u8_content.is_variant is False

        counter = 0

        def download_ts(segment: m3u8.Segment):
            nonlocal counter
            url = Path(segment.base_uri) / segment.uri
            fname = temp_folder / self._get_valid_pathname(segment.uri)
            self.info_callback(f"{str(url)} -> {str(fname)}")
            try:
                res = self._session.get(str(url))
                if not res.ok:
                    raise RequestError(res.url, res.status_code)

                with fname.open("wb") as fout:
                    fout.write(res.content)

                counter += 1
                self.progress_callback(counter / len(m3u8_content.segments) * 100)
            except Exception as e:
                DownloadError(f"Encountered this error while downloading: {str(e)}")
        try:
            with ThreadPoolExecutor(12) as pool_video:
                pool_video.map(download_ts, m3u8_content.segments)
            exit()

            self.info_callback(color(colors.CYAN, "Parts Downloaded"))

            self.info_callback(color(colors.CYAN, "Merging Parts"))
            with download_path.with_suffix(".ts").open("wb") as merged:
                for segment in m3u8_content.segments:
                    fname = temp_folder.with_name(self._get_valid_pathname(segment.uri))
                    if not fname.is_file():
                        raise DownloadError(
                            f"Could not merge, missing a segment of this playlist: {stream.url}"
                        )

                    with fname.open("rb") as mergefile:
                        shutil.copyfileobj(mergefile, merged)

            self.info_callback(color(colors.CYAN, "Merge Finished"))
            shutil.rmtree(temp_folder)

            return download_path.with_suffix(".ts")
        except KeyboardInterrupt:
            self.info_callback("Download Interrupted, deleting partial file.")
            download_path.unlink()
            shutil.rmtree(temp_folder)

    def mp4_download(self, stream: ProviderStream, download_path: Path) -> Path:
        r = self._session.get(stream.url, stream=True)
        total = int(r.headers.get("content-length", 0))
        try:
            file_handle = download_path.with_suffix(".mp4").open("w")
            for data in r.iter_content(chunk_size=1024):
                size = file_handle.write(data)
                self.progress_callback(size / total * 100)
        except KeyboardInterrupt:
            self.info_callback("Download Interrupted, deleting partial file.")
            download_path.unlink()

        self.info_callback(color(colors.CYAN, "Download finished."))

        return download_path.with_suffix(".mp4")

    def ffmpeg_download(
        self, stream: ProviderStream, download_path: Path, ffmpeg_log_path: Path
    ) -> Path:
        ffmpeg_process = FfmpegProcess(
            [
                "ffmpeg",
                "-i",
                stream.url,
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-c:s",
                "mov_text",
                "-c",
                "copy",
                str(download_path),
            ]
        )

        try:
            ffmpeg_process.run(
                ffmpeg_output_file=str(
                    ffmpeg_log_path / download_path.name.replace("mp4", "log")
                ),
                progress_handler=lambda p, s, a, e: self.progress_callback(p),
            )
        except KeyboardInterrupt:
            self.info_callback("interrupted deleting partially downloaded file")
            download_path.unlink()

        return download_path

    def download(
        self,
        stream: ProviderStream,
        anime: Anime,
        ffmpeg: bool = False,
        download_path: Optional[Path] = None,
    ) -> Path:
        config = Config()

        if not download_path:
            download_path = self.get_download_path(anime, stream)

        download_path.parent.mkdir(parents=True, exist_ok=True)

        if "m3u8" in stream.url:
            if ffmpeg or config.ffmpeg_hls:
                self.info_callback("Using FFMPEG downloader")
                config.ffmpeg_log_path.mkdir(exist_ok=True, parents=True)
                return self.ffmpeg_download(
                    stream, download_path, config.ffmpeg_log_path
                )

            self.info_callback("Using internal M3U8 downloader")
            return self.m3u8_download(stream, download_path)
        elif "mp4" in stream.url:
            self.info_callback("Using internal MP4 downloader")
            return self.mp4_download(stream, download_path)
        else:
            self.info_callback(
                "No fitting downloader available for stream, using FFMPEG downloader as fallback"
            )
            config.ffmpeg_log_path.mkdir(exist_ok=True, parents=True)
            return self.ffmpeg_download(stream, download_path, config.ffmpeg_log_path)
