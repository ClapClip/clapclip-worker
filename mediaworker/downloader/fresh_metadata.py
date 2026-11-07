"""Small final-video bridge for this attempt's freshly inspected component."""
import json
import math
import re
from urllib.parse import urljoin, urlsplit

MAX_FRESH_BYTES = 8 * 1024 * 1024
PROTOCOLS = {"http", "https", "m3u8_native", "http_dash_segments"}
EXTENSIONS = {"mp4", "webm", "m4a", "mp3", "opus", "ogg", "aac"}
FORMAT_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
HEADERS = {"user-agent", "accept", "accept-language", "referer", "origin"}
NUMBERS = {"width", "height", "fps", "asr", "abr", "tbr", "audio_channels", "filesize", "filesize_approx"}


def remote_url(value):
    if not isinstance(value, str) or not value or len(value) > MAX_FRESH_BYTES:
        raise ValueError("INVALID_FRESH_METADATA")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.fragment or any(c in value for c in "\r\n\x00")):
        raise ValueError("INVALID_FRESH_METADATA")
    # DNS/public-IP validation still belongs to the authenticated egress proxy.
    return value


def build_component(info, component):
    """Rebuild, rather than copy, an untrusted extractor result and its state."""
    if (not isinstance(info, dict) or info.get("_type", "video") != "video"
            or "entries" in info or info.get("is_live")
            or info.get("live_status") in {"is_live", "is_upcoming"}
            or not isinstance(component, str) or not FORMAT_ID.fullmatch(component)):
        raise ValueError("INVALID_FRESH_METADATA")
    formats = info.get("formats")
    if not isinstance(formats, list):
        raise ValueError("INVALID_FRESH_METADATA")
    matches = [row for row in formats if isinstance(row, dict) and row.get("format_id") == component]
    if len(matches) != 1:
        raise ValueError("INVALID_FRESH_METADATA")
    source = matches[0]
    if source.get("protocol") not in PROTOCOLS or source.get("ext") not in EXTENSIONS or source.get("has_drm"):
        raise ValueError("INVALID_FRESH_METADATA")
    # Custom AES/downloader overrides are not part of this bridge. Native HLS
    # still obtains ordinary manifest/key data through the original proxy.
    if source.get("hls_aes") or info.get("hls_aes"):
        raise ValueError("UNSUPPORTED_FRESH_FORMAT")
    row = {"format_id": component, "url": remote_url(source.get("url")),
           "protocol": source["protocol"], "ext": source["ext"]}
    for name in ("vcodec", "acodec"):
        value = source.get(name)
        if isinstance(value, str) and len(value) <= 200:
            row[name] = value
    for name in NUMBERS:
        value = source.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            row[name] = value
    for name in ("manifest_url", "fragment_base_url"):
        if source.get(name) is not None:
            row[name] = remote_url(source[name])
    headers = {}
    for original in (info.get("http_headers"), source.get("http_headers")):
        if original is None:
            continue
        if not isinstance(original, dict):
            raise ValueError("INVALID_FRESH_METADATA")
        for key, value in original.items():
            if (isinstance(key, str) and key.casefold() in HEADERS and isinstance(value, str)
                    and len(value) <= 4096 and not any(c in value for c in "\r\n\x00")):
                headers[key] = value
    if headers:
        row["http_headers"] = headers
    for name in ("extra_param_to_segment_url", "extra_param_to_key_url"):
        value = source.get(name, info.get(name))
        if value is not None:
            if not isinstance(value, str) or len(value) > 8192 or any(c in value for c in "\r\n\x00"):
                raise ValueError("INVALID_FRESH_METADATA")
            row[name] = value
    options = source.get("downloader_options")
    if isinstance(options, dict):
        chunk = options.get("http_chunk_size")
        if isinstance(chunk, int) and not isinstance(chunk, bool) and 0 < chunk <= 10 * 1024 * 1024:
            row["downloader_options"] = {"http_chunk_size": chunk}
    if "fragments" in source:
        fragments = source["fragments"]
        if not isinstance(fragments, list) or len(fragments) > 100000:
            raise ValueError("INVALID_FRESH_METADATA")
        row["fragments"] = []
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise ValueError("INVALID_FRESH_METADATA")
            clean = {}
            if fragment.get("url") is not None:
                clean["url"] = remote_url(fragment["url"])
            elif fragment.get("path") is not None and row.get("fragment_base_url"):
                value = fragment["path"]
                if not isinstance(value, str) or urlsplit(value).scheme or value.startswith(("/", "\\")):
                    raise ValueError("INVALID_FRESH_METADATA")
                # Validate the assembled URL too; never admit local indirection.
                remote_url(urljoin(row["fragment_base_url"], value))
                clean["path"] = value
            else:
                raise ValueError("INVALID_FRESH_METADATA")
            duration = fragment.get("duration")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration >= 0:
                clean["duration"] = duration
            if fragment.get("range") is not None:
                value = fragment["range"]
                if not isinstance(value, str) or not re.fullmatch(r"\d+-\d+", value):
                    raise ValueError("INVALID_FRESH_METADATA")
                clean["range"] = value
            row["fragments"].append(clean)
    if row["protocol"] == "http_dash_segments" and not row.get("fragments"):
        raise ValueError("INVALID_FRESH_METADATA")
    result = {"_type": "video", "id": "clapclip-download", "title": "media",
              "is_live": False, "formats": [row]}
    duration = info.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration > 0:
        result["duration"] = duration
    for name in ("extractor", "extractor_key"):
        if isinstance(info.get(name), str) and len(info[name]) <= 128:
            result[name] = info[name]
    return result


def encode_component(info, component):
    payload = json.dumps(build_component(info, component), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_FRESH_BYTES:
        raise ValueError("FRESH_METADATA_LIMIT")
    return payload


def read_component(path, component):
    with open(path, "rb") as stream:
        payload = stream.read(MAX_FRESH_BYTES + 1)
    if len(payload) > MAX_FRESH_BYTES:
        raise ValueError("FRESH_METADATA_LIMIT")
    return build_component(json.loads(payload), component)
