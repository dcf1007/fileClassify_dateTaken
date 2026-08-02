import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from exiftool import ExifToolHelper
from exiftool.exceptions import ExifToolException, ExifToolExecuteError


DATETYPE = {
    0: "OS_DATE",
    1: "METADATA",
    2: "METADATA_DST_CORRECTED",
}

DEFAULT_TIMEZONE_NAME = "Europe/Berlin"

# Read ExifTool's complete Time group instead of naming individual EXIF tags.
# Time:All includes date/time tags from EXIF, XMP, IPTC, QuickTime, maker notes,
# composite tags, and other metadata families supported by ExifTool.
#
# Normal classification deliberately does not request ExifTool's Validate,
# Warning, or Error pseudo-tags. Validate performs additional conformance
# checks and can generate warnings about non-standard tag locations even when
# the timestamps themselves are fully readable. Those checks are independent
# of timestamp discovery and therefore do not belong in this classification
# pass.
EXIFTOOL_TAGS = [
    "Time:All",
    "File:FileType",
    "File:MIMEType",
    # Raw maker-note value: Nikon stores 0/1, Canon stores 0/60.
    "DaylightSavings#",
    # Timezone context is not a classification date, but helps explain and
    # corroborate a daylight-saving mismatch.
    "TimeZone",
    "TimeZoneCity",
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
    "TimeZoneOffset",
]

# Refine the Time:All request inside ExifTool instead of extracting everything
# and discarding unwanted values afterward. The excluded tags describe camera
# operation, metadata/editing activity, resource modification, or recording end
# rather than when the image or recording was originally created.
#
# Capture-oriented maker-note fields such as SonyDateTime, SonyDateTime2,
# PanasonicDateTime, and Olympus DateTimeUTC are intentionally retained.
EXCLUDED_TIME_TAGS = (
    # Camera/device operation rather than capture.
    "PowerUpTime",
    "TimeSincePowerOn",
    "RunTimeSincePowerUp",
    "ShotNumberSincePowerUp",
    # Metadata and application history.
    "MetadataDate",
    "HistoryWhen",
    # Modification timestamps rather than original creation.
    "ModifyDate",
    "SubSecModifyDate",
    "MediaModifyDate",
    "TrackModifyDate",
    "LastModifyDate",
    # Recording or resource end timestamps.
    "DateTimeEnd",
    "EndTime",
    # Dates belonging to embedded resources or edit structures.
    "ProfileDateTime",
    "LayerModifyDates",
)

# -a retains duplicate tags and -ee reads supported embedded documents and
# streams. The embedded pass deliberately has no global -d format: raw values
# are needed to distinguish complete timestamps from date-only and time-only
# IPTC fields. Family-1 System is excluded so filesystem pseudo-tags never enter
# the embedded-metadata pass.
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
TIME_CONTEXT_TAGS = {
    "DaylightSavings",
    "TimeZone",
    "TimeZoneCity",
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
    "TimeZoneOffset",
}
TIMESTAMP_OFFSET_TAGS = {
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
    "TimeZoneOffset",
}
COMPLETE_DATE_TIME_PATTERN = re.compile(
    r"^(?P<year>\d{4})[:-](?P<month>\d{2})[:-](?P<day>\d{2})[ T]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<timezone>Z|[+-]\d{2}:?\d{2})?$"
)
CLASSIFIED_FOLDER_NAME = "classified"
UNCLASSIFIED_FOLDER_NAME = "unclassified"
COPY_CHUNK_SIZE = 1024 * 1024

# This extension list is used only when ExifTool cannot identify a damaged file
# from its contents. It replaces Pillow's registered-extension check.
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
    """
    Split an ExifTool JSON key into its group path and final tag name.

    ExifTool is started with -G0:1:4, so a key may look like:

        EXIF:ExifIFD:Copy1:DateTimeOriginal

    Family 0 identifies the metadata type, family 1 identifies the exact
    storage location, and family 4 gives duplicate instances unique JSON names.
    A DateTimeOriginal stored in IFD0 is therefore retained with its real
    location instead of being hidden by another copy.
    """
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


