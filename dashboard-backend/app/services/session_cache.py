"""
In-memory cache of the session archive for fast time-window queries.

The dashboard polls /topics/data and /topics/image several times a second
per panel; re-reading and re-parsing whole JSONL files (or re-globbing the
frame directory) on every request costs O(file size) and stalls the whole
backend once a few panels are open. This cache parses each data.jsonl once,
tails only appended bytes on subsequent requests (live recording keeps
growing the files), and answers window queries with a binary search.

Memory note: decoded entries for the whole session are kept resident. For
typical HRI sessions this is tens of MB; if bags grow far beyond that, an
eviction strategy belongs here.
"""
from __future__ import annotations

import json
import threading
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class _TopicData:
    ino: int = -1          # file identity — a wiped/recreated file resets the cache
    offset: int = 0        # bytes already consumed
    entries: list = field(default_factory=list)
    ts: list = field(default_factory=list)   # parallel array of entry["t"] for bisect


@dataclass
class _FrameIndex:
    mtime_ns: int = -1
    ts: list = field(default_factory=list)
    names: list = field(default_factory=list)


class SessionCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, _TopicData] = {}
        self._frames: dict[str, _FrameIndex] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._data.clear()
            self._frames.clear()

    # -- windowed data -------------------------------------------------------

    def window(self, jsonl: Path, slug: str, lo_t: float, hi_t: float) -> list:
        """Entries with lo_t <= t <= hi_t, parsing only bytes appended since
        the previous call. Entries are appended in time order by both the
        indexer and the live-capture node."""
        with self._lock:
            try:
                st = jsonl.stat()
            except FileNotFoundError:
                self._data.pop(slug, None)
                return []

            c = self._data.get(slug)
            if c is None or st.st_ino != c.ino or st.st_size < c.offset:
                c = self._data[slug] = _TopicData(ino=st.st_ino)

            if st.st_size > c.offset:
                with jsonl.open("rb") as f:
                    f.seek(c.offset)
                    chunk = f.read()
                # A live writer may be mid-line at EOF — consume whole lines only.
                nl = chunk.rfind(b"\n")
                if nl >= 0:
                    for line in chunk[:nl].split(b"\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        c.entries.append(e)
                        c.ts.append(e.get("t", 0.0))
                    c.offset += nl + 1

            lo = bisect_left(c.ts, lo_t)
            hi = bisect_right(c.ts, hi_t)
            return c.entries[lo:hi]

    # -- image frames --------------------------------------------------------

    def nearest_frame(self, tdir: Path, slug: str, t: float) -> Optional[Path]:
        """Path of the JPEG frame closest to t, rescanning the directory only
        when its mtime changes."""
        with self._lock:
            try:
                mt = tdir.stat().st_mtime_ns
            except FileNotFoundError:
                self._frames.pop(slug, None)
                return None

            c = self._frames.get(slug)
            if c is None or mt != c.mtime_ns:
                pairs = []
                for f in tdir.glob("*.jpg"):
                    if f.stem == "latest":
                        continue
                    try:
                        pairs.append((float(f.stem), f.name))
                    except ValueError:
                        continue
                pairs.sort()
                c = self._frames[slug] = _FrameIndex(
                    mtime_ns=mt,
                    ts=[p[0] for p in pairs],
                    names=[p[1] for p in pairs],
                )

            if not c.ts:
                return None
            i = bisect_left(c.ts, t)
            best = min(
                (j for j in (i - 1, i) if 0 <= j < len(c.ts)),
                key=lambda j: abs(c.ts[j] - t),
            )
            return tdir / c.names[best]


session_cache = SessionCache()
