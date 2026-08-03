"""
Classify related media files by their best supported capture date.

The script keeps original files unchanged, uses ExifTool for broad
metadata support, and organizes classified copies by ``YYYY-MM-DD``.
The module is ordered by responsibility: metadata I/O, timezone/DST
validation, correction writing, date decisions, copying, then the
high-level interactive workflow.
"""

import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from exiftool import ExifToolHelper
from exiftool.exceptions import ExifToolException, ExifToolExecuteError

# ============================================================================
# CONFIGURATION AND METADATA POLICY
# ============================================================================
# Constants in this section define what ExifTool reads, which timestamps are
# capture candidates, which fields are contextual evidence, and how classified
# copies may be corrected.

DATETYPE = {
    0: "OS_DATE",
    1: "METADATA",
    2: "METADATA_TIMEZONE_CORRECTED",
}

DEFAULT_TIMEZONE_NAME = "Europe/Berlin"

# ExifTool's Time:All group discovers timestamps across EXIF, XMP, IPTC,
# QuickTime, maker notes, composite tags, and other supported metadata families.
# The additional tags are timezone evidence only. They are requested by tag
# semantics rather than by camera make, so standard metadata from unknown and
# future cameras follows the same path as metadata from known brands.
EXIFTOOL_TAGS = [
    "Time:All",
    "File:FileType",
    "File:MIMEType",
    # Generic camera daylight-saving configuration used by several makers.
    "DaylightSavings#",
    # Pentax/Ricoh travel profiles. The active profile is selected below from
    # WorldTimeLocation; no Make/Model dispatch is required.
    "WorldTimeLocation#",
    "HometownDST#",
    "DestinationDST#",
    "HometownCity",
    "DestinationCity",
    # Standard and maker-note offset context.
    "OffsetTime",
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
    "TimeZoneOffset",
    "TimeZone",
    "TimeZoneCity",
    "TimeOffset",
    # UTC counterparts used by Olympus/OM System, GPS-enabled cameras, phones,
    # GoPro, DJI, and any other file in which ExifTool exposes these tags.
    "DateTimeUTC",
    "GPSDateTime",
]

# Remove timestamps that describe device operation, editing, or embedded
# resources rather than original image/recording creation. Capture-oriented
# local fields such as SonyDateTime, PanasonicDateTime, RicohDate, and ordinary
# EXIF/XMP creation dates remain candidates. UTC_REFERENCE_TAGS are routed to
# timezone evidence separately and therefore are not date choices.
EXCLUDED_TIME_TAGS = (
    "PowerUpTime",
    "TimeSincePowerOn",
    "RunTimeSincePowerUp",
    "ShotNumberSincePowerUp",
    "MetadataDate",
    "HistoryWhen",
    "ModifyDate",
    "SubSecModifyDate",
    "MediaModifyDate",
    "TrackModifyDate",
    "LastModifyDate",
    "DateTimeEnd",
    "EndTime",
    "ProfileDateTime",
    "LayerModifyDates",
    "ExtensionCreateDate",
    "ExtensionModifyDate",
)

EXIFTOOL_READ_PARAMS = [
    "-a",
    "-ee",
    "-x",
    "1System:All",
]
for excluded_time_tag in EXCLUDED_TIME_TAGS:
    EXIFTOOL_READ_PARAMS.extend(["-x", excluded_time_tag])

FILE_MODIFY_DATE_TAGS = ["FileModifyDate"]
FILE_MODIFY_DATE_PARAMS = []

# Metadata correction is driven by timestamp semantics rather than camera make.
# Existing local wall-clock values are shifted, aware values keep their clock
# reading and receive the corrected offset, and known UTC references remain
# unchanged. The write pass never creates missing date/time fields.
ASSOCIATED_OFFSET_TAGS = {
    "DateTimeOriginal": "OffsetTimeOriginal",
    "CreateDate": "OffsetTimeDigitized",
    "ModifyDate": "OffsetTime",
}
PARTIAL_DATE_TIME_PAIRS = (
    ("DateCreated", "TimeCreated"),
    ("DigitalCreationDate", "DigitalCreationTime"),
)
NONLOCAL_CORRECTION_TAGS = {
    "PowerUpTime",
    "TimeSincePowerOn",
    "RunTimeSincePowerUp",
    "ShotNumberSincePowerUp",
    "MetadataDate",
    "HistoryWhen",
    "ProfileDateTime",
    "LayerModifyDates",
    "ExtensionCreateDate",
    "ExtensionModifyDate",
}
CORRECTION_WRITE_PARAMS = [
    "-overwrite_original_in_place",
    "-P",
    "-wm",
    "w",
]

DIRECT_DST_TAGS = {"DaylightSavings"}
PROFILE_SELECTOR_TAG = "WorldTimeLocation"
PROFILE_DST_TAGS = {
    "Hometown": "HometownDST",
    "Destination": "DestinationDST",
}
PROFILE_CITY_TAGS = {
    "Hometown": "HometownCity",
    "Destination": "DestinationCity",
}
OFFSET_CONTEXT_TAGS = {
    "OffsetTime",
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
    "TimeZoneOffset",
    "TimeZone",
    "TimeOffset",
}
DISPLAY_CONTEXT_TAGS = {
    "TimeZoneCity",
    "HometownCity",
    "DestinationCity",
    PROFILE_SELECTOR_TAG,
}
TIME_CONTEXT_TAGS = (
    DIRECT_DST_TAGS
    | set(PROFILE_DST_TAGS.values())
    | OFFSET_CONTEXT_TAGS
    | DISPLAY_CONTEXT_TAGS
)
UTC_REFERENCE_TAGS = {
    "DateTimeUTC",
    "GPSDateTime",
    "GPSDateStamp",
    "GPSTimeStamp",
    "UTCDateTime",
}

CORRECTION_READ_TAGS = [
    "Time:All",
    *sorted(TIME_CONTEXT_TAGS),
    "File:FileCreateDate",
]
CORRECTION_READ_PARAMS = ["-a", "-ee"]

COMPLETE_DATE_TIME_PATTERN = re.compile(
    r"^(?P<year>\d{4})[:-](?P<month>\d{2})[:-](?P<day>\d{2})[ T]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<timezone>Z|[+-]\d{2}:?\d{2})?$"
)
SIGNED_OFFSET_PATTERN = re.compile(
    r"(?<!\d)(?:UTC|GMT)?\s*(?P<sign>[+-])"
    r"(?P<hour>\d{1,2})(?::?(?P<minute>\d{2}))?(?!\d)",
    re.IGNORECASE,
)
DATE_ONLY_PATTERN = re.compile(
    r"^(?P<year>\d{4})[:-](?P<month>\d{2})[:-](?P<day>\d{2})$"
)
TIME_ONLY_PATTERN = re.compile(
    r"^(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<timezone>Z|[+-]\d{2}:?\d{2})?$"
)

CLASSIFIED_FOLDER_NAME = "classified"
UNCLASSIFIED_FOLDER_NAME = "unclassified"
COPY_CHUNK_SIZE = 1024 * 1024

VIDEO_EXTENSIONS = {
    ".3gp",
    ".360",
    ".avi",
    ".insv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mts",
    ".mxf",
    ".webm",
}

IMAGE_EXTENSIONS = {
    ".arw",
    ".avif",
    ".bmp",
    ".cr2",
    ".cr3",
    ".dng",
    ".gif",
    ".heic",
    ".heif",
    ".jpe",
    ".jpeg",
    ".jpg",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".png",
    ".raf",
    ".rw2",
    ".sr2",
    ".srf",
    ".tif",
    ".tiff",
    ".webp",
}

# ============================================================================
# USER INPUT AND BASIC CONFIGURATION
# ============================================================================
# Small helpers that normalize interactive input. The detailed timezone rules
# live in their own section below.


def clean_input_path(raw_path):
    """Remove one matching pair of drag-and-drop quotes."""
    cleaned_path = raw_path.strip()
    if (
        len(cleaned_path) >= 2
        and cleaned_path[0] == cleaned_path[-1]
        and cleaned_path[0] in {'"', "'"}
    ):
        cleaned_path = cleaned_path[1:-1]
    return Path(cleaned_path).expanduser()


# ============================================================================
# EXIFTOOL METADATA READING AND VALUE PARSING
# ============================================================================
# These functions are the boundary between ExifTool data and the rest of the
# program. They preserve raw metadata semantics and never invent missing date
# or time components.


def iter_metadata_values(metadata):
    """Yield one item for every scalar or list value returned by ExifTool."""
    for metadata_key, metadata_value in metadata.items():
        if metadata_key == "SourceFile":
            continue

        key_parts = metadata_key.split(":")
        groups = key_parts[:-1]
        tag_name = key_parts[-1]
        values = (
            metadata_value if isinstance(metadata_value, list) else [metadata_value]
        )

        for value in values:
            if value is not None:
                yield metadata_key, groups, tag_name, value


def parse_timezone_offset_minutes(timezone_text):
    """Parse Z, +HH:MM, -HH:MM, +HHMM, or -HHMM into minutes."""
    if not timezone_text:
        return None
    if timezone_text == "Z":
        return 0

    sign = 1 if timezone_text[0] == "+" else -1
    timezone_hour = int(timezone_text[1:3])
    timezone_minute = int(timezone_text[-2:])
    if timezone_hour > 23 or timezone_minute > 59:
        raise ValueError(f"invalid timezone offset {timezone_text!r}")
    return sign * (timezone_hour * 60 + timezone_minute)


def parse_context_offset(tag_name, metadata_value):
    """
    Parse an independent timezone-offset field when its representation is safe.

    TimeZoneOffset is standardized as hours and may be numeric. Other maker-note
    fields are accepted only when their text explicitly carries a sign, avoiding
    assumptions about undocumented numeric units.
    """
    if tag_name == "TimeZoneOffset" and isinstance(
        metadata_value,
        (int, float),
    ):
        numeric_hours = float(metadata_value)
        if -24 <= numeric_hours <= 24:
            return int(round(numeric_hours * 60))
        return None

    value_text = str(metadata_value).strip()
    offset_match = SIGNED_OFFSET_PATTERN.search(value_text)
    if offset_match is None:
        return None

    hours = int(offset_match.group("hour"))
    minutes = int(offset_match.group("minute") or 0)
    if hours > 23 or minutes > 59:
        return None

    sign = 1 if offset_match.group("sign") == "+" else -1
    return sign * (hours * 60 + minutes)