def parse_timezone_offset_minutes(timezone_text):
    """Return a numeric UTC offset in minutes, or None when absent."""
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


def parse_metadata_offset_value(metadata_value):
    """Parse a standalone ExifTool timezone-offset value when possible."""
    if isinstance(metadata_value, (int, float)):
        numeric_value = float(metadata_value)
        # EXIF TimeZoneOffset is expressed in hours. Some maker-note TimeZone
        # fields use minutes, but those are context only and are not passed here.
        if -24 <= numeric_value <= 24:
            return int(numeric_value * 60)
        return None

    value_text = str(metadata_value).strip()
    offset_match = re.search(
        r"(?P<sign>[+-])(?P<hour>\d{2}):?(?P<minute>\d{2})",
        value_text,
    )
    if offset_match is None:
        return None

    timezone_text = (
        offset_match.group("sign")
        + offset_match.group("hour")
        + ":"
        + offset_match.group("minute")
    )
    return parse_timezone_offset_minutes(timezone_text)


def parse_complete_metadata_datetime(metadata_value):
    """
    Parse one complete ExifTool date/time into canonical classification data.

    Raw values are retained until this boundary so partial date-only and
    time-only fields can be rejected. Complete timestamps are canonicalized to
    one-second wall-clock precision, while an explicit UTC offset is returned
    separately as supporting timezone evidence.

    Return (local_datetime, utc_offset_minutes), or None for a partial/non-date
    value. An explicit offset is not applied to the wall-clock value because the
    classifier organizes files by the photographer's local calendar date.
    """
    date_text = str(metadata_value).strip()
    date_match = COMPLETE_DATE_TIME_PATTERN.fullmatch(date_text)
    if date_match is None:
        return None

    timezone_offset_minutes = parse_timezone_offset_minutes(
        date_match.group("timezone")
    )

    # Construct only through whole seconds. Fractional precision may differ
    # between EXIF and XMP representations of the same exposure, but it does not
    # change the classifier's second-resolution identity.
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
    """Normalize maker-note daylight-saving values to True, False, or None."""
    if isinstance(metadata_value, bool):
        return metadata_value
    if isinstance(metadata_value, (int, float)):
        return metadata_value != 0

    value_text = str(metadata_value).strip().casefold()
    if value_text in {"on", "yes", "true", "enabled"}:
        return True
    if value_text in {"off", "no", "false", "disabled"}:
        return False

    try:
        return float(value_text) != 0
    except ValueError:
        return None


