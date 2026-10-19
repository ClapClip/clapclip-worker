"""Shared download processing for local jobs and the private outbound node."""
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from .safety import ServiceError, hostname
from .policy import DOWNLOAD_POLICY_VERSION, DOWNLOAD_LIMITS

PROTOCOLS = {"http", "https", "m3u8_native", "http_dash_segments"}
FORMAT_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


def positive_env(name, default, maximum):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value < 1 or value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


@dataclass
class Config:
    max_duration: int = DOWNLOAD_LIMITS["maxDuration"]
    max_width: int = DOWNLOAD_LIMITS["maxWidth"]
    max_height: int = DOWNLOAD_LIMITS["maxHeight"]
    job_timeout: int = 300
    info_timeout: int = 40
    extra_hosts: tuple = ()
    public_dns: bool = False
    python: str = sys.executable
    ffmpeg: str = field(default_factory=lambda: shutil.which("ffmpeg") or "ffmpeg")
    ffprobe: str = field(default_factory=lambda: shutil.which("ffprobe") or "ffprobe")
    node: str = field(default_factory=lambda: shutil.which("node") or "")

    @classmethod
    def from_env(cls):
        extra = tuple(hostname(item.strip()) for item in os.environ.get("DOWNLOAD_EXTRA_ALLOWED_HOSTS", "").split(",") if item.strip())
        return cls(
            job_timeout=positive_env("DOWNLOAD_JOB_TIMEOUT", 300, 1800),
            info_timeout=positive_env("DOWNLOAD_INFO_TIMEOUT", 40, 120),
            extra_hosts=extra,
            public_dns=os.environ.get("DOWNLOAD_PUBLIC_DNS") == "true",
            ffmpeg=os.environ.get("DOWNLOAD_FFMPEG", shutil.which("ffmpeg") or "ffmpeg"),
            ffprobe=os.environ.get("DOWNLOAD_FFPROBE", shutil.which("ffprobe") or "ffprobe"),
            node=os.environ.get("DOWNLOAD_NODE", shutil.which("node") or ""),
        )


def finite_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def format_options(info, max_width=1280, max_height=720):
    candidates = []
    for item in info.get("formats", []):
        if not isinstance(item, dict) or not FORMAT_ID.fullmatch(str(item.get("format_id", ""))):
            continue
        if item.get("has_drm") or item.get("protocol") not in PROTOCOLS:
            continue
        if item.get("ext") not in {"mp4", "webm", "m4a", "mp3", "opus", "ogg", "aac"}:
            continue
        # Missing codecs are not evidence of audio; never advertise fake MP3.
        audio = bool(item.get("acodec") and item["acodec"] != "none")
        video = bool(item.get("vcodec") and item["vcodec"] != "none")
        # Generic direct files may omit codecs entirely. Offer their container,
        # verify its video stream locally before release, and never infer MP3.
        unknown = (info.get("extractor") == "generic" and item.get("vcodec") is None
                   and item.get("acodec") is None and item.get("video_ext") in {"mp4", "webm"}
                   and item.get("protocol") in {"http", "https"})
        candidates.append({**item, "_audio": audio, "_video": video or unknown, "_unknown": unknown})
    audios = sorted([item for item in candidates if item["_audio"]], key=lambda item: (not item["_video"], finite_number(item.get("abr")) or 0), reverse=True)
    options, seen = {}, set()
    def video_order(row):
        height = finite_number(row.get("height")) or 0
        width = finite_number(row.get("width")) or 0
        if min(width, height) <= 0:
            return (-height, False, 0)
        scale = min(1, max_width / width, max_height / height)
        # Prefer an already conforming source at the same output resolution,
        # then the smallest larger source instead of needlessly fetching 4K.
        return (-int(height * scale), scale < 1, width * height)

    for item in sorted(candidates, key=video_order):
        if not item["_video"] or item.get("ext") not in {"mp4", "webm"}:
            continue
        height = finite_number(item.get("height"))
        width = finite_number(item.get("width"))
        if height is None or width is None or height <= 0 or width <= 0:
            continue
        if min(width, height, max_width, max_height) < 2:
            continue  # 4:2:0 video needs even dimensions; audio remains valid.
        scale = min(1, max_width / width, max_height / height)
        width, height = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
        quality = f"{int(height)}p" if height else "Original"
        ext = item["ext"]
        selected = [item]
        silent = False
        if not item["_audio"] and not item["_unknown"]:
            compatible = next((audio for audio in audios if not audio["_video"] and ((ext == "mp4" and audio["ext"] == "m4a") or (ext == "webm" and audio["ext"] in {"webm", "opus", "ogg"}))), None)
            if compatible is not None:
                selected.append(compatible)
            else:
                silent = True
        if (ext, quality) in seen:
            continue
        seen.add((ext, quality))
        fid = "f_" + uuid.uuid4().hex
        public = {"id": fid, "label": f"{quality} · {ext.upper()}" + (" · video only" if silent else ""), "ext": ext, "quality": quality,
                  "width": int(width), "height": int(height)}
        options[fid] = {
            "public": public,
            "components": [part["format_id"] for part in selected],
            # Audio has no video dimensions. Larger video sources are scaled
            # locally; never filter a separate audio track by video dimensions.
            "component_video": [part["_video"] for part in selected],
            "audio": False,
        }
        if len(options) >= 8:
            break
    if audios:
        audio = audios[0]
        fid = "f_" + uuid.uuid4().hex
        options[fid] = {
            "public": {"id": fid, "label": "Audio · MP3", "ext": "mp3", "quality": "192 kbps"},
            "components": [audio["format_id"]],
            "component_video": [False],
            "audio": True,
        }
    return options


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        # taskkill may be unavailable or denied; the Popen handle can still
        # terminate our own child. Never silently wait for a timed-out job.
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def _extractor_failure_code(stderr):
    """Return a reviewed category; never expose extractor output in logs or APIs."""
    text = stderr.casefold()
    if re.search(r"destination unavailable|proxy authentication|required proxy|proxy error.{0,80}403", text):
        return "EGRESS_REJECTED"
    if re.search(r"\b429\b|too many requests|rate.?limit", text):
        return "HTTP_RATE_LIMITED"
    if re.search(r"\b(500|502|503|504)\b|timed? out|temporary failure|connection reset|connection refused|remote end closed", text):
        return "HTTP_TRANSIENT"
    if re.search(r"\b403\b", text):
        return "HTTP_FORBIDDEN"
    if re.search(r"\bprivate\b|members.only|login required|sign.?in required|not authorized", text):
        return "SOURCE_RESTRICTED"
    if re.search(r"requested format.{0,80}(not available|unavailable)|format.{0,40}(not available|unavailable)", text):
        return "FORMAT_UNAVAILABLE"
    if re.search(r"unsupported url|no suitable extractor", text):
        return "UNSUPPORTED_MEDIA"
    return "EXTRACTOR_FAILED"


