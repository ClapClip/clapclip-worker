"""Conservative native-fragment plans for one freshly selected component.

URLs remain private attempt data. Plans never estimate timestamps from bytes.
"""
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import parse_qsl, urlsplit


class UnsupportedPrefix(ValueError):
    pass


def hls_prefix(text, window):
    """Retain original VOD lines/sequence/key/range semantics through boundary."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > 8 * 1024 * 1024:
        raise UnsupportedPrefix()
    lines = text.splitlines()
    if not lines or lines[0].strip() != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        raise UnsupportedPrefix()
    allowed = {"#EXTM3U", "#EXT-X-VERSION", "#EXT-X-TARGETDURATION", "#EXT-X-MEDIA-SEQUENCE",
               "#EXT-X-PLAYLIST-TYPE", "#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-KEY",
               "#EXT-X-MAP", "#EXT-X-BYTERANGE", "#EXTINF", "#EXT-X-ENDLIST",
               "#EXT-X-ALLOW-CACHE", "#EXT-X-PROGRAM-DATE-TIME", "#EXT-X-DISCONTINUITY-SEQUENCE"}
    elapsed = Decimal(0)
    pending = None
    cutoff = None
    count = total = 0
    seen_map = False
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            tag = line.split(":", 1)[0]
            if (tag.startswith(("#EXT", "#ANVATO", "#UPLYNK")) and tag not in allowed):
                raise UnsupportedPrefix()
            if tag == "#EXT-X-PLAYLIST-TYPE" and line != "#EXT-X-PLAYLIST-TYPE:VOD":
                raise UnsupportedPrefix()
            if tag == "#EXT-X-MAP":
                if total or seen_map:
                    raise UnsupportedPrefix()
                seen_map = True
            if tag == "#EXT-X-KEY" and not re.match(r"#EXT-X-KEY:METHOD=(?:NONE|AES-128)(?:,|$)", line):
                raise UnsupportedPrefix()
            if tag == "#EXTINF":
                if pending is not None:
                    raise UnsupportedPrefix()
                try:
                    pending = Decimal(line.split(":", 1)[1].split(",", 1)[0])
                except (InvalidOperation, IndexError):
                    raise UnsupportedPrefix() from None
                if not pending.is_finite() or pending <= 0:
                    raise UnsupportedPrefix()
        else:
            if pending is None:
                raise UnsupportedPrefix()
            total += 1
            if cutoff is None:
                remaining=Decimal(window)-elapsed
                elapsed=Decimal(window) if pending>=remaining else elapsed+pending
                count += 1
                if elapsed >= window:
                    cutoff = index + 1
            pending = None
    if pending is not None or cutoff is None or not total:
        raise UnsupportedPrefix()
    return "\n".join(lines[:cutoff] + ["#EXT-X-ENDLIST", ""]), count, total


def byte_dashy(source):
    """Prove a complete byte partition of the exact selected object.

The official YouTube builder may declare end==filesize only for the final
range. Normalize that one-past-end spelling to its exact valid EOF byte.
"""
    from yt_dlp.utils import update_url_query
    size = source.get("filesize")
    fragments = source.get("fragments")
    if (type(size) is not int or size <= 0 or not isinstance(fragments, list)
            or not fragments or source.get("extra_param_to_segment_url")):
        raise UnsupportedPrefix()
    base = source["url"]
    if any(key == "range" for key, _ in parse_qsl(urlsplit(base).query, keep_blank_values=True)):
        raise UnsupportedPrefix()
    result = []
    next_byte = 0
    clamped = False
    for index, fragment in enumerate(fragments):
        if not isinstance(fragment, dict) or set(fragment) - {"url"}:
            raise UnsupportedPrefix()
        url = fragment.get("url")
        if not isinstance(url, str):
            raise UnsupportedPrefix()
        ranges = [value for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True) if key == "range"]
        if len(ranges) != 1 or not re.fullmatch(r"\d+-\d+", ranges[0]):
            raise UnsupportedPrefix()
        start, end = map(int, ranges[0].split("-"))
        if url != update_url_query(base, {"range": ranges[0]}):
            raise UnsupportedPrefix()
        if start != next_byte or end < start or end - start + 1 > 10 * 1024 * 1024 + 1:
            raise UnsupportedPrefix()
        if end == size and index == len(fragments) - 1:
            end -= 1
            clamped = True
        if end < start or end >= size:
            raise UnsupportedPrefix()
        result.append({"url": update_url_query(base, {"range": f"{start}-{end}"})})
        next_byte = end + 1
    if next_byte != size:
        raise UnsupportedPrefix()
    return result, clamped


def prepare(info, ydl, window):
    """Prepare only internally; serialized fresh metadata cannot inject a plan."""
    row = info["formats"][0]
    protocol = row["protocol"]
    if protocol == "m3u8_native":
        from yt_dlp.downloader.hls import HlsFD
        fd = HlsFD(ydl, ydl.params)
        response = ydl.urlopen(fd._prepare_url(row, row["url"]))
        try:
            text = response.read().decode("utf-8", "strict")
            final_url = response.url
        finally:
            response.close()
        playlist, selected, total = hls_prefix(text, window)
        row["url"] = final_url
        row["hls_media_playlist_data"] = playlist
        return {"mode": "native_hls_prefix", "fragments_selected": selected,
                "fragments_total": total, "source_bytes": 0, "tail_clamped": False}
    if protocol == "http_dash_segments":
        fragments, clamped = byte_dashy(row)
        row["fragments"] = fragments
        return {"mode": "native_dash_byte_pipe", "fragments_selected": len(fragments),
                "fragments_total": len(fragments), "source_bytes": row["filesize"], "tail_clamped": clamped}
    return {"mode": "native_pipe_faststart", "fragments_selected": 0,
            "fragments_total": 0, "source_bytes": 0, "tail_clamped": False}