def request_classification_timezone():
    """Ask once for an IANA timezone, defaulting to EU CET/CEST rules."""
    while True:
        timezone_name = input(
            "Timezone for daylight-saving checks "
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
    """
    Return valid (DST state, UTC offset, abbreviation) states for local time.

    Most local times have one state. The repeated hour at the autumn transition
    has two. A spring-forward gap has none. UTC round-tripping distinguishes
    these cases instead of assuming that attaching tzinfo always creates a
    valid local time.
    """
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
        aware_date = current_date.replace(tzinfo=classification_timezone)
        dst_delta = aware_date.dst() or timedelta(0)
        if abs(dst_delta) > abs(largest_delta):
            largest_delta = dst_delta
        current_date += timedelta(days=1)

    return largest_delta


def format_utc_offset(offset_minutes):
    """Format an offset in minutes as UTC+HH:MM or UTC-HH:MM."""
    sign = "+" if offset_minutes >= 0 else "-"
    absolute_minutes = abs(offset_minutes)
    hours, minutes = divmod(absolute_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def format_daylight_records(records):
    """Group daylight-saving metadata sources by file for display."""
    fields_by_file = {}
    for filename, source_name, state in records:
        state_text = "ON" if state else "OFF"
        item = f"{source_name}={state_text}"
        fields_by_file.setdefault(filename, [])
        if item not in fields_by_file[filename]:
            fields_by_file[filename].append(item)

    return ", ".join(
        f"{filename.name} ({', '.join(items)})"
        for filename, items in fields_by_file.items()
    )


def format_timezone_records(records):
    """Group timezone-context metadata by file for display."""
    fields_by_file = {}
    for filename, source_name, raw_value, _ in records:
        item = f"{source_name}={raw_value}"
        fields_by_file.setdefault(filename, [])
        if item not in fields_by_file[filename]:
            fields_by_file[filename].append(item)

    return ", ".join(
        f"{filename.name} ({', '.join(items)})"
        for filename, items in fields_by_file.items()
    )


def review_daylight_savings(
    selected_file_date,
    selected_date_option,
    daylight_records,
    timezone_records,
    timezone_name,
    classification_timezone,
):
    """
    Offer a one-hour-style correction when camera DST metadata contradicts the
    selected timezone's rule for the selected local date.

    The original files are never rewritten. A correction changes only the date
    used to classify copies. Explicit timestamp offsets are shown as evidence;
    when one matches the expected timezone offset, keeping the wall-clock time
    is recommended because only the maker-note flag may be stale.
    """
    if selected_file_date is None or not daylight_records:
        return selected_file_date

    selected_datetime = selected_file_date[1]
    valid_states = timezone_states_for_local_time(
        selected_datetime,
        classification_timezone,
    )
    recorded_states = {state for _, _, state in daylight_records}

    if not valid_states:
        expected_state = None
    else:
        valid_dst_states = {state[0] for state in valid_states}
        if recorded_states and recorded_states.issubset(valid_dst_states):
            # Every camera setting is valid for this local time. This also
            # handles the repeated autumn hour, where both DST states may be
            # possible.
            return selected_file_date
        expected_state = (
            next(iter(valid_dst_states))
            if len(valid_dst_states) == 1
            else None
        )

    print()
    print("Daylight-saving review for this related group:")
    print(f"  Timezone: {timezone_name}")
    print(
        "  Selected local time: "
        f"{selected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(f"  Camera metadata: {format_daylight_records(daylight_records)}")

    explicit_offsets = set(selected_date_option.get("offsets", set()))
    explicit_offsets.update(
        offset_minutes
        for _, source_name, _, offset_minutes in timezone_records
        if source_name.split(":")[-1] in TIMESTAMP_OFFSET_TAGS
        and offset_minutes is not None
    )
    explicit_offsets = sorted(explicit_offsets)

    if timezone_records:
        print(
            "  Timezone metadata: "
            + format_timezone_records(timezone_records)
        )

    if explicit_offsets:
        print(
            "  Explicit timestamp offsets: "
            + ", ".join(format_utc_offset(value) for value in explicit_offsets)
        )

    if not valid_states:
        print(
            "  This local wall-clock time falls inside a daylight-saving "
            "transition gap and does not exist in the selected timezone."
        )
        correction_delta = daylight_saving_delta_for_year(
            classification_timezone,
            selected_datetime.year,
        )
        if correction_delta == timedelta(0):
            return selected_file_date
        corrected_datetime = selected_datetime + correction_delta
    elif expected_state is None or len(recorded_states) != 1:
        print(
            "  The timezone rule or camera metadata is ambiguous, so no "
            "single automatic correction can be inferred."
        )
        return selected_file_date
    else:
        expected_dst, expected_offset, timezone_abbreviation = valid_states[0]
        recorded_dst = next(iter(recorded_states))
        print(
            "  Expected setting: "
            f"{'ON' if expected_dst else 'OFF'} "
            f"({timezone_abbreviation}, {format_utc_offset(expected_offset)})"
        )

        daylight_delta = daylight_saving_delta_for_year(
            classification_timezone,
            selected_datetime.year,
        )
        if daylight_delta == timedelta(0):
            return selected_file_date

        correction_delta = (
            daylight_delta  if expected_dst and not recorded_dst
            else -daylight_delta
        )
        corrected_datetime = selected_datetime + correction_delta

        if expected_offset in explicit_offsets:
            print(
                "  At least one embedded timestamp already carries the "
                "expected UTC offset. Keeping the time is recommended; the "
                "maker-note daylight-saving flag may be stale."
            )
        else:
            direction = "behind" if correction_delta > timedelta(0) else "ahead"
            print(
                f"  If the camer applied its recorded setting, its clock may "
                f"be {abs(correction_delta.total_seconds()) / 3600:g} hour(s) "
                f"{direction}."
            )

    print("Choose how this group should be classified:")
    print(
        "  1. Keep "
        f"{selected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(
        "  2. Correct to "
        f"{corrected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    while True:
        raw_selection = input("Enter 1 or 2: ").strip()
        if raw_selection == "1":
            print()
            return selected_file_date
        if raw_selection == "2":
            print(
                "Selected daylight-saving correction: "
                f"{corrected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            print()
            return (2, corrected_datetime)
        print("Invalid selection. Enter 1 or 2.")


def get_dates(metadata_reader, filename):
    """
    Return dates, review reason, image flag, DST records, and timezone records.

    The first pass asks ExifTool for Time:All while excluding filesystem
    pseudo-tags and the specifically non-creation timestamps listed in
    EXIFTOOL_READ_PARAMS. Complete embedded timestamps from EXIF, XMP, IPTC,
    QuickTime, maker notes, composites, and other supported metadata groups
    become candidates for the existing numbered conflict prompt.

    This function never falls back to a file timestamp. The caller first checks
    the complete related group. Only when no embedded candidate exists anywhere
    in that group may it request FileModifyDate from a base image file.
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
            )

        # Sidecars and other related files may legitimately contain no ExifTool
        # metadata. They remain usable members of the group but provide no date.
        return [], None, False, [], []
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
            )

        return [], None, False, [], []

    metadata = metadata_results[0]
    file_is_image = is_image_file(filename, metadata)
    date_candidates = []
    daylight_records = []
    timezone_records = []
    invalid_date_messages = []

    for metadata_key, groups, tag_name, metadata_value in iter_metadata_values(
        metadata
    ):
        if tag_name == "DaylightSavings":
            daylight_state = parse_daylight_savings_value(metadata_value)
            if daylight_state is not None:
                daylight_records.append(
                    (metadata_key, daylight_state)
                )
            continue

        if tag_name in TIME_CONTEXT_TAGS:
            timezone_records.append(
                (
                    metadata_key,
                    str(metadata_value).strip(),
                    parse_metadata_offset_value(metadata_value)
                    if tag_name in TIMESTAMP_OFFSET_TAGS
                    else None,
                )
            )
            continue

        # These tags support file identification but are not dates.
        if tag_name in {"FileType", "MIMEType"}:
            continue

        # The ExifTool request already excludes the family-1 System group and
        # the semantic exclusions above. Retain both checks as safety invariants
        # in case a future ExifTool version returns an explicitly excluded tag.
        if "System" in groups or tag_name in EXCLUDED_TIME_TAGS:
            continue

        try:
            parsed_metadata_date = parse_complete_metadata_datetime(
                metadata_value
            )
        except ValueError:
            # A value with the complete date/time shape but invalid calendar
            # data remains a review case. Partial date/time values return None
            # and are simply not standalone candidates.
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
            daylight_records,
            timezone_records,
        )

    return (
        date_candidates,
        None,
        file_is_image,
        daylight_records,
        timezone_records,
    )


def get_file_modify_date(metadata_reader, filename):
    """
    Return the ExifTool FileModifyDate candidate for one base image file.

    FileModifyDate is requested separately so FileCreateDate, FileAccessDate,
    and other System pseudo-tags are never read for fallback selection.
    """
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

    metadata = metadata_results[0]
    modification_date_text = get_first_metadata_value(
        metadata,
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
    """
    Format one timestamp's sources with each filename displayed only once.

    Repeated list values and duplicate tag instances are also collapsed so the
    prompt shows a concise list of unique fields for each file.
    """
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
    """
    A derivative must begin with the complete base stem. Its first added
    character may be anything except an ASCII digit.

    IMG_0001pano and IMG_0001-edit match IMG_0001.
    IMG_00010 does not match IMG_0001.
    """
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
    """
    Group exact stems, then attach derivative stems to the longest matching
    shorter stem. The input is a fixed snapshot containing regular files only.
    """
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
            groups[max(matching_bases, key=len)].extend(
                files_by_stem[stem]
            )
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
    """
    Copy without overwriting. Identical existing files are duplicates; different
    files cause _1, _2, ... to be inserted before the extension.

    Return (actual_destination, copied, renamed).
    """
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
            if files_are_binary_identical(
                source_file,
                destination_file,
            ):
                return destination_file, False, suffix_number > 0
            suffix_number += 1
            continue

        destination_created = False
        try:
            with source_file.open("rb") as source_stream:
                # Exclusive mode is the final protection against overwriting.
                with destination_file.open("xb") as destination_stream:
                    destination_created = True
                    shutil.copyfileobj(
                        source_stream,
                        destination_stream,
                        length=COPY_CHUNK_SIZE,
                    )
            shutil.copystat(source_file, destination_file)

        except FileExistsError:
            # Another process used this name after the exists() check.
            continue

        except OSError:
            # Remove an incomplete output copy; the source is never modified.
            if destination_created:
                try:
                    destination_file.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        return destination_file, True, suffix_number > 0


# Request the source directory in the same interactive style as the original.
# The script deliberately examines only regular files located directly inside
# this directory. It never walks into existing subdirectories.
directory = clean_input_path(
    input("Please write (or drag) the source directory path: ")
)

if not directory.exists() or not directory.is_dir():
    print(f"Error: source directory is invalid: '{directory}'")
    input("Press Enter to exit")
    raise SystemExit(1)

directory = directory.resolve()
timezone_name, classification_timezone = request_classification_timezone()

# Create the two output locations inside the selected directory:
#
#   classified/
#       YYYY-MM-DD/
#           successfully classified copies
#
#   unclassified/
#       damaged or otherwise unreliable images requiring manual review
#
# These directories are not included in processing because the input snapshot
# below contains only files directly in the selected directory. No recursive
# directory traversal is performed.
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

    # Snapshot regular, non-symlink files once. Using iterdir() here is
    # intentionally non-recursive: files inside classified, unclassified, or
    # any other subdirectory are never examined.
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

# Start one persistent ExifTool process for the complete run. PyExifTool keeps
# this process open, so reading each file does not launch a new executable.
try:
    metadata_reader = ExifToolHelper(
        common_args=["-G0:1:4"],
    )
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

# Follow the original processing flow: determine one preferred date for each
# related group, then copy the files to that group's date directory.
for base_stem, same_stem_files in same_stem_groups:
    file_dates = {}
    usable_files = set()
    base_image_files = []
    daylight_records_by_file = {}
    timezone_records_by_file = {}
    review_reasons = {}
    selected_file_date = None
    selected_date_option = None

    # First pass: collect only embedded metadata timestamps from every related
    # file. Sidecars may contribute embedded XMP/IPTC metadata, but no file in
    # this pass contributes filesystem timestamps.
    for same_stem_file in same_stem_files:
        try:
            (
                date_candidates,
                review_reason,
                file_is_image,
                daylight_records,
                timezone_records,
            ) = get_dates(
                metadata_reader,
                same_stem_file,
            )
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

        if daylight_records:
            daylight_records_by_file[same_stem_file] = daylight_records
        if timezone_records:
            timezone_records_by_file[same_stem_file] = timezone_records

        if date_candidates:
            file_dates[same_stem_file] = date_candidates

        # A fallback source must be an image whose stem is exactly the base stem
        # chosen by group_related_files(). This excludes sidecars and suffix-
        # derived images such as IMG_0001-edit.jpg.
        if (
            file_is_image
            and same_stem_file.stem.casefold() == base_stem.casefold()
        ):
            base_image_files.append(same_stem_file)

    # Second pass: only when the complete related group has no embedded
    # timestamp, request FileModifyDate from the base image file(s). The request
    # names only FileModifyDate, so create/access dates are never extracted.
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

    # If neither embedded metadata nor a base-image FileModifyDate is available,
    # the group has no defensible classification date.
    if not file_dates:
        for same_stem_file in usable_files:
            review_reasons[same_stem_file] = (
                "no embedded timestamp and no base image FileModifyDate"
            )

    # Group identical timestamp values together, whether they came from
    # separate files, separate metadata fields, or duplicate tag instances.
    # The complete source paths are retained for the numbered conflict list.
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
                {"date_type": date_type, "sources": [], "offsets": set()},
            )
            date_option["date_type"] = max(
                date_option["date_type"],
                date_type,
            )
            date_option["sources"].append(
                (same_stem_file, source_name)
            )
            if timezone_offset_minutes is not None:
                date_option["offsets"].add(timezone_offset_minutes)

    if len(date_options) == 1:
        # Every available field agrees on one timestamp, so no user interaction
        # is required. Use the strongest source associated with that timestamp.
        date_value, date_option = next(iter(date_options.items()))
        selected_date_option = date_option
        selected_file_date = (
            date_option["date_type"],
            date_value,
        )

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
            date_sources = format_date_sources(
                date_option["sources"]
            )
            print(
                f"  {option_number}. "
                f"{date_value.strftime('%Y-%m-%d %H:%M:%S')} "
                f"- {date_sources}"
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

            selected_date_value, selected_date_option = sorted_date_options[
                selected_option_number - 1
            ]
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
        base_image_daylight_records = []
        base_image_timezone_records = []
        for base_image_file in base_image_files:
            for source_name, state in daylight_records_by_file.get(
                base_image_file,
                [],
            ):
                base_image_daylight_records.append(
                    (base_image_file, source_name, state)
                )
            for (
                source_name,
                raw_value,
                offset_minutes,
            ) in timezone_records_by_file.get(base_image_file, []):
                base_image_timezone_records.append(
                    (
                        base_image_file,
                        source_name,
                        raw_value,
                        offset_minutes,
                    )
                )

        selected_file_date = review_daylight_savings(
            selected_file_date,
            selected_date_option,
            base_image_daylight_records,
            base_image_timezone_records,
            timezone_name,
            classification_timezone,
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
            folder_name = (
                f"{CLASSIFIED_FOLDER_NAME}/{date_folder_name}"
            )
            destination_directory = (
                classified_directory / date_folder_name
            )
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

        try:
            destination_directory.mkdir(parents=True, exist_ok=True)
            destination_file, copied, renamed = copy_file_safely(
                same_stem_file,
                destination_directory / same_stem_file.name,
            )
        except OSError as error:
            print(
                f"Error copying '{same_stem_file.name}': {error}",
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

        if same_stem_file in review_reasons:
            review_count += 1
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

# Stop the persistent ExifTool subprocess before printing the final summary.
metadata_reader.terminate()

print()
print(f"Classified directory: {classified_directory}")
print(f"Unclassified directory: {unclassified_directory}")
print(f"Copied files: {copied_count}")
print(f"Binary duplicates: {duplicate_count}")
print(f"Renamed collision copies: {renamed_count}")
print(f"Files sent for review: {review_count}")
print(f"Failed files: {failed_count}")
print("Original source files were not modified.")

input("Press Enter to exit")

if failed_count:
    raise SystemExit(2)
