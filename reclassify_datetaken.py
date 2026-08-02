import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from exiftool import ExifToolHelper
from exiftool.exceptions import ExifToolException, ExifToolExecuteError


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
FILE_MODIFY_DATE_PARAMS = [
    "-d",
    "%Y:%m:%d %H:%M:%S",
]

EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"

# A user-approved timezone correction is also written to existing common EXIF
# timestamp fields in each image copy. The fields are shifted independently by
# the same exact timedelta, preserving legitimate differences between capture,
# digitization, and modification times instead of replacing every field with
# one selected value. Missing EXIF date fields are not invented.
EXIF_DATE_OFFSET_TAGS = {
    "DateTimeOriginal": "OffsetTimeOriginal",
    "CreateDate": "OffsetTimeDigitized",
    "ModifyDate": "OffsetTime",
}
EXIF_CORRECTION_READ_TAGS = [
    *(f"EXIF:{tag_name}" for tag_name in EXIF_DATE_OFFSET_TAGS),
    *(f"EXIF:{tag_name}" for tag_name in EXIF_DATE_OFFSET_TAGS.values()),
]
EXIF_CORRECTION_WRITE_PARAMS = [
    # Work only on a temporary copy, but still avoid ExifTool backup files.
    "-overwrite_original_in_place",
    # Preserve the copied filesystem modification timestamp while metadata is
    # rewritten. copystat() below restores all copied attributes once more.
    "-P",
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
}

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


def split_metadata_key(metadata_key):
    """Split a -G0:1:4 ExifTool key into group path and final tag name."""
    key_parts = metadata_key.split(":")
    return key_parts[:-1], key_parts[-1]


def iter_metadata_values(metadata):
    """Yield one item for every scalar or list value returned by ExifTool."""
    for metadata_key, metadata_value in metadata.items():
        if metadata_key == "SourceFile":
            continue

        groups, tag_name = split_metadata_key(metadata_key)
        values = (
            metadata_value
            if isinstance(metadata_value, list)
            else [metadata_value]
        )

        for value in values:
            if value is not None:
                yield metadata_key, groups, tag_name, value


def get_metadata_values(metadata, tag_name):
    """Return all values whose final ExifTool tag name matches tag_name."""
    return [
        value
        for _, _, current_tag_name, value in iter_metadata_values(metadata)
        if current_tag_name == tag_name
    ]


def get_first_metadata_value(metadata, tag_name):
    """Return the first value for a requested ExifTool tag, or None."""
    values = get_metadata_values(metadata, tag_name)
    return values[0] if values else None


def is_image_file(filename, metadata):
    """Return True when ExifTool or the filename identifies an image."""
    mime_type = get_first_metadata_value(metadata, "MIMEType")
    if isinstance(mime_type, str) and mime_type.casefold().startswith("image/"):
        return True
    return filename.suffix.casefold() in IMAGE_EXTENSIONS


def is_capture_media_file(filename, metadata):
    """Identify base image/video files that may supply timezone evidence."""
    mime_type = get_first_metadata_value(metadata, "MIMEType")
    if isinstance(mime_type, str):
        folded_mime = mime_type.casefold()
        if folded_mime.startswith(("image/", "video/")):
            return True
    return filename.suffix.casefold() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


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


def request_classification_timezone():
    """Ask once for an IANA timezone, defaulting to EU CET/CEST rules."""
    while True:
        timezone_name = input(
            "Timezone for timezone/DST checks "
            f"[{DEFAULT_TIMEZONE_NAME}]: "
        ).strip() or DEFAULT_TIMEZONE_NAME
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
        dst_delta = (
            current_date.replace(tzinfo=classification_timezone).dst()
            or timedelta(0)
        )
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


