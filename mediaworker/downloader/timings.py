"""Fixed download-only phases; no task identity or untrusted text."""
from contextlib import suppress
import logging
import math
import time

log = logging.getLogger(__name__)
STAGES = {"info", "source", "source_probe", "copy", "encode", "decode", "output_probe",
          "engine", "preupload", "upload", "complete"}
PROTOCOLS = {"http", "https", "m3u8_native", "http_dash_segments", "none"}


def record(stage, started, *, state="end", input_bytes=0, output_bytes=0, protocol="none", input_path=None, output_path=None):
    # Logging must never turn an accepted ACK into a reported business failure.
    with suppress(Exception):
        if input_path is not None:input_bytes=input_path.stat().st_size
        if output_path is not None:output_bytes=output_path.stat().st_size
        if (stage not in STAGES or state not in {"begin", "end"} or protocol not in PROTOCOLS
                or type(input_bytes) is not int or type(output_bytes) is not int
                or min(input_bytes, output_bytes) < 0):
            return
        elapsed = time.monotonic() - started
        if not math.isfinite(elapsed) or elapsed < 0:
            return
        log.info("download stage=%s state=%s elapsed_seconds=%.3f input_bytes=%d output_bytes=%d protocol=%s",
                 stage, state, elapsed, input_bytes, output_bytes, protocol)