def parse_complete_metadata_datetime(metadata_value):
    """
    Parse one complete date/time into canonical whole-second wall-clock data.

    Raw values remain unformatted until this boundary, so date-only and
    time-only fields cannot acquire invented components. An explicit UTC offset
    is returned separately as timezone evidence and is not applied to the local
    wall-clock value used for date-folder classification.
    """
    date_text = str(metadata_value).strip()
    date_match = COMPLETE_DATE_TIME_PATTERN.fullmatch(date_text)
    if date_match is None:
        return None

    timezone_offset_minutes = parse_timezone_offset_minutes(
        date_match.group("timezone")
    )
    local_datetime = datetime(
        int(date_match.group("year")),
        int(date_match.group("month")),
        int(date_match.group("day")),
        int(date_match.group("hour")),
        int(date_match.group("minute")),
        int(date_match.group("second")),
    )
    return local_datetime, timezone_offset_minutes


def parse_partial_datetime(date_value, time_value):
    """Combine matching date-only and time-only fields for safe correction."""
    date_match = DATE_ONLY_PATTERN.fullmatch(str(date_value).strip())
    time_match = TIME_ONLY_PATTERN.fullmatch(str(time_value).strip())
    if date_match is None or time_match is None:
        return None
    local_datetime = datetime(
        int(date_match.group("year")),
        int(date_match.group("month")),
        int(date_match.group("day")),
        int(time_match.group("hour")),
        int(time_match.group("minute")),
        int(time_match.group("second")),
    )
    offset_minutes = parse_timezone_offset_minutes(time_match.group("timezone"))
    return local_datetime, offset_minutes


def parse_daylight_savings_value(metadata_value):
    """Normalize a camera DST value to True, False, or None."""
    if isinstance(metadata_value, bool):
        return metadata_value
    if isinstance(metadata_value, (int, float)):
        return metadata_value != 0

    value_text = str(metadata_value).strip().casefold()
    if value_text in {"on", "yes", "true", "enabled", "daylight saving"}:
        return True
    if value_text in {"off", "no", "false", "disabled", "standard time"}:
        return False
    try:
        return float(value_text) != 0
    except ValueError:
        return None


def parse_world_time_location(metadata_value):
    """Return Pentax-style active profile name without checking camera make."""
    if isinstance(metadata_value, (int, float)):
        numeric_value = int(metadata_value)
        if numeric_value == 0:
            return "Hometown"
        if numeric_value == 1:
            return "Destination"
        return None

    value_text = str(metadata_value).strip().casefold()
    if value_text in {"0", "home", "hometown"}:
        return "Hometown"
    if value_text in {"1", "destination", "travel"}:
        return "Destination"
    return None


def get_metadata_write_target(groups, tag_name, raw=False):
    """Map a -G0:1:4 extraction path to an explicit writable group target."""
    if not groups or "System" in groups or groups[0] in {"Composite", "File"}:
        return None

    family_zero = groups[0]
    family_one = None
    for group_name in groups[1:]:
        if not group_name.casefold().startswith("copy"):
            family_one = group_name
            break

    target_group = family_one or family_zero
    raw_suffix = "#" if raw else ""
    return f"{target_group}:{tag_name}{raw_suffix}"


def format_complete_datetime(original_value, local_datetime, offset_minutes=None):
    """Preserve a timestamp's syntax while replacing its date/time components."""
    original_text = str(original_value).strip()
    date_match = COMPLETE_DATE_TIME_PATTERN.fullmatch(original_text)
    if date_match is None:
        raise ValueError(f"not a complete timestamp: {original_value!r}")

    date_separator = original_text[4]
    datetime_separator = original_text[10]
    fraction = date_match.group("fraction")
    timezone_text = date_match.group("timezone")
    if offset_minutes is not None:
        timezone_text = format_offset(offset_minutes)

    formatted = (
        f"{local_datetime.year:04d}{date_separator}"
        f"{local_datetime.month:02d}{date_separator}"
        f"{local_datetime.day:02d}{datetime_separator}"
        f"{local_datetime.hour:02d}:{local_datetime.minute:02d}:"
        f"{local_datetime.second:02d}"
    )
    if fraction is not None:
        formatted += f".{fraction}"
    if timezone_text is not None:
        formatted += timezone_text
    return formatted


def format_date_only(original_value, local_datetime):
    """Preserve the date separator of a date-only metadata value."""
    original_text = str(original_value).strip()
    date_match = DATE_ONLY_PATTERN.fullmatch(original_text)
    if date_match is None:
        raise ValueError(f"not a date-only value: {original_value!r}")
    separator = original_text[4]
    return (
        f"{local_datetime.year:04d}{separator}"
        f"{local_datetime.month:02d}{separator}"
        f"{local_datetime.day:02d}"
    )


def format_time_only(original_value, local_datetime, offset_minutes=None):
    """Preserve fractional precision while formatting a time-only value."""
    original_text = str(original_value).strip()
    time_match = TIME_ONLY_PATTERN.fullmatch(original_text)
    if time_match is None:
        raise ValueError(f"not a time-only value: {original_value!r}")
    formatted = (
        f"{local_datetime.hour:02d}:{local_datetime.minute:02d}:"
        f"{local_datetime.second:02d}"
    )
    fraction = time_match.group("fraction")
    if fraction is not None:
        formatted += f".{fraction}"
    timezone_text = time_match.group("timezone")
    if offset_minutes is not None:
        timezone_text = format_offset(offset_minutes)
    if timezone_text is not None:
        formatted += timezone_text
    return formatted


def read_correction_metadata(metadata_reader, filename):
    """Read all existing date/time and timezone fields used by correction."""
    try:
        metadata_results = metadata_reader.get_tags(
            files=filename,
            tags=CORRECTION_READ_TAGS,
            params=CORRECTION_READ_PARAMS,
        )
    except ExifToolException as error:
        raise OSError(
            f"ExifTool could not inspect correction fields in '{filename}': {error}"
        ) from error
    return metadata_results[0] if metadata_results else {}


def get_dates(metadata_reader, filename):
    """
    Return date candidates, review reason, image flag, context, and UTC records.

    The embedded pass never falls back to filesystem timestamps. UTC reference
    tags are removed from date choices and retained as timezone evidence.
    """
    extension_is_image = filename.suffix.casefold() in IMAGE_EXTENSIONS

    try:
        metadata_results = metadata_reader.get_tags(
            files=filename,
            tags=EXIFTOOL_TAGS,
            params=EXIFTOOL_READ_PARAMS,
        )
    except ExifToolExecuteError as error:
        if extension_is_image:
            error_message = str(error.stderr).strip() if error.stderr else str(error)
            return (
                [],
                f"ExifTool could not inspect the image ({error_message})",
                True,
                [],
                [],
                extension_is_image,
            )
        return [], None, False, [], [], False
    except ExifToolException as error:
        raise OSError(
            f"ExifTool could not read metadata from '{filename}': {error}"
        ) from error

    if not metadata_results:
        if extension_is_image:
            return (
                [],
                "ExifTool returned no metadata for the image",
                True,
                [],
                [],
                extension_is_image,
            )
        return [], None, False, [], [], False

    metadata = metadata_results[0]
    file_extension = filename.suffix.casefold()
    mime_type = None
    date_candidates = []
    context_records = []
    utc_records = []
    invalid_date_messages = []

    for metadata_key, groups, tag_name, metadata_value in iter_metadata_values(
        metadata
    ):
        if tag_name in UTC_REFERENCE_TAGS:
            try:
                parsed_utc = parse_complete_metadata_datetime(metadata_value)
            except ValueError:
                parsed_utc = None
            if parsed_utc is not None:
                utc_datetime, _ = parsed_utc
                utc_record = (metadata_key, utc_datetime)
                if utc_record not in utc_records:
                    utc_records.append(utc_record)
            continue

        if tag_name in TIME_CONTEXT_TAGS:
            context_record = (metadata_key, tag_name, metadata_value)
            if context_record not in context_records:
                context_records.append(context_record)
            continue

        if tag_name == "MIMEType":
            if mime_type is None and isinstance(metadata_value, str):
                mime_type = metadata_value.casefold()
            continue
        if tag_name == "FileType":
            continue
        if "System" in groups or tag_name in EXCLUDED_TIME_TAGS:
            continue

        try:
            parsed_metadata_date = parse_complete_metadata_datetime(metadata_value)
        except ValueError:
            invalid_date_messages.append(f"{metadata_key}={metadata_value!r}")
            continue

        if parsed_metadata_date is None:
            continue

        metadata_date, timezone_offset_minutes = parsed_metadata_date
        date_candidates.append(
            (
                1,
                metadata_date,
                metadata_key,
                timezone_offset_minutes,
            )
        )

    mime_is_image = mime_type is not None and mime_type.startswith("image/")
    mime_is_video = mime_type is not None and mime_type.startswith("video/")
    file_is_image = mime_is_image or file_extension in IMAGE_EXTENSIONS
    file_is_capture_media = (
        mime_is_image
        or mime_is_video
        or file_extension in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    )

    if invalid_date_messages and file_is_image:
        return (
            [],
            "invalid metadata date: " + "; ".join(invalid_date_messages),
            True,
            context_records,
            utc_records,
            file_is_capture_media,
        )

    return (
        date_candidates,
        None,
        file_is_image,
        context_records,
        utc_records,
        file_is_capture_media,
    )


