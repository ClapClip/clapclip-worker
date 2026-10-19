"""MediaWorker — a generic AI processing node for the clapclip cluster.

It pulls jobs from the Next.js worker API (claim → process → complete), runs
them on the local GPU via a pluggable engine registry keyed by `task_type`,
and never touches the database or AWS directly (only outbound HTTPS + a token).
"""

__version__ = "0.1.0"
