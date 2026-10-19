"""One current download policy. The wire marker is not a product version."""
from dataclasses import replace

from .safety import ServiceError

DOWNLOAD_POLICY_VERSION = 2
DOWNLOAD_POLICY_VERSIONS = (DOWNLOAD_POLICY_VERSION,)
DOWNLOAD_LIMITS = {
    "version": DOWNLOAD_POLICY_VERSION, "maxHeight": 720, "maxWidth": 1280,
    "maxDuration": 300, "maxBytes": None,
    "requireDuration": True, "requireKnownHeight": True,
}
METADATA_BYTES = 512 * 1024
EXTRACTOR_OUTPUT_BYTES = 8 * 1024 * 1024


def validate_contract(contract):
    if (not isinstance(contract, dict) or set(contract) != set(DOWNLOAD_LIMITS)
            or type(contract.get("version")) is not int
            or contract["version"] != DOWNLOAD_POLICY_VERSION
            or contract.get("maxBytes") is not None
            or contract.get("requireDuration") is not True
            or contract.get("requireKnownHeight") is not True):
        raise ServiceError("INVALID_LIMITS", "Analyze the video again using the current download policy.", 422)
    for field in ("maxHeight", "maxWidth", "maxDuration"):
        # All ceilings are below JavaScript's safe integer maximum. bool and
        # float (including whole-valued floats) are not accepted JSON integers.
        if type(contract[field]) is not int or not 1 <= contract[field] <= DOWNLOAD_LIMITS[field]:
            raise ServiceError("INVALID_LIMITS", "Analyze the video again using the current download policy.", 422)
    return DOWNLOAD_POLICY_VERSION


def resolve_job_config(contract, local_config):
    validate_contract(contract)
    # An isolated current task may narrow bounds, never clamp an invalid task
    # or inherit removed media-size/duration policy from the local environment.
    return replace(local_config, max_height=contract["maxHeight"], max_width=contract["maxWidth"],
                   max_duration=contract["maxDuration"])