def get_file_modify_date(metadata_reader, filename):
    """Return raw FileModifyDate as the separately requested fallback."""
    try:
        metadata_results = metadata_reader.get_tags(
            files=filename,
            tags=FILE_MODIFY_DATE_TAGS,
            params=FILE_MODIFY_DATE_PARAMS,
        )
    except ExifToolException as error:
        raise OSError(
            f"ExifTool could not read FileModifyDate from '{filename}': {error}"
        ) from error

    if not metadata_results:
        return None, "ExifTool returned no FileModifyDate"

    modification_date_text = next(
        (
            metadata_value
            for _, _, tag_name, metadata_value in iter_metadata_values(
                metadata_results[0]
            )
            if tag_name == "FileModifyDate"
        ),
        None,
    )
    if modification_date_text is None:
        return None, "ExifTool returned no FileModifyDate"

    try:
        parsed_modification_date = parse_complete_metadata_datetime(
            modification_date_text
        )
    except ValueError as error:
        return None, (f"invalid FileModifyDate {modification_date_text!r} ({error})")
    if parsed_modification_date is None:
        return None, f"incomplete FileModifyDate {modification_date_text!r}"

    modification_date, timezone_offset_minutes = parsed_modification_date
    return (
        0,
        modification_date,
        "File:System:FileModifyDate",
        timezone_offset_minutes,
    ), None


# ============================================================================
# TIMEZONE AND DAYLIGHT-SAVING VALIDATION
# ============================================================================
# This section converts offsets, UTC counterparts, and camera settings into
# generic evidence. It determines when a correction is defensible and keeps
# the user-facing DST decision in one place.


def request_classification_timezone():
    """Ask once for an IANA timezone, defaulting to EU CET/CEST rules."""
    while True:
        timezone_name = (
            input(
                f"Timezone for timezone/DST checks [{DEFAULT_TIMEZONE_NAME}]: "
            ).strip()
            or DEFAULT_TIMEZONE_NAME
        )
        try:
            return timezone_name, ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            print(
                f"Unknown timezone '{timezone_name}'. Enter an IANA name such "
                "as Europe/Berlin, Europe/London, or America/New_York."
            )


