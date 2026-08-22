"""Media resolution and decoding.

Two decisions here matter for throughput:

1. Ask YouTube for the SMALLEST stream available. Output is at most 128x128, so
   a 144p source loses nothing and cuts decode cost enormously.
2. Prefer avc1 (H.264) over vp09. Measured ~1.3 ms/frame vs ~10 ms/frame for an
   equivalent 256x144 stream. yt-dlp's plain `worstvideo` often picks VP9.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

import av
import yt_dlp
from PIL import Image

from .codec import square_resize


@dataclass
class MediaInfo:
    url: str
    title: str = ""
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    vcodec: str = ""
    format_id: str = ""
    webpage_url: str = ""

    @property
    def is_remote(self) -> bool:
        return self.url.startswith(("http://", "https://"))


def _fmt_selector(max_h: int, prefer_h264: bool) -> str:
    if prefer_h264:
        return (f"worstvideo[height>={max_h}][vcodec^=avc1]/"
                f"worstvideo[height>={max_h}]/worstvideo/worst")
    return f"worstvideo[height>={max_h}]/worstvideo/worst"


def resolve(target: str, max_h: int = 144, prefer_h264: bool = True) -> MediaInfo:
    """Resolve a YouTube URL / ytsearch query / local path to a decodable stream."""
    if "://" not in target and not target.startswith("ytsearch"):
        return MediaInfo(url=target, title=target)
    opts = {"quiet": True, "no_warnings": True, "format": _fmt_selector(max_h, prefer_h264)}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(target, download=False)
    if "entries" in info:
        info = info["entries"][0]
    return MediaInfo(url=info["url"], title=info.get("title", ""),
                     duration=info.get("duration"), width=info.get("width"),
                     height=info.get("height"), vcodec=info.get("vcodec", ""),
                     format_id=str(info.get("format_id", "")),
                     webpage_url=info.get("webpage_url", target))


def fetch_audio(target: str, out_path: str) -> str:
    """Download the audio track to a local file (used by the CLI's --audio mode)."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                           "format": "bestaudio[ext=m4a]/bestaudio",
                           "outtmpl": out_path, "overwrites": True}) as y:
        y.download([target])
    return out_path


class FrameSource(threading.Thread):
    """Decode -> centre-crop -> resize, pushing PIL images onto a queue.

    Images are emitted UN-posterised so the rate controller can re-render the
    same batch at any quality rung without re-decoding.
    """

    def __init__(self, url: str, size: int, fps: float, *, seconds: float | None = None,
                 start: float = 0.0, maxsize: int = 240):
        super().__init__(daemon=True)
        self.url, self.size, self.fps = url, size, fps
        self.seconds, self.start = seconds, start
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.error: Exception | None = None
        self.frames_out = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        container = None
        try:
            container = av.open(self.url, options={"rw_timeout": "15000000"})
            vs = container.streams.video[0]
            vs.thread_type = "AUTO"
            tb = vs.time_base
            if self.start > 0:
                try:
                    container.seek(int(self.start / tb), stream=vs)
                except Exception:
                    pass
            next_t, base = 0.0, None
            for f in container.decode(vs):
                if self._stop.is_set():
                    break
                if f.pts is None:
                    continue
                t = float(f.pts * tb)
                if base is None:
                    base = t
                t -= base
                if self.seconds is not None and t > self.seconds:
                    break
                while next_t <= t:
                    if self._stop.is_set():
                        break
                    img = square_resize(f.to_image(), self.size, smooth=True)
                    self.q.put(img)
                    self.frames_out += 1
                    next_t += 1.0 / self.fps
        except Exception as e:
            self.error = e
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            self.q.put(None)