def format_utc_offset(offset_minutes):
    """Format an offset in minutes as UTC+HH:MM or UTC-HH:MM."""
    sign = "+" if offset_minutes >= 0 else "-"
    absolute_minutes = abs(offset_minutes)
    hours, minutes = divmod(absolute_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"



def format_exif_offset(offset_minutes):
    """Format an offset in minutes for standard EXIF OffsetTime* fields."""
    sign = "+" if offset_minutes >= 0 else "-"
    absolute_minutes = abs(offset_minutes)
    hours, minutes = divmod(absolute_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def get_exif_write_target(groups, tag_name):
    """Return the family-1 EXIF target represented by a -G0:1:4 key."""
    if not groups or groups[0] != "EXIF":
        return None

    # Family 1 identifies the actual IFD. Family 4 may add extraction-only
    # instance labels such as Copy1, which must not be used for writing.
    if len(groups) >= 2 and not groups[1].casefold().startswith("copy"):
        return f"{groups[1]}:{tag_name}"
    return f"EXIF:{tag_name}"


def read_exif_correction_state(metadata_reader, filename):
    """
    Read existing common EXIF timestamps and standard offset fields.

    Timestamp values are keyed by their writable IFD target so duplicate EXIF
    locations are shifted and verified independently rather than collapsed by
    final tag name.
    """
    try:
        metadata_results = metadata_reader.get_tags(
            files=filename,
            tags=EXIF_CORRECTION_READ_TAGS,
            params=["-a"],
        )
    except ExifToolException as error:
        raise OSError(
            f"ExifTool could not inspect EXIF correction fields in "
            f"'{filename}': {error}"
        ) from error

    timestamps_by_target = {}
    offsets_by_target = {}
    offset_targets_by_tag = {}
    if not metadata_results:
        return (
            timestamps_by_target,
            offsets_by_target,
            offset_targets_by_tag,
        )

    offset_tag_names = set(EXIF_DATE_OFFSET_TAGS.values())
    for metadata_key, groups, tag_name, metadata_value in iter_metadata_values(
        metadata_results[0]
    ):
        if not groups or groups[0] != "EXIF":
            continue

        write_target = get_exif_write_target(groups, tag_name)
        if tag_name in EXIF_DATE_OFFSET_TAGS:
            try:
                parsed_date = parse_complete_metadata_datetime(metadata_value)
            except ValueError as error:
                raise OSError(
                    f"cannot shift invalid EXIF timestamp "
                    f"{metadata_key}={metadata_value!r}: {error}"
                ) from error
            if parsed_date is None:
                raise OSError(
                    f"cannot shift incomplete EXIF timestamp "
                    f"{metadata_key}={metadata_value!r}"
                )
            timestamps_by_target.setdefault(write_target, []).append(
                parsed_date[0]
            )
            continue

        if tag_name in offset_tag_names:
            offset_minutes = parse_context_offset(tag_name, metadata_value)
            if offset_minutes is None:
                raise OSError(
                    f"cannot interpret EXIF offset "
                    f"{metadata_key}={metadata_value!r}"
                )
            offsets_by_target.setdefault(write_target, []).append(
                offset_minutes
            )
            offset_targets_by_tag.setdefault(tag_name, set()).add(
                write_target
            )

    return timestamps_by_target, offsets_by_target, offset_targets_by_tag


def get_unambiguous_timezone_offset(local_datetime, classification_timezone):
    """Return the sole valid UTC offset for a local time, or None."""
    offsets = {
        state[1]
        for state in timezone_states_for_local_time(
            local_datetime,
            classification_timezone,
        )
    }
    return next(iter(offsets)) if len(offsets) == 1 else None


def update_corrected_exif_metadata(
    metadata_reader,
    filename,
    correction_delta,
    classification_timezone,
):
    """
    Shift and verify existing common EXIF timestamps in one temporary copy.

    DateTimeOriginal, CreateDate, and ModifyDate are shifted at every EXIF IFD
    where they already exist. Their corresponding standard OffsetTime* fields
    are set to the corrected IANA-zone offset when that offset is unambiguous.
    Vendor maker notes and UTC/GPS reference fields are intentionally untouched.
    """
    (
        timestamps_before,
        _,
        existing_offset_targets,
    ) = read_exif_correction_state(metadata_reader, filename)
    if not timestamps_before:
        return []

    correction_seconds = int(correction_delta.total_seconds())
    if correction_delta != timedelta(seconds=correction_seconds):
        raise OSError(
            "timezone correction contains subsecond precision and cannot be "
            "applied safely to whole-second EXIF date fields"
        )
    if correction_seconds == 0:
        return []

    shift_operator = "+=" if correction_seconds > 0 else "-="
    shift_value = f"0:0:{abs(correction_seconds)}"
    write_arguments = list(EXIF_CORRECTION_WRITE_PARAMS)
    for write_target in sorted(timestamps_before):
        write_arguments.append(
            f"-{write_target}{shift_operator}{shift_value}"
        )

    # Determine the correct offset independently for original, digitized, and
    # modification times. ModifyDate may legitimately fall under a different
    # seasonal offset than the capture date. One standard OffsetTime* field
    # cannot represent conflicting duplicate dates, so such a file is rejected
    # instead of writing a value that is correct for only one occurrence.
    shifted_values_by_tag = {}
    for write_target, old_values in timestamps_before.items():
        timestamp_tag_name = write_target.rsplit(":", 1)[-1]
        shifted_values_by_tag.setdefault(timestamp_tag_name, []).extend(
            old_value + correction_delta for old_value in old_values
        )

    expected_offset_targets = {}
    for timestamp_tag_name, shifted_values in sorted(
        shifted_values_by_tag.items()
    ):
        offset_results = [
            get_unambiguous_timezone_offset(
                shifted_value,
                classification_timezone,
            )
            for shifted_value in shifted_values
        ]
        if any(offset_result is None for offset_result in offset_results):
            # At least one value falls in the repeated autumn hour. Do not use
            # another duplicate occurrence to guess the ambiguous offset.
            continue
        possible_offsets = set(offset_results)
        if len(possible_offsets) > 1:
            raise OSError(
                f"cannot assign one EXIF offset to conflicting shifted "
                f"{timestamp_tag_name} values: {shifted_values!r}"
            )
        if not possible_offsets:
            # The repeated autumn hour has two valid offsets. Do not guess.
            continue

        target_offset_minutes = next(iter(possible_offsets))
        offset_text = format_exif_offset(target_offset_minutes)
        offset_tag_name = EXIF_DATE_OFFSET_TAGS[timestamp_tag_name]
        targets = set(existing_offset_targets.get(offset_tag_name, set()))
        # These tags are standardized in ExifIFD. Create the standard field when
        # absent and also update any existing duplicate EXIF location.
        targets.add(f"ExifIFD:{offset_tag_name}")
        for write_target in sorted(targets):
            write_arguments.append(f"-{write_target}={offset_text}")
            expected_offset_targets[write_target] = target_offset_minutes

    write_arguments.append(str(filename))
    try:
        metadata_reader.execute(*write_arguments)
    except ExifToolException as error:
        raise OSError(
            f"ExifTool could not update corrected EXIF metadata in "
            f"'{filename}': {error}"
        ) from error

    (
        timestamps_after,
        offsets_after,
        _,
    ) = read_exif_correction_state(metadata_reader, filename)

    verification_errors = []
    for write_target, old_values in timestamps_before.items():
        expected_values = sorted(
            old_value + correction_delta for old_value in old_values
        )
        actual_values = sorted(timestamps_after.get(write_target, []))
        if actual_values != expected_values:
            verification_errors.append(
                f"{write_target}={actual_values!r}, expected "
                f"{expected_values!r}"
            )

    for write_target, expected_offset in expected_offset_targets.items():
        actual_offsets = offsets_after.get(write_target, [])
        if not actual_offsets or any(
            actual_offset != expected_offset
            for actual_offset in actual_offsets
        ):
            verification_errors.append(
                f"{write_target}={actual_offsets!r}, expected "
                f"{format_exif_offset(expected_offset)!r}"
            )

    if verification_errors:
        raise OSError(
            "ExifTool verification failed after writing corrected metadata: "
            + "; ".join(verification_errors)
        )

    return sorted(timestamps_before)


def format_records_by_file(records):
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
            display_records.append(
                (filename, f"{source_name}={raw_value}")
            )

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
                format_utc_offset(offset_minutes),
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


def review_timezone_evidence(
    selected_file_date,
    selected_date_option,
    base_capture_files,
    context_records_by_file,
    utc_records_by_file,
    timezone_name,
    classification_timezone,
):
    """Offer corrections only when metadata evidence contradicts the zone."""
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
    print(
        "  Selected local time: "
        f"{selected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    if valid_states:
        expected_text = ", ".join(
            f"{abbreviation or timezone_name} "
            f"{format_utc_offset(offset_minutes)}, "
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
            + format_records_by_file(
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
            + format_records_by_file(
                [
                    (
                        filename,
                        f"{source_name}={raw_value} -> "
                        f"{format_utc_offset(offset_minutes)} "
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
        print(
            "  Camera timezone context: "
            + format_records_by_file(display_records)
        )

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
                correction_delta = timedelta(
                    minutes=expected_offset - observed_offset
                )
                corrected_datetime = selected_datetime + correction_delta
                correction_reasons.setdefault(corrected_datetime, []).append(
                    f"convert {format_utc_offset(observed_offset)} evidence "
                    f"to {format_utc_offset(expected_offset)}"
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
    print("Choose how this group should be classified:")
    print(
        "  1. Keep "
        f"{selected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
    )
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
            print(
                "Selected timezone correction: "
                f"{corrected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            print()
            return (2, corrected_datetime)
        print("Invalid selection. Enter one of the listed numbers.")


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
            error_message = (
                str(error.stderr).strip() if error.stderr else str(error)
            )
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
    file_is_image = is_image_file(filename, metadata)
    file_is_capture_media = is_capture_media_file(filename, metadata)
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

        if tag_name in {"FileType", "MIMEType"}:
            continue
        if "System" in groups or tag_name in EXCLUDED_TIME_TAGS:
            continue

        try:
            parsed_metadata_date = parse_complete_metadata_datetime(
                metadata_value
            )
        except ValueError:
            invalid_date_messages.append(
                f"{metadata_key}={metadata_value!r}"
            )
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
    """Return the separately requested FileModifyDate fallback candidate."""
    try:
        metadata_results = metadata_reader.get_tags(
            files=filename,
            tags=FILE_MODIFY_DATE_TAGS,
            params=FILE_MODIFY_DATE_PARAMS,
        )
    except ExifToolException as error:
        raise OSError(
            f"ExifTool could not read FileModifyDate from '{filename}': "
            f"{error}"
        ) from error

    if not metadata_results:
        return None, "ExifTool returned no FileModifyDate"

    modification_date_text = get_first_metadata_value(
        metadata_results[0],
        "FileModifyDate",
    )
    if modification_date_text is None:
        return None, "ExifTool returned no FileModifyDate"

    try:
        modification_date = datetime.strptime(
            str(modification_date_text),
            EXIF_DATE_FORMAT,
        )
    except ValueError as error:
        return None, (
            f"invalid FileModifyDate {modification_date_text!r} ({error})"
        )

    return (
        0,
        modification_date,
        "File:System:FileModifyDate",
        None,
    ), None


def format_date_sources(sources):
    """Format one timestamp's source fields with each filename shown once."""
    fields_by_file = {}
    for filename, source_name in sources:
        source_fields = fields_by_file.setdefault(filename, [])
        if source_name not in source_fields:
            source_fields.append(source_name)

    return ", ".join(
        f"{filename.name} ({', '.join(source_fields)})"
        for filename, source_fields in fields_by_file.items()
    )


def stems_are_related(base_stem, longer_stem):
    """Return whether longer_stem is a non-numeric-suffix derivative."""
    base_stem = base_stem.casefold()
    longer_stem = longer_stem.casefold()
    if (
        len(longer_stem) <= len(base_stem)
        or not longer_stem.startswith(base_stem)
    ):
        return False
    first_added_character = longer_stem[len(base_stem)]
    return not ("0" <= first_added_character <= "9")


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
        matching_bases = [
            base_stem
            for base_stem in groups
            if stems_are_related(base_stem, stem)
        ]
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


def copy_file_with_corrected_exif(
    metadata_reader,
    source_file,
    requested_destination,
    correction_delta,
    classification_timezone,
):
    """
    Build and verify a corrected temporary copy before collision-safe placement.

    Metadata is never written to the source. Duplicate comparison uses the final
    corrected bytes, allowing repeated runs to recognize an existing corrected
    output rather than creating another numbered copy.
    """
    temporary_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".reclassify_",
            suffix=source_file.suffix,
            dir=requested_destination.parent,
            delete=False,
        ) as temporary_stream:
            temporary_file = Path(temporary_stream.name)
            with source_file.open("rb") as source_stream:
                shutil.copyfileobj(
                    source_stream,
                    temporary_stream,
                    length=COPY_CHUNK_SIZE,
                )

        shutil.copystat(source_file, temporary_file)
        updated_targets = update_corrected_exif_metadata(
            metadata_reader,
            temporary_file,
            correction_delta,
            classification_timezone,
        )
        # ExifTool may rewrite the temporary file. Restore the source copy's
        # filesystem attributes after metadata verification.
        shutil.copystat(source_file, temporary_file)

        destination_file, copied, renamed = copy_file_safely(
            temporary_file,
            requested_destination,
        )
        return destination_file, copied, renamed, updated_targets

    finally:
        if temporary_file is not None:
            try:
                temporary_file.unlink(missing_ok=True)
            except OSError:
                pass


# Request the source directory and intentionally process only regular files
# directly inside it. Existing subdirectories are never traversed.
directory = clean_input_path(
    input("Please write (or drag) the source directory path: ")
)
if not directory.exists() or not directory.is_dir():
    print(f"Error: source directory is invalid: '{directory}'")
    input("Press Enter to exit")
    raise SystemExit(1)

directory = directory.resolve()
timezone_name, classification_timezone = request_classification_timezone()

classified_directory = directory / CLASSIFIED_FOLDER_NAME
unclassified_directory = directory / UNCLASSIFIED_FOLDER_NAME

for output_directory in (classified_directory, unclassified_directory):
    if output_directory.exists() and not output_directory.is_dir():
        print(
            f"Error: '{output_directory}' already exists as a file. "
            "It must be a directory."
        )
        input("Press Enter to exit")
        raise SystemExit(1)

try:
    classified_directory.mkdir(exist_ok=True)
    unclassified_directory.mkdir(exist_ok=True)
    source_files = sorted(
        (
            filename
            for filename in directory.iterdir()
            if filename.is_file() and not filename.is_symlink()
        ),
        key=lambda filename: filename.name.casefold(),
    )
except OSError as error:
    print(f"Error preparing directories: {error}")
    input("Press Enter to exit")
    raise SystemExit(1)

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
    raise SystemExit(1)

copied_count = 0
duplicate_count = 0
renamed_count = 0
review_count = 0
failed_count = 0
exif_updated_count = 0
exif_absent_count = 0

try:
    for base_stem, same_stem_files in same_stem_groups:
        file_dates = {}
        usable_files = set()
        image_files = set()
        base_image_files = []
        base_capture_files = []
        context_records_by_file = {}
        utc_records_by_file = {}
        review_reasons = {}
        selected_file_date = None
        selected_date_option = None
        timezone_correction_delta = None

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
                failed_count += 1
                continue

            if review_reason is not None:
                print(
                    f"Warning: '{same_stem_file.name}' requires review: "
                    f"{review_reason}",
                    file=sys.stderr,
                )
                review_reasons[same_stem_file] = review_reason
                continue

            usable_files.add(same_stem_file)
            if file_is_image:
                image_files.add(same_stem_file)
            if context_records:
                context_records_by_file[same_stem_file] = context_records
            if utc_records:
                utc_records_by_file[same_stem_file] = utc_records
            if date_candidates:
                file_dates[same_stem_file] = date_candidates

            if same_stem_file.stem.casefold() == base_stem.casefold():
                if file_is_image:
                    base_image_files.append(same_stem_file)
                if file_is_capture_media:
                    base_capture_files.append(same_stem_file)

        # Filesystem fallback is separate and restricted to exact-base images.
        if not file_dates:
            for base_image_file in base_image_files:
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
                    failed_count += 1
                    usable_files.discard(base_image_file)
                    continue

                if review_reason is not None:
                    print(
                        f"Warning: '{base_image_file.name}' requires review: "
                        f"{review_reason}",
                        file=sys.stderr,
                    )
                    review_reasons[base_image_file] = review_reason
                    usable_files.discard(base_image_file)
                    continue

                file_dates.setdefault(base_image_file, []).append(
                    file_modify_candidate
                )

        if not file_dates:
            for same_stem_file in usable_files:
                review_reasons[same_stem_file] = (
                    "no embedded timestamp and no base image FileModifyDate"
                )

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
                date_option["sources"].append(
                    (same_stem_file, source_name)
                )
                if timezone_offset_minutes is not None:
                    offset_record = (
                        same_stem_file,
                        source_name,
                        timezone_offset_minutes,
                    )
                    if offset_record not in date_option["offset_records"]:
                        date_option["offset_records"].append(offset_record)

        if len(date_options) == 1:
            date_value, date_option = next(iter(date_options.items()))
            selected_date_option = date_option
            selected_file_date = (date_option["date_type"], date_value)

        elif len(date_options) > 1:
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
                    f"{format_date_sources(date_option['sources'])}"
                )

            while True:
                raw_selection = input(
                    f"Enter a number from 1 to "
                    f"{len(sorted_date_options)}: "
                ).strip()
                try:
                    selected_option_number = int(raw_selection)
                except ValueError:
                    print(
                        "Invalid selection. Enter one of the listed numbers."
                    )
                    continue

                if not 1 <= selected_option_number <= len(sorted_date_options):
                    print(
                        "Invalid selection. Enter one of the listed numbers."
                    )
                    continue

                (
                    selected_date_value,
                    selected_date_option,
                ) = sorted_date_options[selected_option_number - 1]
                selected_file_date = (
                    selected_date_option["date_type"],
                    selected_date_value,
                )
                print(
                    "Selected date: "
                    f"{selected_date_value.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                print()
                break

        if (
            selected_file_date is not None
            and selected_file_date[0] == 1
            and selected_date_option is not None
        ):
            uncorrected_datetime = selected_file_date[1]
            selected_file_date = review_timezone_evidence(
                selected_file_date,
                selected_date_option,
                base_capture_files,
                context_records_by_file,
                utc_records_by_file,
                timezone_name,
                classification_timezone,
            )
            if selected_file_date[0] == 2:
                timezone_correction_delta = (
                    selected_file_date[1] - uncorrected_datetime
                )

        for same_stem_file in same_stem_files:
            if same_stem_file in review_reasons:
                folder_name = UNCLASSIFIED_FOLDER_NAME
                destination_directory = unclassified_directory
            elif (
                same_stem_file in usable_files
                and selected_file_date is not None
            ):
                date_folder_name = selected_file_date[1].strftime("%Y-%m-%d")
                folder_name = f"{CLASSIFIED_FOLDER_NAME}/{date_folder_name}"
                destination_directory = classified_directory / date_folder_name
            else:
                continue

            if (
                destination_directory.exists()
                and not destination_directory.is_dir()
            ):
                print(
                    f"Error: '{destination_directory}' is a file. "
                    f"Skipping '{same_stem_file.name}'.",
                    file=sys.stderr,
                )
                failed_count += 1
                continue

            updated_exif_targets = []
            corrected_image_copy = (
                timezone_correction_delta is not None
                and same_stem_file not in review_reasons
                and same_stem_file in image_files
            )

            try:
                destination_directory.mkdir(parents=True, exist_ok=True)
                requested_destination = (
                    destination_directory / same_stem_file.name
                )
                if corrected_image_copy:
                    (
                        destination_file,
                        copied,
                        renamed,
                        updated_exif_targets,
                    ) = copy_file_with_corrected_exif(
                        metadata_reader,
                        same_stem_file,
                        requested_destination,
                        timezone_correction_delta,
                        classification_timezone,
                    )
                else:
                    destination_file, copied, renamed = copy_file_safely(
                        same_stem_file,
                        requested_destination,
                    )
            except OSError as error:
                print(
                    f"Error copying or updating '{same_stem_file.name}': "
                    f"{error}",
                    file=sys.stderr,
                )
                failed_count += 1
                continue

            print(
                f"{same_stem_file.name}\t--->\t{folder_name}\t",
                end="",
            )
            if copied:
                copied_count += 1
                if renamed:
                    renamed_count += 1
                    print(f"COPIED AS {destination_file.name}", end="")
                else:
                    print("COPIED", end="")
            else:
                duplicate_count += 1
                print(
                    f"DUPLICATE OF {destination_file.name}",
                    end="",
                )

            if corrected_image_copy:
                if updated_exif_targets:
                    if copied:
                        exif_updated_count += 1
                        print(
                            "; EXIF UPDATED: "
                            + ", ".join(updated_exif_targets),
                            end="",
                        )
                    else:
                        print("; EXIF ALREADY CORRECTED", end="")
                else:
                    if copied:
                        exif_absent_count += 1
                    print(
                        "; NO EXISTING COMMON EXIF DATE FIELDS",
                        end="",
                    )

            if same_stem_file in review_reasons:
                review_count += 1
                print(
                    f"; REVIEW: {review_reasons[same_stem_file]}",
                    end="",
                )
            else:
                print(
                    f"; "
                    f"{selected_file_date[1].strftime('%Y-%m-%d %H:%M:%S')} "
                    f"{DATETYPE[selected_file_date[0]]}",
                    end="",
                )
            print()
finally:
    metadata_reader.terminate()

print()
print(f"Classified directory: {classified_directory}")
print(f"Unclassified directory: {unclassified_directory}")
print(f"Copied files: {copied_count}")
print(f"Binary duplicates: {duplicate_count}")
print(f"Renamed collision copies: {renamed_count}")
print(f"Files sent for review: {review_count}")
print(f"Failed files: {failed_count}")
print(f"Copies with corrected EXIF timestamps: {exif_updated_count}")
print(
    "Corrected image copies without common EXIF date fields: "
    f"{exif_absent_count}"
)
print("Original source files were not modified.")

input("Press Enter to exit")
if failed_count:
    raise SystemExit(2)
