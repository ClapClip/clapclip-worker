"""Conservative admission for local MP4 remuxing; never authorizes network I/O."""
import math
from fractions import Fraction


PROBE_ENTRIES = (
    "format=duration,start_time:stream=codec_type,codec_name,profile,level,pix_fmt,width,height,"
    "duration,duration_ts,start_time,start_pts,time_base,sample_aspect_ratio:stream_tags=rotate:"
    "stream_side_data=rotation"
)


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def probe_shape(details):
    return (isinstance(details, dict) and isinstance(details.get("format"), dict)
            and isinstance(details.get("streams"), list)
            and all(isinstance(stream, dict) for stream in details["streams"]))


def compatible(details, max_width, max_height, *, video, audio, max_duration=None):
    """Unknown timestamps/codecs/display metadata require the normal encoder."""
    if not probe_shape(details):
        return False
    streams = details["streams"]
    if (not all(isinstance(s, dict) for s in streams)
            or len(streams) != int(video) + int(audio)
            or sum(s.get("codec_type") == "video" for s in streams) != int(video)
            or sum(s.get("codec_type") == "audio" for s in streams) != int(audio)):
        return False
    for item in [details.get("format", {}), *streams]:
        duration, start = number(item.get("duration")), number(item.get("start_time"))
        if duration is None or duration <= 0 or start != 0:
            return False
        if max_duration is not None and duration > max_duration:
            return False
    for stream in streams:
        try:
            numerator, denominator = (int(part) for part in stream.get("time_base", "").split("/"))
            if numerator <= 0 or denominator <= 0:
                return False
        except (AttributeError, ValueError):
            return False
        ticks = stream.get("duration_ts")
        if (type(stream.get("start_pts")) is not int or stream["start_pts"] != 0
                or type(ticks) is not int or ticks <= 0
                or (max_duration is not None and Fraction(ticks * numerator, denominator) > max_duration)):
            return False
        if stream["codec_type"] == "audio":
            if stream.get("codec_name") != "aac" or stream.get("profile") != "LC":
                return False
            continue
        width, height = stream.get("width"), stream.get("height")
        level = stream.get("level")
        if (stream.get("codec_name") != "h264" or stream.get("pix_fmt") != "yuv420p"
                or stream.get("profile") not in {"Constrained Baseline", "Baseline", "Main", "High"}
                or type(level) is not int or not 10 <= level <= 41
                or type(width) is not int or type(height) is not int
                or not 2 <= width <= max_width or not 2 <= height <= max_height
                or width % 2 or height % 2 or stream.get("sample_aspect_ratio") != "1:1"):
            return False
        tags, sides = stream.get("tags", {}), stream.get("side_data_list", [])
        if not isinstance(tags, dict) or not isinstance(sides, list) or not all(isinstance(s, dict) for s in sides):
            return False
        rotations = [tags.get("rotate", 0)]
        rotations += [side.get("rotation", 0) for side in sides]
        if any(number(rotation) is None or number(rotation) % 360 for rotation in rotations):
            return False
    return True
