"""Private, short-lived browser hints from the current info result only.

No destination is resolved or requested here. The owner-bound backend must
admit the destination before projection; the browser must use an anonymous
CORS fetch with redirect:error. A hint is never proof of a usable download.
"""
from datetime import datetime, timezone
import ipaddress
import math
import re
import time
from urllib.parse import parse_qsl, urlsplit

from .safety import ServiceError, hostname

_BROWSER_DEFAULT_HEADERS = {"user-agent", "accept", "accept-language"}
_CREDENTIAL_FIELDS = {"cookies", "cookiefile", "cookiesfrombrowser", "cookies_from_browser"}
_EXPIRY_KEYS = {"expire", "expires", "Expires"}
_MAX_URL = 8192
_TTL = 60


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _codec(value):
    return (isinstance(value, str) and value.lower() not in {"none", "unknown", "n/a", "null", "undefined"}
            and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", value) is not None)


def _anonymous_defaults(info, source):
    for item in (info, source):
        if any(item.get(name) is not None for name in _CREDENTIAL_FIELDS):
            return False
        headers = item.get("http_headers")
        if headers is None:
            continue
        if not isinstance(headers, dict):
            return False
        for name, value in headers.items():
            if (not isinstance(name, str) or not isinstance(value, str)
                    or len(value) > 2048 or re.search(r"[\x00-\x1f\x7f]", value)):
                return False
            if name.lower() in _BROWSER_DEFAULT_HEADERS:
                continue
            # yt-dlp's standard headers include this browser-managed hint.
            # It is not forwarded, and the real CORS fetch can still fail.
            if name.lower() == "sec-fetch-mode" and value == "navigate":
                continue
            return False
    return True


def _url_and_expiry(value, now):
    if (not isinstance(value, str) or not value or len(value) > _MAX_URL
            or re.search(r"[\x00-\x20\\\x7f]", value) or "#" in value):
        return None
    try:
        value.encode("utf-8", errors="strict")
        parsed = urlsplit(value)
        host = hostname(parsed.hostname or "")
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None):
            return None
        if parsed.path.lower().endswith((".m3u8", ".m3u", ".mpd", ".ism", ".isml")):
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return None
        # Reject legacy numeric IPv4 spellings too. Final DNS/public-address
        # validation remains the backend's mandatory projection gate.
        if all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", part) for part in host.split(".")):
            return None
        expires = now + _TTL
        for key, raw in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=128):
            if key.lower() in {"x-amz-signedheaders", "x-goog-signedheaders"} and raw.lower() != "host":
                return None
            if key not in _EXPIRY_KEYS:
                continue
            if not re.fullmatch(r"[0-9]{1,12}", raw):
                return None
            source_expiry = int(raw)
            if source_expiry > 253402300799:
                return None
            expires = min(expires, source_expiry)
        if expires <= now:
            return None
        return value, datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, UnicodeError, ServiceError):
        return None


def candidate(info, option, *, now=None):
    """Return exactly the agreed three fields, or preserve the Worker fallback."""
    if (not isinstance(info, dict) or not isinstance(option, dict)
            or info.get("_type") not in (None, "video") or "entries" in info
            or any(value is not None and value is not False for value in (info.get("is_live"), info.get("has_drm")))
            or info.get("live_status") not in (None, "not_live", "was_live", "post_live")
            or not _number(info.get("duration"))):
        return None
    components = option.get("components")
    video = option.get("component_video")
    public = option.get("public")
    if (option.get("audio") is not False or not isinstance(components, list) or len(components) != 1
            or not isinstance(components[0], str) or not isinstance(video, list) or len(video) != 1
            or video[0] is not True or not isinstance(public, dict) or public.get("ext") not in ("mp4", "webm")):
        return None
    formats = info.get("formats")
    if not isinstance(formats, list):
        return None
    matching = [row for row in formats if isinstance(row, dict) and row.get("format_id") == components[0]]
    if len(matching) != 1:
        return None
    source = matching[0]
    if (source.get("ext") != public["ext"] or source.get("protocol") not in ("http", "https")
            or any(value is not None and value is not False for value in (source.get("has_drm"), source.get("is_live")))
            or source.get("live_status") not in (None, "not_live", "was_live", "post_live")
            or not _codec(source.get("vcodec")) or not _codec(source.get("acodec"))
            or not _number(source.get("width")) or not _number(source.get("height"))
            or any(source.get(name) is not None for name in ("fragments", "fragment_base_url"))
            or source.get("manifest_url") not in (None, "") or not _anonymous_defaults(info, source)):
        return None
    moment = time.time() if now is None else now
    if not _number(moment) or moment > 253402300739:
        return None
    direct = _url_and_expiry(source.get("url"), int(moment))
    if direct is None:
        return None
    url, expires = direct
    return {"url": url, "expiresAt": expires, "ext": public["ext"]}


def attach_candidates(info, options, *, now=None):
    """Only mutate private options, never their public presentation objects."""
    moment = time.time() if now is None else now
    for option in options.values():
        if not isinstance(option, dict):
            continue
        option.pop("browserDirect", None)
        direct = candidate(info, option, now=moment)
        if direct is not None:
            option["browserDirect"] = direct