def timezone_states_for_local_time(local_datetime, classification_timezone):
    """Return valid (DST state, UTC offset minutes, abbreviation) states."""
    states = []
    for fold in (0, 1):
        aware_datetime = local_datetime.replace(
            tzinfo=classification_timezone,
            fold=fold,
        )
        round_trip = (
            aware_datetime.astimezone(timezone.utc)
            .astimezone(classification_timezone)
            .replace(tzinfo=None)
        )
        if round_trip != local_datetime:
            continue

        dst_delta = aware_datetime.dst() or timedelta(0)
        utc_offset = aware_datetime.utcoffset() or timedelta(0)
        state = (
            dst_delta != timedelta(0),
            int(utc_offset.total_seconds() // 60),
            aware_datetime.tzname() or "",
        )
        if state not in states:
            states.append(state)
    return states


def daylight_saving_delta_for_year(classification_timezone, year):
    """Return the largest DST adjustment used by the timezone in that year."""
    current_date = datetime(year, 1, 1, 12)
    end_date = datetime(year + 1, 1, 1, 12)
    largest_delta = timedelta(0)

    while current_date < end_date:
        dst_delta = current_date.replace(
            tzinfo=classification_timezone
        ).dst() or timedelta(0)
        if abs(dst_delta) > abs(largest_delta):
            largest_delta = dst_delta
        current_date += timedelta(days=1)
    return largest_delta


def derive_offset_from_utc(local_datetime, utc_datetime):
    """
    Derive a plausible civil UTC offset from a local/UTC timestamp pair.

    Small sub-minute discrepancies are tolerated because GPS telemetry and the
    camera exposure clock may not be sampled at exactly the same instant.
    """
    raw_seconds = (local_datetime - utc_datetime).total_seconds()
    for day_adjustment in (0, -1, 1, -2, 2):
        adjusted_seconds = raw_seconds + day_adjustment * 86400
        rounded_minutes = int(round(adjusted_seconds / 60))
        residual_seconds = abs(adjusted_seconds - rounded_minutes * 60)
        if -12 * 60 <= rounded_minutes <= 14 * 60 and residual_seconds <= 5:
            return rounded_minutes
    return None


def format_offset(offset_minutes):
    """Format an offset in minutes as +HH:MM or -HH:MM."""
    sign = "+" if offset_minutes >= 0 else "-"
    absolute_minutes = abs(offset_minutes)
    hours, minutes = divmod(absolute_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def resolve_unambiguous_timezone_state(local_datetime, classification_timezone):
    """Return the sole valid (DST, offset, abbreviation) state, or None."""
    states = timezone_states_for_local_time(
        local_datetime,
        classification_timezone,
    )
    return states[0] if len(states) == 1 else None


def resolve_unambiguous_timezone_offset(local_datetime, classification_timezone):
    """Return the sole valid UTC offset for a local time, or None."""
    state = resolve_unambiguous_timezone_state(
        local_datetime,
        classification_timezone,
    )
    return state[1] if state is not None else None


def resolve_system_timestamp_offset(
    local_datetime,
    classification_timezone,
    preferred_offset_minutes=None,
):
    """Resolve one UTC offset for an absolute filesystem timestamp."""
    valid_offsets = {
        state[1]
        for state in timezone_states_for_local_time(
            local_datetime,
            classification_timezone,
        )
    }
    if preferred_offset_minutes in valid_offsets:
        return preferred_offset_minutes
    if len(valid_offsets) == 1:
        return next(iter(valid_offsets))
    return None


def system_datetime_to_epoch_ns(local_datetime, offset_minutes):
    """Convert a chosen local timestamp and offset to Unix nanoseconds."""
    aware_datetime = local_datetime.replace(
        tzinfo=timezone(timedelta(minutes=offset_minutes))
    )
    utc_datetime = aware_datetime.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc_datetime - epoch
    return (
        delta.days * 86400 + delta.seconds
    ) * 1_000_000_000 + delta.microseconds * 1000


def format_labels_by_file(records):
    """Format prebuilt evidence labels with each filename shown once."""
    labels_by_file = {}
    for filename, label in records:
        file_labels = labels_by_file.setdefault(filename, [])
        if label not in file_labels:
            file_labels.append(label)

    return ", ".join(
        f"{filename.name} ({', '.join(labels)})"
        for filename, labels in labels_by_file.items()
    )


def resolve_camera_configuration(filename, context_records):
    """
    Convert camera configuration tags into generic DST and context evidence.

    Direct DaylightSavings tags need no make-specific handling. Pentax-style
    hometown/destination fields are resolved from their active selector by tag
    relationship only, which also supports compatible Ricoh metadata without a
    camera-brand conditional.
    """
    values_by_tag = {}
    for source_name, tag_name, raw_value in context_records:
        values_by_tag.setdefault(tag_name, []).append((source_name, raw_value))

    dst_records = []
    display_records = []

    for source_name, raw_value in values_by_tag.get("DaylightSavings", []):
        state = parse_daylight_savings_value(raw_value)
        if state is not None:
            dst_records.append((filename, source_name, state))

    active_profiles = {
        parse_world_time_location(raw_value)
        for _, raw_value in values_by_tag.get(PROFILE_SELECTOR_TAG, [])
    }
    active_profiles.discard(None)

    if len(active_profiles) == 1:
        active_profile = next(iter(active_profiles))
        active_dst_tag = PROFILE_DST_TAGS[active_profile]
        active_city_tag = PROFILE_CITY_TAGS[active_profile]

        for source_name, raw_value in values_by_tag.get(active_dst_tag, []):
            state = parse_daylight_savings_value(raw_value)
            if state is not None:
                dst_records.append((filename, source_name, state))

        for source_name, raw_value in values_by_tag.get(active_city_tag, []):
            display_records.append(
                (filename, f"{source_name}={raw_value} ({active_profile})")
            )

        for source_name, raw_value in values_by_tag.get(
            PROFILE_SELECTOR_TAG,
            [],
        ):
            display_records.append(
                (filename, f"{source_name}={raw_value} -> {active_profile}")
            )

    for tag_name in ("TimeZoneCity", "TimeZone"):
        for source_name, raw_value in values_by_tag.get(tag_name, []):
            display_records.append((filename, f"{source_name}={raw_value}"))

    offset_records = []
    for tag_name in OFFSET_CONTEXT_TAGS:
        for source_name, raw_value in values_by_tag.get(tag_name, []):
            offset_minutes = parse_context_offset(tag_name, raw_value)
            if offset_minutes is not None:
                offset_records.append(
                    (
                        filename,
                        source_name,
                        str(raw_value).strip(),
                        offset_minutes,
                        "metadata offset",
                    )
                )

    return dst_records, offset_records, display_records


def collect_timezone_evidence(
    selected_datetime,
    selected_date_option,
    base_capture_files,
    context_records_by_file,
    utc_records_by_file,
):
    """Collect offset, UTC-reference, and camera-configuration evidence."""
    dst_records = []
    offset_records = []
    display_records = []

    # Offsets attached directly to the chosen timestamp are strongest and may
    # legitimately originate in an XMP sidecar selected for this group.
    for filename, source_name, offset_minutes in selected_date_option.get(
        "offset_records",
        [],
    ):
        offset_records.append(
            (
                filename,
                source_name,
                "UTC" + format_offset(offset_minutes),
                offset_minutes,
                "timestamp offset",
            )
        )

    # Camera configuration and UTC counterparts are restricted to exact-base
    # images. Sidecars and suffix-derived images cannot define camera settings.
    for base_capture_file in base_capture_files:
        file_dst, file_offsets, file_context = resolve_camera_configuration(
            base_capture_file,
            context_records_by_file.get(base_capture_file, []),
        )
        dst_records.extend(file_dst)
        offset_records.extend(file_offsets)
        display_records.extend(file_context)

        for source_name, utc_datetime in utc_records_by_file.get(
            base_capture_file,
            [],
        ):
            derived_offset = derive_offset_from_utc(
                selected_datetime,
                utc_datetime,
            )
            if derived_offset is not None:
                offset_records.append(
                    (
                        base_capture_file,
                        source_name,
                        utc_datetime.strftime("%Y-%m-%d %H:%M:%S UTC"),
                        derived_offset,
                        "UTC counterpart",
                    )
                )

    # Collapse duplicate telemetry values without losing distinct sources.
    dst_records = list(dict.fromkeys(dst_records))
    offset_records = list(dict.fromkeys(offset_records))
    display_records = list(dict.fromkeys(display_records))
    return dst_records, offset_records, display_records


def build_automatic_correction_key(
    correction_delta,
    recorded_dst_states,
    expected_dst_states,
    observed_offsets,
    expected_offsets,
):
    """Return a conservative key for reusing one approved correction."""
    return (
        int(correction_delta.total_seconds()),
        tuple(sorted(recorded_dst_states)),
        tuple(sorted(expected_dst_states)),
        tuple(sorted(observed_offsets)),
        tuple(sorted(expected_offsets)),
    )


def review_timezone_evidence(
    selected_file_date,
    selected_date_option,
    base_capture_files,
    context_records_by_file,
    utc_records_by_file,
    timezone_name,
    classification_timezone,
    automatic_correction_rules=None,
):
    """Offer corrections only when metadata evidence contradicts the zone."""
    if automatic_correction_rules is None:
        automatic_correction_rules = set()
    if selected_file_date is None:
        return selected_file_date

    selected_datetime = selected_file_date[1]
    valid_states = timezone_states_for_local_time(
        selected_datetime,
        classification_timezone,
    )
    (
        dst_records,
        offset_records,
        display_records,
    ) = collect_timezone_evidence(
        selected_datetime,
        selected_date_option,
        base_capture_files,
        context_records_by_file,
        utc_records_by_file,
    )

    expected_offsets = {state[1] for state in valid_states}
    expected_dst_states = {state[0] for state in valid_states}
    recorded_dst_states = {state for _, _, state in dst_records}
    observed_offsets = {record[3] for record in offset_records}

    mismatched_offsets = observed_offsets - expected_offsets
    mismatched_dst = recorded_dst_states - expected_dst_states

    # A default timezone is an assumption, not proof that a camera clock was
    # wrong. Without contradictory metadata, classification proceeds silently.
    if valid_states and not mismatched_offsets and not mismatched_dst:
        return selected_file_date
    if valid_states and not dst_records and not offset_records:
        return selected_file_date

    print()
    print("Timezone/daylight-saving review for this related group:")
    print(f"  Assumed timezone: {timezone_name}")
    print(f"  Selected local time: {selected_datetime.strftime('%Y-%m-%d %H:%M:%S')}")

    if valid_states:
        expected_text = ", ".join(
            f"{abbreviation or timezone_name} "
            f"UTC{format_offset(offset_minutes)}, "
            f"DST {'ON' if dst_state else 'OFF'}"
            for dst_state, offset_minutes, abbreviation in valid_states
        )
        print(f"  Expected state: {expected_text}")
    else:
        print(
            "  Expected state: this local wall-clock time does not exist in "
            "the selected timezone because it falls in a transition gap."
        )

    if dst_records:
        print(
            "  Camera DST settings: "
            + format_labels_by_file(
                [
                    (
                        filename,
                        f"{source_name}={'ON' if state else 'OFF'}",
                    )
                    for filename, source_name, state in dst_records
                ]
            )
        )

    if offset_records:
        print(
            "  Offset/UTC evidence: "
            + format_labels_by_file(
                [
                    (
                        filename,
                        f"{source_name}={raw_value} -> "
                        f"UTC{format_offset(offset_minutes)} "
                        f"[{evidence_kind}]",
                    )
                    for (
                        filename,
                        source_name,
                        raw_value,
                        offset_minutes,
                        evidence_kind,
                    ) in offset_records
                ]
            )
        )

    if display_records:
        print("  Camera timezone context: " + format_labels_by_file(display_records))

    correction_reasons = {}

    if not valid_states:
        daylight_delta = daylight_saving_delta_for_year(
            classification_timezone,
            selected_datetime.year,
        )
        if daylight_delta != timedelta(0):
            correction_reasons.setdefault(
                selected_datetime + daylight_delta,
                [],
            ).append("move forward across the transition gap")
    else:
        for observed_offset in sorted(mismatched_offsets):
            for expected_offset in sorted(expected_offsets):
                correction_delta = timedelta(minutes=expected_offset - observed_offset)
                corrected_datetime = selected_datetime + correction_delta
                correction_reasons.setdefault(corrected_datetime, []).append(
                    f"convert UTC{format_offset(observed_offset)} evidence "
                    f"to UTC{format_offset(expected_offset)}"
                )

        # A stale DST flag may exist without an explicit/derived offset. In that
        # case retain the original one-hour-style correction behavior.
        if not correction_reasons and len(recorded_dst_states) == 1:
            if len(expected_dst_states) == 1 and mismatched_dst:
                expected_dst = next(iter(expected_dst_states))
                recorded_dst = next(iter(recorded_dst_states))
                daylight_delta = daylight_saving_delta_for_year(
                    classification_timezone,
                    selected_datetime.year,
                )
                if daylight_delta != timedelta(0):
                    correction_delta = (
                        daylight_delta
                        if expected_dst and not recorded_dst
                        else -daylight_delta
                    )
                    correction_reasons.setdefault(
                        selected_datetime + correction_delta,
                        [],
                    ).append("correct the contradictory camera DST setting")

    if not correction_reasons:
        print(
            "  The evidence is contradictory or ambiguous, and no single "
            "defensible correction can be calculated. The recorded time is "
            "kept."
        )
        print()
        return selected_file_date

    matching_offsets = observed_offsets & expected_offsets
    if matching_offsets and mismatched_dst and not mismatched_offsets:
        print(
            "  A timestamp/UTC offset already matches the selected timezone. "
            "Keeping the time is recommended because the camera DST flag may "
            "be stale."
        )
    elif mismatched_offsets and not matching_offsets:
        print(
            "  The available offset evidence does not match the selected "
            "timezone. Applying the corresponding conversion is recommended."
        )

    correction_options = sorted(correction_reasons.items())

    automatic_matches = []
    for corrected_datetime, reasons in correction_options:
        correction_delta = corrected_datetime - selected_datetime
        correction_key = build_automatic_correction_key(
            correction_delta,
            recorded_dst_states,
            expected_dst_states,
            observed_offsets,
            expected_offsets,
        )
        if correction_key in automatic_correction_rules:
            automatic_matches.append((corrected_datetime, reasons, correction_delta))

    # Reuse a prior decision only when one exact evidence/correction signature
    # matches. Ambiguous or materially different cases continue to prompt.
    if len(automatic_matches) == 1:
        corrected_datetime, _, correction_delta = automatic_matches[0]
        signed_hours = correction_delta.total_seconds() / 3600
        hour_unit = "hour" if abs(signed_hours) == 1 else "hours"
        print(
            "  Automatically applying the previously approved matching "
            f"correction ({signed_hours:+g} {hour_unit}): "
            f"{corrected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        print()
        return (2, corrected_datetime)

    print("Choose how this group should be classified:")
    print(f"  1. Keep {selected_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
    for option_number, (corrected_datetime, reasons) in enumerate(
        correction_options,
        start=2,
    ):
        print(
            f"  {option_number}. Correct to "
            f"{corrected_datetime.strftime('%Y-%m-%d %H:%M:%S')} "
            f"({'; '.join(reasons)})"
        )

    while True:
        raw_selection = input(
            f"Enter a number from 1 to {len(correction_options) + 1}: "
        ).strip()
        try:
            selected_number = int(raw_selection)
        except ValueError:
            print("Invalid selection. Enter one of the listed numbers.")
            continue

        if selected_number == 1:
            print()
            return selected_file_date
        correction_index = selected_number - 2
        if 0 <= correction_index < len(correction_options):
            corrected_datetime = correction_options[correction_index][0]
            correction_delta = corrected_datetime - selected_datetime
            correction_key = build_automatic_correction_key(
                correction_delta,
                recorded_dst_states,
                expected_dst_states,
                observed_offsets,
                expected_offsets,
            )
            print(
                "Selected timezone correction: "
                f"{corrected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            while True:
                reuse_selection = (
                    input(
                        "Automatically apply this same correction to all later "
                        "groups with the same daylight-saving/offset mismatch "
                        "during this run? [y/N]: "
                    )
                    .strip()
                    .casefold()
                )
                if reuse_selection in {"", "n", "no"}:
                    break
                if reuse_selection in {"y", "yes"}:
                    automatic_correction_rules.add(correction_key)
                    print(
                        "This exact correction and evidence pattern will be "
                        "applied automatically to later matching groups."
                    )
                    break
                print("Invalid selection. Enter y or n.")

            print()
            return (2, corrected_datetime)
        print("Invalid selection. Enter one of the listed numbers.")


# ============================================================================
# METADATA AND FILESYSTEM CORRECTION WRITING
# ============================================================================
# Correction planning is separate from classification decisions. These
# functions build absolute writes, execute them through ExifTool, verify the
# result, and align supported system timestamps on classified copies.


def add_correction_operation(operations, skipped, operation):
    """Add one absolute write, rejecting ambiguous duplicate target values."""
    target_key = operation["target"].removesuffix("#")
    existing = operations.get(target_key)
    if existing is None:
        operations[target_key] = operation
        return
    if (
        existing["value"] == operation["value"]
        and existing["kind"] == operation["kind"]
        and existing["expected"] == operation["expected"]
    ):
        return
    operations.pop(target_key, None)
    skipped.add(f"{target_key} (conflicting duplicate values)")


def format_context_offset_value(tag_name, original_value, offset_minutes):
    """Preserve a standalone offset field's representation where practical."""
    if tag_name == "TimeZoneOffset" and isinstance(original_value, (int, float)):
        numeric_hours = offset_minutes / 60
        return (
            str(int(numeric_hours))
            if numeric_hours.is_integer()
            else str(numeric_hours)
        )

    original_text = str(original_value).strip()
    replacement = format_offset(offset_minutes)
    if SIGNED_OFFSET_PATTERN.search(original_text):
        return SIGNED_OFFSET_PATTERN.sub(replacement, original_text, count=1)
    return replacement


def raw_dst_write_value(groups, original_value, expected_dst):
    """Preserve maker encodings when known, with Canon's 60-minute ON value."""
    if not expected_dst:
        return "0"
    try:
        numeric_value = int(float(str(original_value).strip()))
    except ValueError:
        numeric_value = 0
    if numeric_value:
        return str(numeric_value)
    if any(group_name.casefold().startswith("canon") for group_name in groups):
        return "60"
    return "1"


def operation_matches(operation, actual_values):
    """Compare read-back values semantically instead of by formatting alone."""
    kind = operation["kind"]
    expected = operation["expected"]
    for actual_value in actual_values:
        try:
            if kind == "datetime":
                parsed = parse_complete_metadata_datetime(actual_value)
                if parsed == expected:
                    return True
            elif kind == "date":
                match = DATE_ONLY_PATTERN.fullmatch(str(actual_value).strip())
                if (
                    match is not None
                    and (
                        int(match.group("year")),
                        int(match.group("month")),
                        int(match.group("day")),
                    )
                    == expected
                ):
                    return True
            elif kind == "time":
                match = TIME_ONLY_PATTERN.fullmatch(str(actual_value).strip())
                if match is not None:
                    actual_time = (
                        int(match.group("hour")),
                        int(match.group("minute")),
                        int(match.group("second")),
                        parse_timezone_offset_minutes(match.group("timezone")),
                    )
                    if actual_time == expected:
                        return True
            elif kind == "offset":
                if (
                    parse_context_offset(operation["tag_name"], actual_value)
                    == expected
                ):
                    return True
            elif kind == "dst":
                if parse_daylight_savings_value(actual_value) == expected:
                    return True
        except (TypeError, ValueError):
            continue
    return False


def index_correction_metadata(records):
    """
    Index metadata once for the correction-planning helpers.

    ``records_by_family_tag`` keeps duplicate values scoped to their ExifTool
    family path. ``values_by_tag`` supports context fields whose semantics do
    not depend on one specific storage family.
    """
    records_by_family_tag = {}
    values_by_tag = {}
    for metadata_key, groups, tag_name, raw_value in records:
        family_key = tuple(
            group_name
            for group_name in groups
            if not group_name.casefold().startswith("copy")
        )
        records_by_family_tag.setdefault((family_key, tag_name), []).append(
            (metadata_key, groups, raw_value)
        )
        values_by_tag.setdefault(tag_name, []).append((metadata_key, groups, raw_value))
    return records_by_family_tag, values_by_tag


def plan_complete_timestamp_corrections(
    records,
    records_by_family_tag,
    timezone_correction_active,
    effective_correction_delta,
    selected_corrected_datetime,
    classification_timezone,
    conflicting_date_values,
    operations,
    skipped,
):
    """
    Plan writes for complete local timestamps and inline offsets.

    Rejected conflict values are replaced by the selected date. Other local
    timestamps are shifted only for an approved timezone correction. Values
    with a separate OffsetTime* field are left unchanged here so the offset
    planner can update the paired offset without shifting the wall clock.
    """
    offset_presence = {
        (family_key[0] if family_key else "", tag_name)
        for (family_key, tag_name), tag_records in records_by_family_tag.items()
        if tag_records and tag_name in set(ASSOCIATED_OFFSET_TAGS.values())
    }
    corrected_local_values = {}

    for metadata_key, groups, tag_name, raw_value in records:
        if (
            tag_name in TIME_CONTEXT_TAGS
            or tag_name in UTC_REFERENCE_TAGS
            or tag_name in NONLOCAL_CORRECTION_TAGS
            or tag_name in {"FileCreateDate", "FileModifyDate", "FileAccessDate"}
            or not groups
            or groups[0] == "Composite"
        ):
            continue

        try:
            parsed = parse_complete_metadata_datetime(raw_value)
        except ValueError:
            skipped.add(f"{metadata_key} (invalid timestamp)")
            continue
        if parsed is None:
            continue

        local_datetime, inline_offset = parsed
        family_zero = groups[0]
        associated_offset_tag = ASSOCIATED_OFFSET_TAGS.get(tag_name)
        has_separate_offset = (
            associated_offset_tag is not None
            and (family_zero, associated_offset_tag) in offset_presence
        )
        write_target = get_metadata_write_target(groups, tag_name)
        if write_target is None:
            continue

        align_to_selected = (
            local_datetime in conflicting_date_values
            and tag_name not in EXCLUDED_TIME_TAGS
        )
        if not timezone_correction_active and not align_to_selected:
            continue

        if align_to_selected:
            expected_local = selected_corrected_datetime
            if inline_offset is not None:
                expected_offset = resolve_unambiguous_timezone_offset(
                    expected_local,
                    classification_timezone,
                )
                if expected_offset is None:
                    skipped.add(f"{metadata_key} (ambiguous selected-date offset)")
                    continue
                expected_text = format_complete_datetime(
                    raw_value,
                    expected_local,
                    expected_offset,
                )
                expected = (expected_local, expected_offset)
            else:
                expected_text = format_complete_datetime(
                    raw_value,
                    expected_local,
                )
                expected = (expected_local, None)
        elif inline_offset is not None:
            expected_offset = resolve_unambiguous_timezone_offset(
                local_datetime,
                classification_timezone,
            )
            if expected_offset is None:
                skipped.add(f"{metadata_key} (ambiguous corrected offset)")
                continue
            expected_local = local_datetime
            expected_text = format_complete_datetime(
                raw_value,
                expected_local,
                expected_offset,
            )
            expected = (expected_local, expected_offset)
        elif has_separate_offset:
            expected_local = local_datetime
            corrected_local_values.setdefault(
                (family_zero, tag_name),
                [],
            ).append(expected_local)
            continue
        else:
            expected_local = local_datetime + effective_correction_delta
            expected_text = format_complete_datetime(
                raw_value,
                expected_local,
            )
            expected = (expected_local, None)

        corrected_local_values.setdefault(
            (family_zero, tag_name),
            [],
        ).append(expected_local)
        add_correction_operation(
            operations,
            skipped,
            {
                "target": write_target,
                "value": expected_text,
                "kind": "datetime",
                "expected": expected,
                "tag_name": tag_name,
                "source": metadata_key,
            },
        )

    return corrected_local_values


def plan_partial_timestamp_corrections(
    records_by_family_tag,
    timezone_correction_active,
    effective_correction_delta,
    selected_corrected_datetime,
    classification_timezone,
    conflicting_date_values,
    operations,
    skipped,
):
    """Plan paired IPTC-style date and time writes, including day rollover."""
    for date_tag, time_tag in PARTIAL_DATE_TIME_PAIRS:
        family_keys = {
            family_key
            for family_key, tag_name in records_by_family_tag
            if tag_name in {date_tag, time_tag}
        }
        for family_key in family_keys:
            date_records = records_by_family_tag.get(
                (family_key, date_tag),
                [],
            )
            time_records = records_by_family_tag.get(
                (family_key, time_tag),
                [],
            )
            if len(date_records) != 1 or len(time_records) != 1:
                continue

            date_key, date_groups, date_value = date_records[0]
            time_key, time_groups, time_value = time_records[0]
            try:
                parsed_pair = parse_partial_datetime(date_value, time_value)
            except ValueError:
                parsed_pair = None
            if parsed_pair is None:
                continue

            local_datetime, inline_offset = parsed_pair
            align_to_selected = local_datetime in conflicting_date_values
            if not timezone_correction_active and not align_to_selected:
                continue

            if align_to_selected:
                expected_local = selected_corrected_datetime
                if inline_offset is None:
                    expected_offset = None
                else:
                    expected_offset = resolve_unambiguous_timezone_offset(
                        expected_local,
                        classification_timezone,
                    )
                    if expected_offset is None:
                        skipped.add(f"{time_key} (ambiguous selected-date offset)")
                        continue
            elif inline_offset is None:
                expected_local = local_datetime + effective_correction_delta
                expected_offset = None
            else:
                expected_local = local_datetime
                expected_offset = resolve_unambiguous_timezone_offset(
                    local_datetime,
                    classification_timezone,
                )
                if expected_offset is None:
                    skipped.add(f"{time_key} (ambiguous corrected offset)")
                    continue

            date_target = get_metadata_write_target(date_groups, date_tag)
            time_target = get_metadata_write_target(time_groups, time_tag)
            if date_target is not None:
                add_correction_operation(
                    operations,
                    skipped,
                    {
                        "target": date_target,
                        "value": format_date_only(
                            date_value,
                            expected_local,
                        ),
                        "kind": "date",
                        "expected": (
                            expected_local.year,
                            expected_local.month,
                            expected_local.day,
                        ),
                        "tag_name": date_tag,
                        "source": date_key,
                    },
                )
            if time_target is not None:
                add_correction_operation(
                    operations,
                    skipped,
                    {
                        "target": time_target,
                        "value": format_time_only(
                            time_value,
                            expected_local,
                            expected_offset,
                        ),
                        "kind": "time",
                        "expected": (
                            expected_local.hour,
                            expected_local.minute,
                            expected_local.second,
                            expected_offset,
                        ),
                        "tag_name": time_tag,
                        "source": time_key,
                    },
                )


def plan_offset_corrections(
    values_by_tag,
    corrected_local_values,
    metadata_correction_active,
    selected_corrected_datetime,
    classification_timezone,
    operations,
    skipped,
):
    """Plan writes for existing standalone offset fields."""
    if not metadata_correction_active:
        return

    inverse_offset_tags = {
        offset_tag: date_tag for date_tag, offset_tag in ASSOCIATED_OFFSET_TAGS.items()
    }
    for tag_name in sorted(OFFSET_CONTEXT_TAGS):
        for metadata_key, groups, raw_value in values_by_tag.get(
            tag_name,
            [],
        ):
            family_zero = groups[0] if groups else ""
            reference_values = corrected_local_values.get(
                (family_zero, inverse_offset_tags.get(tag_name)),
                [],
            )
            unique_reference_values = set(reference_values)
            reference_datetime = (
                next(iter(unique_reference_values))
                if len(unique_reference_values) == 1
                else selected_corrected_datetime
            )
            expected_offset = resolve_unambiguous_timezone_offset(
                reference_datetime,
                classification_timezone,
            )
            if expected_offset is None:
                skipped.add(f"{metadata_key} (ambiguous corrected offset)")
                continue

            write_target = get_metadata_write_target(
                groups,
                tag_name,
                raw=(
                    tag_name == "TimeZoneOffset" and isinstance(raw_value, (int, float))
                ),
            )
            if write_target is None:
                continue

            add_correction_operation(
                operations,
                skipped,
                {
                    "target": write_target,
                    "value": format_context_offset_value(
                        tag_name,
                        raw_value,
                        expected_offset,
                    ),
                    "kind": "offset",
                    "expected": expected_offset,
                    "tag_name": tag_name,
                    "source": metadata_key,
                },
            )


def plan_dst_setting_corrections(
    values_by_tag,
    metadata_correction_active,
    selected_corrected_datetime,
    classification_timezone,
    operations,
    skipped,
):
    """
    Plan writes for direct or active-profile daylight-saving settings.

    The active Pentax/Ricoh profile is selected from WorldTimeLocation. The
    inactive profile remains unchanged, and no camera make/model dispatch is
    needed.
    """
    if not metadata_correction_active:
        return

    expected_state = resolve_unambiguous_timezone_state(
        selected_corrected_datetime,
        classification_timezone,
    )
    if expected_state is None:
        return

    expected_dst = expected_state[0]
    active_profiles = {
        parse_world_time_location(raw_value)
        for _, _, raw_value in values_by_tag.get(
            PROFILE_SELECTOR_TAG,
            [],
        )
    }
    active_profiles.discard(None)

    active_profile_tag = None
    if len(active_profiles) == 1:
        active_profile_tag = PROFILE_DST_TAGS[next(iter(active_profiles))]

    dst_tag_names = set(DIRECT_DST_TAGS)
    if active_profile_tag is not None:
        dst_tag_names.add(active_profile_tag)

    for tag_name in sorted(dst_tag_names):
        for metadata_key, groups, raw_value in values_by_tag.get(
            tag_name,
            [],
        ):
            write_target = get_metadata_write_target(
                groups,
                tag_name,
                raw=True,
            )
            if write_target is None:
                continue

            add_correction_operation(
                operations,
                skipped,
                {
                    "target": write_target,
                    "value": raw_dst_write_value(
                        groups,
                        raw_value,
                        expected_dst,
                    ),
                    "kind": "dst",
                    "expected": expected_dst,
                    "tag_name": tag_name,
                    "source": metadata_key,
                },
            )


def plan_file_create_date_alignment(
    records,
    align_system_times,
    selected_corrected_datetime,
    classification_timezone,
    preferred_system_offset_minutes,
    operations,
    skipped,
):
    """Align an existing FileCreateDate to the absolute selected instant."""
    if not align_system_times:
        return

    expected_offset = resolve_system_timestamp_offset(
        selected_corrected_datetime,
        classification_timezone,
        preferred_system_offset_minutes,
    )
    if expected_offset is None:
        skipped.add("FileCreateDate (ambiguous selected-date UTC offset)")
        return

    expected = (selected_corrected_datetime, expected_offset)
    for metadata_key, groups, tag_name, raw_value in records:
        if tag_name != "FileCreateDate":
            continue
        try:
            parsed = parse_complete_metadata_datetime(raw_value)
        except ValueError:
            parsed = None
        if parsed is None:
            skipped.add(f"{metadata_key} (invalid FileCreateDate)")
            continue
        if parsed == expected:
            continue

        add_correction_operation(
            operations,
            skipped,
            {
                "target": "FileCreateDate",
                "value": format_complete_datetime(
                    raw_value,
                    selected_corrected_datetime,
                    expected_offset,
                ),
                "kind": "datetime",
                "expected": expected,
                "tag_name": tag_name,
                "source": metadata_key,
            },
        )


def build_correction_operations(
    source_metadata,
    correction_delta,
    selected_corrected_datetime,
    classification_timezone,
    conflicting_date_values=None,
    align_system_times=False,
    preferred_system_offset_minutes=None,
):
    """
    Build all metadata and FileCreateDate writes for a classified copy.

    The wrapper coordinates focused planners for complete timestamps, paired
    date/time fields, offsets, DST settings, and system creation time. Each
    planner adds absolute, verifiable operations to the same operation map.
    """
    timezone_correction_active = correction_delta is not None
    effective_correction_delta = correction_delta or timedelta(0)
    conflicting_date_values = set(conflicting_date_values or ())
    metadata_correction_active = timezone_correction_active or bool(
        conflicting_date_values
    )

    operations = {}
    skipped = set()
    records = list(iter_metadata_values(source_metadata))
    records_by_family_tag, values_by_tag = index_correction_metadata(records)

    corrected_local_values = plan_complete_timestamp_corrections(
        records,
        records_by_family_tag,
        timezone_correction_active,
        effective_correction_delta,
        selected_corrected_datetime,
        classification_timezone,
        conflicting_date_values,
        operations,
        skipped,
    )
    plan_partial_timestamp_corrections(
        records_by_family_tag,
        timezone_correction_active,
        effective_correction_delta,
        selected_corrected_datetime,
        classification_timezone,
        conflicting_date_values,
        operations,
        skipped,
    )
    plan_offset_corrections(
        values_by_tag,
        corrected_local_values,
        metadata_correction_active,
        selected_corrected_datetime,
        classification_timezone,
        operations,
        skipped,
    )
    plan_dst_setting_corrections(
        values_by_tag,
        metadata_correction_active,
        selected_corrected_datetime,
        classification_timezone,
        operations,
        skipped,
    )
    plan_file_create_date_alignment(
        records,
        align_system_times,
        selected_corrected_datetime,
        classification_timezone,
        preferred_system_offset_minutes,
        operations,
        skipped,
    )
    return list(operations.values()), skipped


def execute_correction_operations(metadata_reader, filename, operations):
    """Write all supported operations, then verify them by reading the copy."""
    skipped = set()
    if operations:
        write_arguments = list(CORRECTION_WRITE_PARAMS)
        write_arguments.extend(
            f"-{operation['target']}={operation['value']}" for operation in operations
        )
        write_arguments.append(str(filename))
        try:
            metadata_reader.execute(*write_arguments)
        except ExifToolException:
            # A file may contain a mixture of writable and read-only tags. Retry
            # individually so unsupported maker-note fields do not block common
            # EXIF/XMP fields that ExifTool can safely write.
            for operation in operations:
                try:
                    metadata_reader.execute(
                        *CORRECTION_WRITE_PARAMS,
                        f"-{operation['target']}={operation['value']}",
                        str(filename),
                    )
                except ExifToolException as error:
                    skipped.add(f"{operation['target'].removesuffix('#')} ({error})")

    destination_metadata = read_correction_metadata(metadata_reader, filename)
    actual_by_target = {}
    for _, groups, tag_name, raw_value in iter_metadata_values(destination_metadata):
        target = get_metadata_write_target(groups, tag_name)
        if target is not None:
            actual_by_target.setdefault(target, []).append(raw_value)
        elif tag_name == "FileCreateDate":
            actual_by_target.setdefault("FileCreateDate", []).append(raw_value)

    updated = []
    for operation in operations:
        target = operation["target"].removesuffix("#")
        if operation_matches(operation, actual_by_target.get(target, [])):
            updated.append(target)
        else:
            skipped.add(f"{target} (not writable or verification failed)")
    return sorted(set(updated)), sorted(skipped)


def update_corrected_copy_metadata(
    metadata_reader,
    source_file,
    destination_file,
    correction_delta,
    selected_corrected_datetime,
    classification_timezone,
    conflicting_date_values=None,
    align_system_times=False,
    preferred_system_offset_minutes=None,
):
    """Correct metadata and align supported system times in a copy."""
    source_metadata = read_correction_metadata(metadata_reader, source_file)
    operations, plan_skipped = build_correction_operations(
        source_metadata,
        correction_delta,
        selected_corrected_datetime,
        classification_timezone,
        conflicting_date_values,
        align_system_times,
        preferred_system_offset_minutes,
    )
    updated, write_skipped = execute_correction_operations(
        metadata_reader,
        destination_file,
        operations,
    )

    # FileModifyDate is the most widely consumed system timestamp, so
    # align it to the absolute final selected capture time rather than
    # applying a delta to an unrelated source-file modification time.
    if align_system_times:
        expected_offset = resolve_system_timestamp_offset(
            selected_corrected_datetime,
            classification_timezone,
            preferred_system_offset_minutes,
        )
        if expected_offset is None:
            plan_skipped.add("FileModifyDate (ambiguous selected-date UTC offset)")
        else:
            selected_mtime_ns = system_datetime_to_epoch_ns(
                selected_corrected_datetime,
                expected_offset,
            )
            destination_stat = destination_file.stat()
            if destination_stat.st_mtime_ns != selected_mtime_ns:
                os.utime(
                    destination_file,
                    ns=(destination_stat.st_atime_ns, selected_mtime_ns),
                )
                if destination_file.stat().st_mtime_ns != selected_mtime_ns:
                    raise OSError(
                        "could not verify aligned FileModifyDate for "
                        f"'{destination_file}'"
                    )
                updated.append("FileModifyDate")
    return sorted(set(updated)), sorted(set(plan_skipped) | set(write_skipped))


# ============================================================================
# DATE CLASSIFICATION AND MISMATCH HANDLING
# ============================================================================
# Date options are assembled and displayed here. The workflow records rejected
# embedded values so only relevant capture/creation fields are normalized
# after the user chooses the authoritative date.


def build_date_options(file_dates):
    """
    Merge identical timestamps from all related files into display options.

    Each option retains its strongest date type, source fields, and explicit
    offsets so later conflict and timezone review can use the same evidence.
    """
    date_options = {}
    for same_stem_file, date_candidates in file_dates.items():
        for (
            date_type,
            date_value,
            source_name,
            timezone_offset_minutes,
        ) in date_candidates:
            date_option = date_options.setdefault(
                date_value,
                {
                    "date_type": date_type,
                    "sources": [],
                    "offset_records": [],
                },
            )
            date_option["date_type"] = max(
                date_option["date_type"],
                date_type,
            )
            date_option["sources"].append((same_stem_file, source_name))

            if timezone_offset_minutes is not None:
                offset_record = (
                    same_stem_file,
                    source_name,
                    timezone_offset_minutes,
                )
                if offset_record not in date_option["offset_records"]:
                    date_option["offset_records"].append(offset_record)
    return date_options


def choose_group_date(date_options, same_stem_files, file_dates):
    """
    Select the authoritative group timestamp and record rejected values.

    A single date is accepted silently. Multiple distinct dates are shown
    with their exact metadata sources, and the user's selection determines
    which rejected capture/creation values are aligned in classified copies.
    """
    if not date_options:
        return None, None, False, {}

    if len(date_options) == 1:
        date_value, date_option = next(iter(date_options.items()))
        return (
            (date_option["date_type"], date_value),
            date_option,
            False,
            {},
        )

    sorted_date_options = sorted(date_options.items())
    print()
    print("Conflicting dates found for this related group:")
    for same_stem_file in same_stem_files:
        print(f"  - {same_stem_file.name}")

    print("Choose the date that should be used for this group:")
    for option_number, (date_value, date_option) in enumerate(
        sorted_date_options,
        start=1,
    ):
        print(
            f"  {option_number}. "
            f"{date_value.strftime('%Y-%m-%d %H:%M:%S')} - "
            f"{format_labels_by_file(date_option['sources'])}"
        )

    while True:
        raw_selection = input(
            f"Enter a number from 1 to {len(sorted_date_options)}: "
        ).strip()
        try:
            selected_option_number = int(raw_selection)
        except ValueError:
            print("Invalid selection. Enter one of the listed numbers.")
            continue

        if not 1 <= selected_option_number <= len(sorted_date_options):
            print("Invalid selection. Enter one of the listed numbers.")
            continue

        selected_date_value, selected_date_option = sorted_date_options[
            selected_option_number - 1
        ]
        selected_file_date = (
            selected_date_option["date_type"],
            selected_date_value,
        )
        print(f"Selected date: {selected_date_value.strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        break

    conflicting_date_values_by_file = {
        filename: {
            candidate[1]
            for candidate in candidates
            if candidate[0] == 1 and candidate[1] != selected_date_value
        }
        for filename, candidates in file_dates.items()
    }
    conflicting_date_values_by_file = {
        filename: values
        for filename, values in conflicting_date_values_by_file.items()
        if values
    }

    return (
        selected_file_date,
        selected_date_option,
        True,
        conflicting_date_values_by_file,
    )


# ============================================================================
# RELATED-FILE GROUPING AND COLLISION-SAFE COPYING
# ============================================================================
# These functions determine related stems, preserve originals, avoid
# overwrites, detect binary duplicates, and update only classified copies.


def group_related_files(files):
    """Group exact stems and attach derivatives to the longest matching base."""
    files_by_stem = {}
    for filename in files:
        files_by_stem.setdefault(filename.stem, []).append(filename)

    groups = {}
    sorted_stems = sorted(
        files_by_stem,
        key=lambda stem: (len(stem), stem.casefold()),
    )
    for stem in sorted_stems:
        folded_stem = stem.casefold()
        matching_bases = []
        for base_stem in groups:
            folded_base_stem = base_stem.casefold()
            if len(folded_stem) <= len(folded_base_stem):
                continue
            if not folded_stem.startswith(folded_base_stem):
                continue

            first_added_character = folded_stem[len(folded_base_stem)]
            if "0" <= first_added_character <= "9":
                continue
            matching_bases.append(base_stem)

        if matching_bases:
            groups[max(matching_bases, key=len)].extend(files_by_stem[stem])
        else:
            groups[stem] = list(files_by_stem[stem])

    return [
        (
            base_stem,
            sorted(group, key=lambda path: path.name.casefold()),
        )
        for base_stem, group in sorted(
            groups.items(),
            key=lambda item: item[0].casefold(),
        )
    ]


def files_are_binary_identical(first_file, second_file):
    """Compare files byte-for-byte without loading either file fully."""
    if not second_file.is_file():
        return False
    if first_file.stat().st_size != second_file.stat().st_size:
        return False

    with first_file.open("rb") as first_stream:
        with second_file.open("rb") as second_stream:
            while True:
                first_chunk = first_stream.read(COPY_CHUNK_SIZE)
                second_chunk = second_stream.read(COPY_CHUNK_SIZE)
                if first_chunk != second_chunk:
                    return False
                if not first_chunk:
                    return True


def copy_file_safely(source_file, requested_destination):
    """Copy without overwriting, returning destination/copy/rename status."""
    suffix_number = 0
    while True:
        if suffix_number == 0:
            destination_file = requested_destination
        else:
            destination_file = requested_destination.with_name(
                f"{requested_destination.stem}_{suffix_number}"
                f"{requested_destination.suffix}"
            )

        if destination_file.exists():
            if files_are_binary_identical(source_file, destination_file):
                return destination_file, False, suffix_number > 0
            suffix_number += 1
            continue

        destination_created = False
        try:
            with source_file.open("rb") as source_stream:
                with destination_file.open("xb") as destination_stream:
                    destination_created = True
                    shutil.copyfileobj(
                        source_stream,
                        destination_stream,
                        length=COPY_CHUNK_SIZE,
                    )
            shutil.copystat(source_file, destination_file)
        except FileExistsError:
            continue
        except OSError:
            if destination_created:
                try:
                    destination_file.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        return destination_file, True, suffix_number > 0


def find_previous_corrected_duplicate(destination_file, requested_destination):
    """Find an earlier collision candidate identical to a corrected new copy."""
    suffix_number = 0
    while True:
        candidate = (
            requested_destination
            if suffix_number == 0
            else requested_destination.with_name(
                f"{requested_destination.stem}_{suffix_number}"
                f"{requested_destination.suffix}"
            )
        )
        if candidate == destination_file:
            return None
        if candidate.is_file() and files_are_binary_identical(
            destination_file,
            candidate,
        ):
            return candidate
        suffix_number += 1


def copy_file_with_corrected_metadata(
    metadata_reader,
    source_file,
    requested_destination,
    correction_delta,
    selected_corrected_datetime,
    classification_timezone,
    conflicting_date_values=None,
    align_system_times=False,
    preferred_system_offset_minutes=None,
):
    """Copy first, then correct and verify the classified copy in place."""
    destination_file, copied, renamed = copy_file_safely(
        source_file,
        requested_destination,
    )
    try:
        updated_targets, skipped_targets = update_corrected_copy_metadata(
            metadata_reader,
            source_file,
            destination_file,
            correction_delta,
            selected_corrected_datetime,
            classification_timezone,
            conflicting_date_values,
            align_system_times,
            preferred_system_offset_minutes,
        )
    except OSError:
        if copied:
            try:
                destination_file.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    if copied:
        previous_duplicate = find_previous_corrected_duplicate(
            destination_file,
            requested_destination,
        )
        if previous_duplicate is not None:
            destination_file.unlink()
            return (
                previous_duplicate,
                False,
                previous_duplicate != requested_destination,
                updated_targets,
                skipped_targets,
                True,
            )

    return (
        destination_file,
        copied,
        renamed,
        updated_targets,
        skipped_targets,
        False,
    )


# ============================================================================
# CLASSIFICATION WORKFLOW AND COMMAND-LINE ENTRY POINT
# ============================================================================
# Main exposes the complete startup sequence. Focused helpers below handle
# metadata processing, per-group decisions, copying, and final reporting.


def scan_related_group(metadata_reader, base_stem, same_stem_files):
    """
    Read all files in one related group and collect classification evidence.

    Embedded metadata is considered first. FileModifyDate fallback is added
    only when the complete group has no usable embedded timestamp, and only
    exact-base image files may supply that fallback.
    """
    scan = {
        "file_dates": {},
        "usable_files": set(),
        "base_image_files": [],
        "base_capture_files": [],
        "context_records_by_file": {},
        "utc_records_by_file": {},
        "review_reasons": {},
        "failures": 0,
    }

    for same_stem_file in same_stem_files:
        try:
            (
                date_candidates,
                review_reason,
                file_is_image,
                context_records,
                utc_records,
                file_is_capture_media,
            ) = get_dates(metadata_reader, same_stem_file)
        except OSError as error:
            print(
                f"Error reading '{same_stem_file.name}': {error}",
                file=sys.stderr,
            )
            scan["failures"] += 1
            continue

        if review_reason is not None:
            print(
                f"Warning: '{same_stem_file.name}' requires review: "
                f"{review_reason}",
                file=sys.stderr,
            )
            scan["review_reasons"][same_stem_file] = review_reason
            continue

        scan["usable_files"].add(same_stem_file)
        if context_records:
            scan["context_records_by_file"][same_stem_file] = context_records
        if utc_records:
            scan["utc_records_by_file"][same_stem_file] = utc_records
        if date_candidates:
            scan["file_dates"][same_stem_file] = date_candidates

        if same_stem_file.stem.casefold() == base_stem.casefold():
            if file_is_image:
                scan["base_image_files"].append(same_stem_file)
            if file_is_capture_media:
                scan["base_capture_files"].append(same_stem_file)

    if not scan["file_dates"]:
        for base_image_file in scan["base_image_files"]:
            try:
                file_modify_candidate, review_reason = get_file_modify_date(
                    metadata_reader,
                    base_image_file,
                )
            except OSError as error:
                print(
                    f"Error reading '{base_image_file.name}': {error}",
                    file=sys.stderr,
                )
                scan["failures"] += 1
                scan["usable_files"].discard(base_image_file)
                continue

            if review_reason is not None:
                print(
                    f"Warning: '{base_image_file.name}' requires review: "
                    f"{review_reason}",
                    file=sys.stderr,
                )
                scan["review_reasons"][base_image_file] = review_reason
                scan["usable_files"].discard(base_image_file)
                continue

            scan["file_dates"].setdefault(base_image_file, []).append(
                file_modify_candidate
            )

    if not scan["file_dates"]:
        for same_stem_file in scan["usable_files"]:
            scan["review_reasons"][
                same_stem_file
            ] = "no embedded timestamp and no base image FileModifyDate"

    return scan


def review_group_timezone(
    selected_file_date,
    selected_date_option,
    scan,
    timezone_name,
    classification_timezone,
    automatic_correction_rules,
):
    """
    Apply timezone/DST review and return the final date plus write context.

    The returned delta is non-None only when the user, or a matching
    session rule, accepted a timezone correction. The preferred system
    offset is taken from the selected timestamp only when it is unique.
    """
    if selected_file_date is None or selected_date_option is None:
        return selected_file_date, None, None

    uncorrected_datetime = selected_file_date[1]
    selected_file_date = review_timezone_evidence(
        selected_file_date,
        selected_date_option,
        scan["base_capture_files"],
        scan["context_records_by_file"],
        scan["utc_records_by_file"],
        timezone_name,
        classification_timezone,
        automatic_correction_rules,
    )

    timezone_correction_delta = None
    if selected_file_date[0] == 2:
        timezone_correction_delta = selected_file_date[1] - uncorrected_datetime

    selected_offsets = {
        offset_record[2]
        for offset_record in selected_date_option.get("offset_records", [])
    }
    preferred_system_offset_minutes = (
        next(iter(selected_offsets)) if len(selected_offsets) == 1 else None
    )
    return (
        selected_file_date,
        timezone_correction_delta,
        preferred_system_offset_minutes,
    )


def copy_group_files(
    metadata_reader,
    same_stem_files,
    scan,
    selected_file_date,
    date_conflict_resolved,
    conflicting_date_values_by_file,
    timezone_correction_delta,
    preferred_system_offset_minutes,
    classification_timezone,
    classified_directory,
    unclassified_directory,
    stats,
):
    """
    Copy one related group and apply approved metadata/system corrections.

    Review files go to ``unclassified``. Classified files use the selected
    date folder, collision-safe copying, and the existing correction writer.
    All counters and console reporting are updated in this one place.
    """
    review_reasons = scan["review_reasons"]
    usable_files = scan["usable_files"]

    for same_stem_file in same_stem_files:
        if same_stem_file in review_reasons:
            folder_name = UNCLASSIFIED_FOLDER_NAME
            destination_directory = unclassified_directory
        elif same_stem_file in usable_files and selected_file_date is not None:
            date_folder_name = selected_file_date[1].strftime("%Y-%m-%d")
            folder_name = f"{CLASSIFIED_FOLDER_NAME}/{date_folder_name}"
            destination_directory = classified_directory / date_folder_name
        else:
            continue

        if destination_directory.exists() and not destination_directory.is_dir():
            print(
                f"Error: '{destination_directory}' is a file. "
                f"Skipping '{same_stem_file.name}'.",
                file=sys.stderr,
            )
            stats["failed"] += 1
            continue

        updated_metadata_targets = []
        skipped_metadata_targets = []
        conflicting_date_values = conflicting_date_values_by_file.get(
            same_stem_file,
            set(),
        )
        align_system_times = (
            date_conflict_resolved or timezone_correction_delta is not None
        ) and same_stem_file not in review_reasons
        corrected_copy = (
            align_system_times or bool(conflicting_date_values)
        ) and same_stem_file not in review_reasons
        already_corrected = False

        try:
            destination_directory.mkdir(parents=True, exist_ok=True)
            requested_destination = destination_directory / same_stem_file.name
            if corrected_copy:
                (
                    destination_file,
                    copied,
                    renamed,
                    updated_metadata_targets,
                    skipped_metadata_targets,
                    already_corrected,
                ) = copy_file_with_corrected_metadata(
                    metadata_reader,
                    same_stem_file,
                    requested_destination,
                    timezone_correction_delta,
                    selected_file_date[1],
                    classification_timezone,
                    conflicting_date_values,
                    align_system_times,
                    preferred_system_offset_minutes,
                )
            else:
                destination_file, copied, renamed = copy_file_safely(
                    same_stem_file,
                    requested_destination,
                )
        except OSError as error:
            print(
                f"Error copying or updating '{same_stem_file.name}': {error}",
                file=sys.stderr,
            )
            stats["failed"] += 1
            continue

        print(
            f"{same_stem_file.name}\t--->\t{folder_name}\t",
            end="",
        )
        if copied:
            stats["copied"] += 1
            if renamed:
                stats["renamed"] += 1
                print(f"COPIED AS {destination_file.name}", end="")
            else:
                print("COPIED", end="")
        else:
            stats["duplicates"] += 1
            print(f"DUPLICATE OF {destination_file.name}", end="")

        if corrected_copy:
            if copied:
                stats["metadata_updated"] += 1
            if skipped_metadata_targets:
                stats["metadata_skipped"] += 1
            if already_corrected:
                print("; METADATA ALREADY CORRECTED", end="")
            elif updated_metadata_targets:
                print(
                    "; METADATA UPDATED: " + ", ".join(updated_metadata_targets),
                    end="",
                )
            if skipped_metadata_targets:
                print(
                    "; NOT WRITABLE/SKIPPED: " + ", ".join(skipped_metadata_targets),
                    end="",
                )

        if same_stem_file in review_reasons:
            stats["review"] += 1
            print(
                f"; REVIEW: {review_reasons[same_stem_file]}",
                end="",
            )
        else:
            print(
                f"; {selected_file_date[1].strftime('%Y-%m-%d %H:%M:%S')} "
                f"{DATETYPE[selected_file_date[0]]}",
                end="",
            )
        print()


def process_related_group(
    metadata_reader,
    base_stem,
    same_stem_files,
    timezone_name,
    classification_timezone,
    automatic_correction_rules,
    classified_directory,
    unclassified_directory,
    stats,
):
    """Run the complete read, decision, DST review, and copy flow for a group."""
    scan = scan_related_group(
        metadata_reader,
        base_stem,
        same_stem_files,
    )
    stats["failed"] += scan["failures"]

    date_options = build_date_options(scan["file_dates"])
    (
        selected_file_date,
        selected_date_option,
        date_conflict_resolved,
        conflicting_date_values_by_file,
    ) = choose_group_date(
        date_options,
        same_stem_files,
        scan["file_dates"],
    )

    (
        selected_file_date,
        timezone_correction_delta,
        preferred_system_offset_minutes,
    ) = review_group_timezone(
        selected_file_date,
        selected_date_option,
        scan,
        timezone_name,
        classification_timezone,
        automatic_correction_rules,
    )

    copy_group_files(
        metadata_reader,
        same_stem_files,
        scan,
        selected_file_date,
        date_conflict_resolved,
        conflicting_date_values_by_file,
        timezone_correction_delta,
        preferred_system_offset_minutes,
        classification_timezone,
        classified_directory,
        unclassified_directory,
        stats,
    )


def print_run_summary(
    classified_directory,
    unclassified_directory,
    stats,
):
    """Print the final destination paths and all processing counters."""
    print()
    print(f"Classified directory: {classified_directory}")
    print(f"Unclassified directory: {unclassified_directory}")
    print(f"Copied files: {stats['copied']}")
    print(f"Binary duplicates: {stats['duplicates']}")
    print(f"Renamed collision copies: {stats['renamed']}")
    print(f"Files sent for review: {stats['review']}")
    print(f"Failed files: {stats['failed']}")
    print(f"Copies with corrected metadata/system time: {stats['metadata_updated']}")
    print(
        "Corrected copies with skipped read-only fields: "
        f"{stats['metadata_skipped']}"
    )
    print("Original source files were not modified.")


def run_classification(
    source_files,
    timezone_name,
    classification_timezone,
    classified_directory,
    unclassified_directory,
):
    """Process all related groups through one persistent ExifTool session."""
    same_stem_groups = group_related_files(source_files)
    try:
        metadata_reader = ExifToolHelper(common_args=["-G0:1:4"])
        metadata_reader.run()
    except (FileNotFoundError, OSError, ValueError, ExifToolException) as error:
        print(
            "Error: PyExifTool could not start the ExifTool executable. "
            "Install ExifTool and make sure it is available on PATH. "
            f"Details: {error}"
        )
        input("Press Enter to exit")
        return 1

    stats = {
        "copied": 0,
        "duplicates": 0,
        "renamed": 0,
        "review": 0,
        "failed": 0,
        "metadata_updated": 0,
        "metadata_skipped": 0,
    }
    automatic_correction_rules = set()

    try:
        for base_stem, same_stem_files in same_stem_groups:
            process_related_group(
                metadata_reader,
                base_stem,
                same_stem_files,
                timezone_name,
                classification_timezone,
                automatic_correction_rules,
                classified_directory,
                unclassified_directory,
                stats,
            )
    finally:
        metadata_reader.terminate()

    print_run_summary(
        classified_directory,
        unclassified_directory,
        stats,
    )
    input("Press Enter to exit")
    return 2 if stats["failed"] else 0


def main():
    """Validate startup configuration and run the interactive classifier."""
    source_directory = clean_input_path(
        input("Please write (or drag) the source directory path: ")
    )
    if not source_directory.exists() or not source_directory.is_dir():
        print(f"Error: source directory is invalid: '{source_directory}'")
        input("Press Enter to exit")
        return 1

    source_directory = source_directory.resolve()
    timezone_name, classification_timezone = request_classification_timezone()

    classified_directory = source_directory / CLASSIFIED_FOLDER_NAME
    unclassified_directory = source_directory / UNCLASSIFIED_FOLDER_NAME
    for output_directory in (classified_directory, unclassified_directory):
        if output_directory.exists() and not output_directory.is_dir():
            print(
                f"Error: '{output_directory}' already exists as a file. "
                "It must be a directory."
            )
            input("Press Enter to exit")
            return 1

    try:
        classified_directory.mkdir(exist_ok=True)
        unclassified_directory.mkdir(exist_ok=True)
        source_files = sorted(
            (
                filename
                for filename in source_directory.iterdir()
                if filename.is_file() and not filename.is_symlink()
            ),
            key=lambda filename: filename.name.casefold(),
        )
    except OSError as error:
        print(f"Error preparing directories: {error}")
        input("Press Enter to exit")
        return 1

    return run_classification(
        source_files,
        timezone_name,
        classification_timezone,
        classified_directory,
        unclassified_directory,
    )


if __name__ == "__main__":
    raise SystemExit(main())
