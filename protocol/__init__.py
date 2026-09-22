"""The queue protocol shared by both halves of Halo-Cable-Label-Printer.

The Docker service and the Windows agent are separate programs on separate
operating systems that never talk directly. Everything they agree on lives
here, so neither half can drift from the other by hand-rolling its own copy
of the naming rules. See spec section 6.
"""
from .queue import (  # noqa: F401
    PAYLOAD_EXTENSIONS,
    PROTOCOL_VERSION,
    SUBDIRS,
    QueueError,
    ensure_queue,
    format_stem,
    iter_inbox_jobs,
    move_job,
    parse_stem,
    read_json,
    sha256_bytes,
    sha256_file,
    stem_of,
    sweep_stale_tmp,
    utc_now_iso,
    validate_job,
    validate_result,
    write_job,
    write_result,
)