class ProcessRunner:
    def __init__(self, config, proxy):
        self.config, self.proxy = config, proxy
        self.output_details = None

    def base_args(self):
        c = self.config
        args = [c.python, str(Path(__file__).with_name("yt_runner.py")),
                "--ignore-config", "--no-plugin-dirs", "--no-remote-components",
                "--no-playlist", "--playlist-end", "1", "--no-cache-dir", "--no-cookies",
                "--no-cookies-from-browser", "--no-write-comments",
                "--no-write-subs", "--no-write-auto-subs", "--no-write-thumbnail",
                "--no-check-formats", "--socket-timeout", "15", "--retries", "1",
                "--fragment-retries", "1", "--extractor-retries", "1",
                "--proxy", self.proxy.url, "--geo-verification-proxy", self.proxy.url,
                "--downloader", "native", "--fixup", "never", "--no-warnings",
                "--no-js-runtimes", "--js-runtimes", "node:" + c.node,
                "--concurrent-fragments", "1", "--limit-rate", "8M",
                "--match-filter", "!is_live"]
        return args

    def run(self, args, directory, cancel, timeout, on_line=None, output_limit=8 * 1024 * 1024, failure_phase="process"):
        # Do not leak service/API secrets, cloud credentials or proxy bypass vars.
        keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        env = {key: value for key, value in os.environ.items() if key.upper() in keep}
        env.update({"HOME": str(directory), "USERPROFILE": str(directory), "TMPDIR": str(directory),
                    "TMP": str(directory), "TEMP": str(directory), "PYTHONUTF8": "1",
                    "CLAPCLIP_DOWNLOAD_POLICY_VERSION": str(DOWNLOAD_POLICY_VERSION),
                    "CLAPCLIP_PROXY_PORT": str(self.proxy.port), "CLAPCLIP_NODE": self.config.node,
                    "HTTP_PROXY": self.proxy.url, "HTTPS_PROXY": self.proxy.url, "ALL_PROXY": self.proxy.url, "NO_PROXY": ""})
        try:
            process = subprocess.Popen(args, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                       start_new_session=os.name != "nt",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError:
            raise ServiceError("SERVICE_UNAVAILABLE", "The download engine is unavailable.", 503) from None
        chunks, stderr_tail, total, overflow = [], bytearray(), [0], threading.Event()

        def reader(stream, save):
            buffered = b""
            while True:
                chunk = stream.read1(8192)
                if not chunk:
                    break
                total[0] += len(chunk)
                if total[0] > output_limit:
                    overflow.set()
                    break
                if save:
                    chunks.append(chunk)
                    if on_line:
                        buffered += chunk
                        while b"\n" in buffered:
                            line, buffered = buffered.split(b"\n", 1)
                            on_line(line.decode("utf-8", "replace"))
                else:
                    # Keep only a small private tail for classifying failures.
                    # Raw stderr can contain source URLs and is never logged.
                    stderr_tail.extend(chunk)
                    if len(stderr_tail) > 32 * 1024:
                        del stderr_tail[:-32 * 1024]
            stream.close()

        readers = [threading.Thread(target=reader, args=(process.stdout, True), daemon=True), threading.Thread(target=reader, args=(process.stderr, False), daemon=True)]
        for thread in readers:
            thread.start()
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel.is_set():
                    raise ServiceError("CANCELLED", "The download was cancelled.", 409)
                if time.monotonic() > deadline:
                    raise ServiceError("TIMEOUT", "This video took too long to process. Try a shorter video.", 422)
                if overflow.is_set():
                    raise ServiceError("MEDIA_UNAVAILABLE", "The source returned too much data.", 422)
                time.sleep(0.1)
            for thread in readers:
                thread.join(timeout=2)
            if overflow.is_set():
                raise ServiceError("MEDIA_UNAVAILABLE", "The source returned too much data.", 422)
            if process.returncode:
                error = ServiceError("MEDIA_UNAVAILABLE", "This video is unavailable, restricted, or not supported by the source right now.", 422)
                error.diagnostic_code = (
                    _extractor_failure_code(stderr_tail.decode("utf-8", "replace"))
                    if failure_phase in {"info", "download_source"}
                    else "LOCAL_PROCESS_FAILED"
                )
                error.phase = failure_phase
                raise error
            return b"".join(chunks).decode("utf-8", "replace")
        finally:
            stop_process(process)
            for thread in readers:
                thread.join(timeout=2)

    def inspect(self, url, directory, cancel):
        raw = self.run(self.base_args() + ["--skip-download", "--dump-single-json", "--", url], directory, cancel, self.config.info_timeout, failure_phase="info")
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise ServiceError("MEDIA_UNAVAILABLE", "The source did not return a supported video.", 422) from None

    def download(self, job, media, option, directory, update):
        info = self.inspect(media["url"], directory, job["cancel"])
        if not isinstance(info, dict):
            raise ServiceError("MEDIA_UNAVAILABLE", "The source did not return supported media metadata.", 422)
        duration = finite_number(info.get("duration"))
        if duration is None or duration <= 0:
            raise ServiceError("DURATION_LIMIT", "The source duration could not be determined.", 422)
        if info.get("_type") in {"playlist", "multi_video"} or info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise ServiceError("MEDIA_UNAVAILABLE", "Choose one public recorded video.", 422)
        safe_options = format_options(info, self.config.max_width, self.config.max_height)
        selected = next((candidate for candidate in safe_options.values()
                         if candidate.get("components") == option.get("components") and
                         candidate.get("audio") is option.get("audio")), None)
        if selected is None:
            raise ServiceError("NO_FORMATS", "The selected format no longer meets download limits.", 422)
        option = selected
        self.output_details = None
        self.proxy.allow_media()
        deadline = time.monotonic() + self.config.job_timeout
        files = []
        component_video = option.get("component_video", [not option["audio"]] * len(option["components"]))
        for index, (component, is_video) in enumerate(zip(option["components"], component_video)):
            def progress(line):
                if line.startswith("progress:"):
                    try:
                        current = float(line.split(":", 1)[1].strip().strip("%"))
                        update("downloading", min(88, int((index + current / 100) / len(option["components"]) * 88)))
                    except ValueError:
                        pass

            selector = "/".join(f"{component}[protocol={protocol}]" for protocol in sorted(PROTOCOLS))
            target = directory / f"source-{index}.media"
            self.run(self.base_args() + ["--no-simulate", "--no-part", "--no-continue", "--no-overwrites",
                     "--format", selector, "--output", str(target), "--newline", "--progress", "--progress-delta", "0.5",
                     "--progress-template", "download:progress:%(progress._percent_str)s", "--", media["url"]],
                     directory, job["cancel"], max(1, deadline - time.monotonic()), progress, failure_phase="download_source")
            if not target.is_file() or target.stat().st_size == 0:
                raise ServiceError("MEDIA_UNAVAILABLE", "The source did not provide a downloadable file.", 422)
            files.append(target)
        update("processing", 90)
        # Protocol + demuxer allowlists prohibit HTTP, HLS, concat and file-playlist indirection.
        input_options = ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,mpegts,mp3,ogg,aac"]
        probe = self.run([self.config.ffprobe, "-v", "error", *input_options, "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(files[0])], directory, job["cancel"], min(15, max(1, deadline - time.monotonic())))
        try:
            details = json.loads(probe)
            duration = float(details.get("format", {}).get("duration", 0))
        except (ValueError, TypeError):
            duration = 0
        if not math.isfinite(duration) or duration <= 0:
            raise ServiceError("DURATION_LIMIT", "The output duration is invalid.", 422)
        if option["audio"] and not any(stream.get("codec_type") == "audio" for stream in details.get("streams", [])):
            raise ServiceError("NO_FORMATS", "This video has no downloadable audio track.", 422)
        if not option["audio"] and not any(stream.get("codec_type") == "video" for stream in details.get("streams", [])):
            raise ServiceError("NO_FORMATS", "This source has no downloadable video track.", 422)
        target = directory / ("result." + option["public"]["ext"])
        command = [self.config.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        for path in files:
            command += input_options + ["-i", str(path)]
        command += ["-map_metadata", "-1", "-map_chapters", "-1", "-threads", "2"]
        if option["audio"]:
            command += ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
        else:
            command += ["-map", "0:v:0", "-map", "1:a:0" if len(files) == 2 else "0:a:0?"]
            # ffmpeg autorotates decoded frames. Scale their displayed shape,
            # then clear SAR/rotation so post-validation and players agree.
            scale = ("scale=w='max(2,trunc(iw*sar/2)*2)':h=ih,setsar=1,"
                     f"scale=w='min(iw,{self.config.max_width})':h='min(ih,{self.config.max_height})':"
                     "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1")
            command += ["-vf", scale, "-pix_fmt", "yuv420p", "-metadata:s:v:0", "rotate=0"]
            if option["public"]["ext"] == "mp4":
                command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac"]
            else:
                command += ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "6",
                            "-crf", "30", "-b:v", "0", "-c:a", "libopus"]
        if option["public"]["ext"] == "mp4":
            command += ["-movflags", "+faststart"]
        # Reserve 0.1s for encoder/container rounding, then enforce the exact
        # output bound using ffprobe; never silently tolerate an overlong file.
        command += ["-ss", "0", "-t", str(self.config.max_duration - 0.1)]
        command += [str(target)]
        self.run(command, directory, job["cancel"], max(1, deadline - time.monotonic()))
        if not target.is_file() or target.stat().st_size <= 0:
            raise ServiceError("MEDIA_UNAVAILABLE", "The source did not produce a playable file.", 422)
        output_probe = self.run([self.config.ffprobe, "-v", "error", *input_options,
                                 "-show_entries", "format=duration:stream=codec_type,width,height,duration,sample_aspect_ratio:stream_tags=rotate:stream_side_data=rotation",
                                 "-of", "json", str(target)], directory, job["cancel"],
                                min(15, max(1, deadline - time.monotonic())))
        try:
            final = json.loads(output_probe)
            final_duration = float(final.get("format", {}).get("duration", 0))
        except (ValueError, TypeError):
            final = {}
            final_duration = 0
        if not math.isfinite(final_duration) or final_duration <= 0 or final_duration > self.config.max_duration:
            raise ServiceError("DURATION_LIMIT", "The output duration is invalid.", 422)
        if option["audio"]:
            valid_stream = any(s.get("codec_type") == "audio" for s in final.get("streams", []))
        else:
            valid_stream = any(s.get("codec_type") == "video" and
                               isinstance(s.get("width"), int) and isinstance(s.get("height"), int) and
                               0 < s["width"] <= self.config.max_width and
                               0 < s["height"] <= self.config.max_height for s in final.get("streams", []))
        if not valid_stream:
            raise ServiceError("MEDIA_UNAVAILABLE", "The output video dimensions are invalid.", 422)
        for stream in final.get("streams", []):
            try:
                stream_duration = float(stream.get("duration", "nan"))
            except (ValueError, TypeError):
                stream_duration = float("nan")
            if math.isfinite(stream_duration) and stream_duration > self.config.max_duration:
                raise ServiceError("DURATION_LIMIT", "The output exceeds the duration limit.", 422)
            if stream.get("codec_type") == "video":
                rotations = [stream.get("tags", {}).get("rotate", 0)] + [s.get("rotation", 0) for s in stream.get("side_data_list", [])]
                if any(float(value) % 360 for value in rotations) or stream.get("sample_aspect_ratio") not in {None, "1:1", "N/A"}:
                    raise ServiceError("MEDIA_UNAVAILABLE", "The output display shape is invalid.", 422)
        video = next((s for s in final.get("streams", []) if s.get("codec_type") == "video"), {})
        self.output_details = (video.get("width", 0), video.get("height", 0), final_duration)
        for path in files:
            path.unlink(missing_ok=True)
        return target
