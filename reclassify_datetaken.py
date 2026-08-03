"""
Classify related media files by their best-supported capture date.

The source directory is treated as immutable: originals are never written,
renamed, moved, or used as metadata-correction targets. Exact-stem files and
recognized derivative stems are processed together so media, derived media,
and sidecars share one capture-time decision.

One persistent ExifTool process reads complete embedded timestamps, calculated
Composite timestamps, timezone/DST evidence, and the narrowly scoped filesystem
``FileModifyDate`` candidate. Complete Composite values may participate in the
capture-time consensus, but calculated/read-only fields are never written.
Existing writable complete, date-only, and time-only fields are normalized in a
newly created copy so ExifTool can recalculate its Composite values.

After the final local capture time and timezone state are established, files are
copied into ``classified/YYYY-MM-DD`` or ``unclassified``. Only newly created
classified copies are corrected. Existing outputs are compared only after the
new copy has reached its final corrected state.
"""

import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from exiftool import ExifToolHelper
from exiftool.exceptions import ExifToolException, ExifToolExecuteError

# ============================================================================
# CONFIGURATION AND METADATA POLICY
# ============================================================================
#
# These constants form one central policy for both reading and writing. A field
# excluded from capture-time consensus must also be excluded from correction;
# otherwise an editing or device timestamp could be shifted as if it described
# the original exposure/recording time.

DATETYPE = {
    0: "OS_DATE",
    1: "METADATA",
    2: "METADATA_TIMEZONE_CORRECTED",
}

DEFAULT_TIMEZONE_NAME = "Europe/Berlin"
CLASSIFIED_FOLDER_NAME = "classified"
UNCLASSIFIED_FOLDER_NAME = "unclassified"
COPY_CHUNK_SIZE = 1024 * 1024

# Candidate timestamps are considered one consensus option when the complete
# earliest-to-latest span does not exceed this threshold. The value is kept as
# seconds so the tolerance can be adjusted without changing consensus logic.
CAPTURE_TIME_CONFLICT_THRESHOLD_SECONDS = 60

# ``Time:All`` discovers standard EXIF, maker-note, XMP, IPTC,
# QuickTime, and calculated Composite fields without camera make/model
# dispatch. The extra tags are requested by meaning rather than by brand, so
# metadata from unknown or future cameras follows the same path as metadata
# from currently recognized makers. ExifTool System timestamps are excluded
# from this broad read; ``FileModifyDate`` is requested separately so only
# exact-primary-stem image/video files can contribute it to consensus.
EXIFTOOL_TAGS = [
    "Time:All",
    "File:FileType",
    "File:MIMEType",
    # Generic camera daylight-saving configuration used by several makers.
    "DaylightSavings",
    # Pentax/Ricoh travel profiles. WorldTimeLocation selects the active
    # Hometown or Destination profile; no Make/Model dispatch is required.
    "WorldTimeLocation",
    "HometownDST",
    "DestinationDST",
    "HometownCity",
    "DestinationCity",
    # Standard and maker-note UTC-offset context.
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
    "TimeZoneOffset",
    "TimeZone",
    "TimeZoneCity",
    "TimeOffset",
    # UTC counterparts exposed by Olympus/OM System, GPS-enabled cameras,
    # phones, GoPro, DJI, and any other file for which ExifTool provides them.
    "DateTimeUTC",
    "GPSDateTime",
]
EXIFTOOL_READ_PARAMS = ["-a", "-ee", "-x", "1System:All"]
FILE_MODIFY_DATE_TAGS = ["FileModifyDate"]
FILE_MODIFY_DATE_PARAMS = []

# These fields describe editing, metadata history, device operation,
# runtime, profile resources, or recording end points rather than original
# capture. They remain untouched: they are neither consensus candidates nor
# correction targets. Capture-oriented local fields such as SonyDateTime,
# PanasonicDateTime, RicohDate, and ordinary EXIF/XMP creation dates remain
# eligible. UTC-reference tags are handled separately because they are offset
# evidence, not local wall-clock choices.
EXCLUDED_CAPTURE_TIME_TAGS = {
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
}


# Correction is driven by timestamp semantics rather than camera make. Every
# eligible local wall-clock field is normalized to the final consensus; an
# existing inline or separate offset is normalized to the final timezone state,
# while UTC-reference timestamps remain unchanged. The write pass never creates
# missing date/time or offset fields.
#
# Standard associated offsets belong to particular local timestamps and may
# also exist in writable sidecars. DateTimeOriginal uses OffsetTimeOriginal and
# CreateDate uses OffsetTimeDigitized. ModifyDate/OffsetTime are intentionally
# absent because modification metadata is excluded from capture correction.
ASSOCIATED_OFFSET_TAGS = {
    "DateTimeOriginal": "OffsetTimeOriginal",
    "CreateDate": "OffsetTimeDigitized",
}

# Camera-global timezone/DST configuration has narrower write scope than
# timestamp-associated offsets: it is normalized only in primary or derivative
# image/video files, never in sidecars. Selector-driven profiles are represented
# declaratively so review and correction use the same relationship data.
DIRECT_DST_TAGS = {"DaylightSavings"}
TIMEZONE_PROFILE_POLICIES = (
    {
        "selector_tag": "WorldTimeLocation",
        "profiles": (
            {
                "name": "Hometown",
                "selector_values": ("0", "home", "hometown"),
                "dst_tag": "HometownDST",
                "context_tags": ("HometownCity",),
            },
            {
                "name": "Destination",
                "selector_values": ("1", "destination", "travel"),
                "dst_tag": "DestinationDST",
                "context_tags": ("DestinationCity",),
            },
        ),
    },
)
PROFILE_SELECTOR_TAGS = {policy["selector_tag"] for policy in TIMEZONE_PROFILE_POLICIES}
PROFILE_DST_TAGS = {
    profile["dst_tag"]
    for policy in TIMEZONE_PROFILE_POLICIES
    for profile in policy["profiles"]
}
PROFILE_CONTEXT_TAGS = {
    tag_name
    for policy in TIMEZONE_PROFILE_POLICIES
    for profile in policy["profiles"]
    for tag_name in profile["context_tags"]
}
ASSOCIATED_OFFSET_FIELDS = set(ASSOCIATED_OFFSET_TAGS.values())
GLOBAL_OFFSET_TAGS = {"TimeZoneOffset", "TimeZone", "TimeOffset"}
OFFSET_CONTEXT_TAGS = ASSOCIATED_OFFSET_FIELDS | GLOBAL_OFFSET_TAGS
DISPLAY_CONTEXT_TAGS = {"TimeZoneCity"} | PROFILE_SELECTOR_TAGS | PROFILE_CONTEXT_TAGS
TIME_CONTEXT_TAGS = (
    DIRECT_DST_TAGS | PROFILE_DST_TAGS | OFFSET_CONTEXT_TAGS | DISPLAY_CONTEXT_TAGS
)
UTC_REFERENCE_TAGS = {
    "DateTimeUTC",
    "GPSDateTime",
    "GPSDateStamp",
    "GPSTimeStamp",
    "UTCDateTime",
}

# ``-wm w`` updates existing metadata only. This prevents the correction
# pass from inventing fields absent from the original representation. Absolute
# final values are written rather than relative deltas, making every operation
# independently verifiable. ``-P`` preserves the host timestamp during embedded
# writes; FileModifyDate is set explicitly afterward only when the copied file
# is image/video capture media.
CORRECTION_WRITE_PARAMS = [
    "-overwrite_original_in_place",
    "-P",
    "-wm",
    "w",
]

COMPLETE_DATE_TIME_PATTERN = re.compile(
    r"^(?P<year>\d{4})[:-](?P<month>\d{2})[:-](?P<day>\d{2})[ T]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<timezone>Z|[+-]\d{2}:?\d{2})?$"
)
DATE_ONLY_PATTERN = re.compile(
    r"^(?P<year>\d{4})[:-](?P<month>\d{2})[:-](?P<day>\d{2})$"
)
TIME_ONLY_PATTERN = re.compile(
    r"^(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<timezone>Z|[+-]\d{2}:?\d{2})?$"
)
SIGNED_OFFSET_PATTERN = re.compile(
    r"(?<!\d)(?:UTC|GMT)?\s*(?P<sign>[+-])"
    r"(?P<hour>\d{1,2})(?::?(?P<minute>\d{2}))?(?!\d)",
    re.IGNORECASE,
)

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


@dataclass(frozen=True)
class ParsedMetadataDateTime:
    """
    Canonical components parsed from one metadata date/time representation.

    A missing date or time remains ``None``. Incomplete values never acquire an
    invented component and therefore cannot accidentally become complete
    capture-time candidates.
    """

    local_date: date | None
    local_time: datetime_time | None
    utc_offset_minutes: int | None

    @property
    def local_datetime(self):
        """Return a complete local datetime only when both components exist."""
        if self.local_date is None or self.local_time is None:
            return None
        return datetime.combine(self.local_date, self.local_time)


# ============================================================================
# INPUT, EXIFTOOL VALUE NORMALIZATION, AND DATE/TIME PARSING
# ============================================================================
#
# This section is the boundary between ExifTool's heterogeneous output and the
# canonical values used by the classifier. Raw syntax is retained for later
# formatting, while local wall-clock components and UTC offsets remain separate.
# Parsing never silently converts a local timestamp into another timezone.


def iterate_exiftool_values(metadata):
    """
    Yield every non-null scalar/list item with its ExifTool group path.

    ``-a`` and embedded extraction can return duplicate tags and list values.
    Flattening them here retains family-zero/family-one group information needed
    for explicit write targets while keeping later policy loops uniform.
    """
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


def parse_utc_offset_minutes(timezone_text):
    """Parse Z, +HH:MM, -HH:MM, +HHMM, or -HHMM into integer minutes."""
    if not timezone_text:
        return None
    if timezone_text == "Z":
        return 0

    sign = 1 if timezone_text[0] == "+" else -1
    timezone_hour = int(timezone_text[1:3])
    timezone_minute = int(timezone_text[-2:])
    if timezone_hour > 23 or timezone_minute > 59:
        raise ValueError(f"invalid UTC offset {timezone_text!r}")
    return sign * (timezone_hour * 60 + timezone_minute)


def parse_metadata_offset_minutes(tag_name, metadata_value):
    """
    Parse a standalone metadata offset only when its representation is safe.

    Numeric ``TimeZoneOffset`` is standardized as hours. Other context and
    maker-note fields are accepted only when their text carries an explicit
    sign; unsigned numeric values are ignored because their units/sign
    conventions may be undocumented.
    """
    if tag_name == "TimeZoneOffset" and isinstance(metadata_value, (int, float)):
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


def parse_metadata_datetime(date_value, time_value=None):
    """
    Parse complete, date-only, time-only, or paired metadata date/time values.

    ``None`` means the supplied representation is not recognized. Recognized
    but impossible calendar/clock values raise ``ValueError``. Missing date or
    time components remain ``None`` and are never invented. An explicit offset
    is returned beside the naive local wall clock and is not applied to it,
    because folder classification uses the camera's local capture date.

    The optional paired form remains a parsing utility, but consensus does not
    synthesize IPTC pairs. It relies on ExifTool's complete Composite timestamp
    and later updates whichever writable components actually exist.
    """
    if time_value is not None:
        parsed_date = parse_metadata_datetime(date_value)
        parsed_time = parse_metadata_datetime(time_value)
        if parsed_date is None or parsed_time is None:
            return None
        if parsed_date.local_date is None or parsed_time.local_time is None:
            return None
        return ParsedMetadataDateTime(
            parsed_date.local_date,
            parsed_time.local_time,
            parsed_time.utc_offset_minutes,
        )

    value_text = str(date_value).strip()

    complete_match = COMPLETE_DATE_TIME_PATTERN.fullmatch(value_text)
    if complete_match is not None:
        parsed_date = date(
            int(complete_match.group("year")),
            int(complete_match.group("month")),
            int(complete_match.group("day")),
        )
        parsed_time = datetime_time(
            int(complete_match.group("hour")),
            int(complete_match.group("minute")),
            int(complete_match.group("second")),
        )
        return ParsedMetadataDateTime(
            parsed_date,
            parsed_time,
            parse_utc_offset_minutes(complete_match.group("timezone")),
        )

    date_match = DATE_ONLY_PATTERN.fullmatch(value_text)
    if date_match is not None:
        return ParsedMetadataDateTime(
            date(
                int(date_match.group("year")),
                int(date_match.group("month")),
                int(date_match.group("day")),
            ),
            None,
            None,
        )

    time_match = TIME_ONLY_PATTERN.fullmatch(value_text)
    if time_match is not None:
        return ParsedMetadataDateTime(
            None,
            datetime_time(
                int(time_match.group("hour")),
                int(time_match.group("minute")),
                int(time_match.group("second")),
            ),
            parse_utc_offset_minutes(time_match.group("timezone")),
        )

    return None


def parse_daylight_saving_value(metadata_value):
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


def resolve_active_timezone_profiles(values_by_tag):
    """
    Resolve unambiguous selector-driven timezone profiles from policy data.

    The returned tuples contain selector tag, active profile name, active DST tag,
    and profile context tags. Unknown or contradictory selector values are ignored
    rather than guessed. This shared resolver is used by both metadata review and
    metadata correction.
    """
    active_profiles = []
    for policy in TIMEZONE_PROFILE_POLICIES:
        recognized_profile_names = set()
        for selector_record in values_by_tag.get(policy["selector_tag"], []):
            raw_selector_value = selector_record[-1]
            if isinstance(raw_selector_value, (int, float)):
                numeric_value = float(raw_selector_value)
                normalized_value = (
                    str(int(numeric_value))
                    if numeric_value.is_integer()
                    else str(numeric_value)
                )
            else:
                normalized_value = str(raw_selector_value).strip().casefold()

            for profile in policy["profiles"]:
                if normalized_value in profile["selector_values"]:
                    recognized_profile_names.add(profile["name"])

        if len(recognized_profile_names) != 1:
            continue

        active_name = next(iter(recognized_profile_names))
        active_profile = next(
            profile for profile in policy["profiles"] if profile["name"] == active_name
        )
        active_profiles.append(
            (
                policy["selector_tag"],
                active_profile["name"],
                active_profile["dst_tag"],
                active_profile["context_tags"],
            )
        )

    return active_profiles


def format_offset(offset_minutes):
    """
    Format integer offset minutes as the metadata form +HH:MM or -HH:MM.

    User-facing callers prepend ``UTC`` themselves; embedded EXIF/XMP offset
    fields store only the signed numeric portion.
    """
    sign = "+" if offset_minutes >= 0 else "-"
    absolute_minutes = abs(offset_minutes)
    hours, minutes = divmod(absolute_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def format_complete_datetime(original_value, local_datetime, offset_minutes=None):
    """Preserve a complete timestamp's syntax while replacing its value."""
    original_text = str(original_value).strip()
    date_match = COMPLETE_DATE_TIME_PATTERN.fullmatch(original_text)
    if date_match is None:
        raise ValueError(f"not a complete timestamp: {original_value!r}")

    date_separator = original_text[4]
    datetime_separator = original_text[10]
    formatted = (
        f"{local_datetime.year:04d}{date_separator}"
        f"{local_datetime.month:02d}{date_separator}"
        f"{local_datetime.day:02d}{datetime_separator}"
        f"{local_datetime.hour:02d}:{local_datetime.minute:02d}:"
        f"{local_datetime.second:02d}"
    )
    # Fractional precision is intentionally field-local. Threshold consensus
    # compares whole seconds, while correction preserves each existing suffix.
    fraction = date_match.group("fraction")
    if fraction is not None:
        formatted += f".{fraction}"

    timezone_text = date_match.group("timezone")
    if offset_minutes is not None:
        timezone_text = format_offset(offset_minutes)
    if timezone_text is not None:
        formatted += timezone_text
    return formatted


def format_date_only(original_value, local_datetime):
    """Preserve a date-only value's separator while replacing its date."""
    original_text = str(original_value).strip()
    if DATE_ONLY_PATTERN.fullmatch(original_text) is None:
        raise ValueError(f"not a date-only value: {original_value!r}")
    separator = original_text[4]
    return (
        f"{local_datetime.year:04d}{separator}"
        f"{local_datetime.month:02d}{separator}"
        f"{local_datetime.day:02d}"
    )


def format_time_only(original_value, local_datetime, offset_minutes=None):
    """Preserve fractional precision while replacing a time-only value."""
    original_text = str(original_value).strip()
    time_match = TIME_ONLY_PATTERN.fullmatch(original_text)
    if time_match is None:
        raise ValueError(f"not a time-only value: {original_value!r}")

    formatted = (
        f"{local_datetime.hour:02d}:{local_datetime.minute:02d}:"
        f"{local_datetime.second:02d}"
    )
    # As with complete timestamps, preserve the source field's own fraction.
    fraction = time_match.group("fraction")
    if fraction is not None:
        formatted += f".{fraction}"

    timezone_text = time_match.group("timezone")
    if offset_minutes is not None:
        timezone_text = format_offset(offset_minutes)
    if timezone_text is not None:
        formatted += timezone_text
    return formatted


def get_metadata_write_target(groups, tag_name, raw=False):
    """
    Map an ExifTool ``-G0:1:4`` path to an explicit writable target.

    System, File, and Composite groups are not direct metadata write targets.
    Duplicate ``Copy*`` labels are ignored when selecting the family-one group.
    The raw ``#`` suffix is used only when a maker/numeric encoding must be
    preserved instead of interpreted by ExifTool.
    """
    if not groups or "System" in groups or groups[0] in {"Composite", "File"}:
        return None

    family_zero = groups[0]
    family_one = None
    for group_name in groups[1:]:
        if not group_name.casefold().startswith("copy"):
            family_one = group_name
            break

    target_group = family_one or family_zero
    return f"{target_group}:{tag_name}{'#' if raw else ''}"


# ============================================================================
# TIMEZONE AND DAYLIGHT-SAVING RULES
# ============================================================================
#
# Local timestamps remain naive until evaluated against the selected IANA zone.
# This section converts inline offsets, standalone offsets, UTC counterparts,
# and camera configuration into generic evidence, then keeps the user-facing
# timezone/DST decision in one place. Civil-time rules preserve repeated autumn
# times (two valid folds), reject nonexistent spring-forward times (no valid
# state), and avoid guessing when a wall clock cannot map to one absolute instant.


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
    """
    Return all valid (DST, UTC-offset-minutes, abbreviation) local-time states.

    Both ``fold`` values preserve the two interpretations of a repeated autumn
    time. A UTC round trip rejects a spring-forward wall-clock value that never
    existed in the selected timezone.
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


def derive_utc_offset_from_timestamp_pair(local_datetime, utc_datetime):
    """
    Derive a plausible civil offset from matching local and UTC timestamps.

    Day adjustments handle midnight crossings. Whole-minute rounding tolerates
    up to five seconds because telemetry and exposure clocks may not be sampled
    simultaneously. Results are limited to civil offsets UTC-12 through UTC+14.
    """
    raw_seconds = (local_datetime - utc_datetime).total_seconds()
    for day_adjustment in (0, -1, 1, -2, 2):
        adjusted_seconds = raw_seconds + day_adjustment * 86400
        rounded_minutes = int(round(adjusted_seconds / 60))
        residual_seconds = abs(adjusted_seconds - rounded_minutes * 60)
        if -12 * 60 <= rounded_minutes <= 14 * 60 and residual_seconds <= 5:
            return rounded_minutes
    return None


def resolve_unambiguous_timezone_state(local_datetime, classification_timezone):
    """Return the sole valid timezone state for a local time, or None."""
    states = timezone_states_for_local_time(local_datetime, classification_timezone)
    return states[0] if len(states) == 1 else None


def format_labels_by_file(records):
    """Format evidence labels while showing each filename only once."""
    labels_by_file = {}
    for filename, label in records:
        labels = labels_by_file.setdefault(filename, [])
        if label not in labels:
            labels.append(label)
    return ", ".join(
        f"{filename.name} ({', '.join(labels)})"
        for filename, labels in labels_by_file.items()
    )


def build_automatic_correction_key(
    correction_delta,
    recorded_dst_states,
    expected_dst_states,
    observed_offsets,
    expected_offsets,
):
    """
    Build a conservative signature for reusing one approved correction.

    Reuse requires the same delta and the complete recorded/expected DST and
    offset evidence. Similar-looking cases with different evidence still prompt.
    """
    return (
        int(correction_delta.total_seconds()),
        tuple(sorted(recorded_dst_states)),
        tuple(sorted(expected_dst_states)),
        tuple(sorted(observed_offsets)),
        tuple(sorted(expected_offsets)),
    )


# ============================================================================
# RELATED-FILE DISCOVERY AND LOW-LEVEL FILE COMPARISON
# ============================================================================
#
# Exact stems form possible bases. Longer non-numeric derivatives attach to the
# longest matching base so edits and sidecars remain with their capture. A digit
# immediately after the base starts a separate sequence rather than a derivative.


def group_related_files(files):
    """
    Cluster exact stems and attach derivatives to the longest matching stem.

    Suffixes introduced by ``_``, ``-``, or text may describe a derivative.
    ``IMG1`` is not attached to ``IMG`` because numeric continuations commonly
    identify a different capture.
    """
    files_by_stem = {}
    for filename in files:
        files_by_stem.setdefault(filename.stem, []).append(filename)

    related_sets = {}
    sorted_stems = sorted(
        files_by_stem,
        key=lambda stem: (len(stem), stem.casefold()),
    )
    for stem in sorted_stems:
        folded_stem = stem.casefold()
        matching_primary_stems = []
        for primary_stem in related_sets:
            folded_primary_stem = primary_stem.casefold()
            if len(folded_stem) <= len(folded_primary_stem):
                continue
            if not folded_stem.startswith(folded_primary_stem):
                continue
            first_added_character = folded_stem[len(folded_primary_stem)]
            if "0" <= first_added_character <= "9":
                continue
            matching_primary_stems.append(primary_stem)

        if matching_primary_stems:
            related_sets[max(matching_primary_stems, key=len)].extend(
                files_by_stem[stem]
            )
        else:
            related_sets[stem] = list(files_by_stem[stem])

    return [
        (
            primary_stem,
            sorted(related_files, key=lambda path: path.name.casefold()),
        )
        for primary_stem, related_files in sorted(
            related_sets.items(),
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


# ============================================================================
# MAJOR OPERATION 1: READ METADATA FROM RELATED FILES
# ============================================================================


def read_related_files_metadata(metadata_reader, primary_stem, related_files):
    """
    Read and classify all metadata needed for one related source-file set.

    Complete eligible values—including ExifTool Composite timestamps—become
    consensus candidates. UTC counterparts and timezone/DST configuration are
    routed to evidence collections. Date-only and time-only values are not
    combined into candidates; the raw records are retained so existing writable
    components can later follow the final consensus.

    ``FileModifyDate`` is read from the start only for exact-primary-stem image
    and video files. Derivative media cannot influence consensus through their
    filesystem timestamp, although a derivative image/video copy is aligned
    later. Sidecars never supply or receive FileModifyDate correction.
    """
    related_files_metadata = {
        "files": {},
        "usable_files": set(),
        "review_reasons": {},
        "primary_capture_media": [],
        "failures": 0,
    }

    for source_file in related_files:
        extension = source_file.suffix.casefold()
        extension_is_image = extension in IMAGE_EXTENSIONS
        extension_is_video = extension in VIDEO_EXTENSIONS
        is_primary_stem = source_file.stem.casefold() == primary_stem.casefold()

        # Extension detection is an initial fallback so a recognizable media
        # file can still be routed to review after an ExifTool failure. MIME
        # metadata, when present, is incorporated below.
        file_record = {
            "records": [],
            "capture_candidates": [],
            "timezone_context": [],
            "utc_references": [],
            "is_image": extension_is_image,
            "is_video": extension_is_video,
            "is_capture_media": extension_is_image or extension_is_video,
        }

        # --------------------------------------------------------------------
        # READ EMBEDDED METADATA, EXCLUDING ALL SYSTEM TIMESTAMPS
        # --------------------------------------------------------------------
        try:
            metadata_results = metadata_reader.get_tags(
                files=source_file,
                tags=EXIFTOOL_TAGS,
                params=EXIFTOOL_READ_PARAMS,
            )
        except ExifToolExecuteError as error:
            if file_record["is_capture_media"]:
                error_message = (
                    str(error.stderr).strip() if error.stderr else str(error)
                )
                related_files_metadata["review_reasons"][
                    source_file
                ] = f"ExifTool could not inspect the media file ({error_message})"
                continue
            metadata_results = []
        except ExifToolException as error:
            print(
                f"Error reading '{source_file.name}': {error}",
                file=sys.stderr,
            )
            related_files_metadata["failures"] += 1
            continue

        if not metadata_results and file_record["is_capture_media"]:
            related_files_metadata["review_reasons"][
                source_file
            ] = "ExifTool returned no metadata for the media file"
            continue

        metadata = metadata_results[0] if metadata_results else {}
        mime_type = None
        invalid_capture_values = []
        for metadata_key, groups, tag_name, raw_value in iterate_exiftool_values(
            metadata
        ):
            file_record["records"].append((metadata_key, groups, tag_name, raw_value))

            if tag_name == "MIMEType":
                if mime_type is None and isinstance(raw_value, str):
                    mime_type = raw_value.casefold()
                continue
            if tag_name == "FileType":
                continue

            # UTC references never compete with local capture timestamps.
            # They are retained to derive/check an offset for the selected wall clock.
            if tag_name in UTC_REFERENCE_TAGS:
                try:
                    parsed_utc = parse_metadata_datetime(raw_value)
                except ValueError:
                    parsed_utc = None
                if parsed_utc is not None and parsed_utc.local_datetime is not None:
                    reference = (metadata_key, parsed_utc.local_datetime)
                    if reference not in file_record["utc_references"]:
                        file_record["utc_references"].append(reference)
                continue

            # Offset, city, profile, and DST fields describe context; they
            # are evidence rather than capture-time choices.
            if tag_name in TIME_CONTEXT_TAGS:
                context_record = (metadata_key, tag_name, raw_value)
                if context_record not in file_record["timezone_context"]:
                    file_record["timezone_context"].append(context_record)
                continue

            if "System" in groups or tag_name in EXCLUDED_CAPTURE_TIME_TAGS:
                continue

            try:
                parsed = parse_metadata_datetime(raw_value)
            except ValueError:
                invalid_capture_values.append(f"{metadata_key}={raw_value!r}")
                continue
            if parsed is None:
                continue

            # Only complete values become candidates. Complete Composite
            # timestamps are valid evidence but are marked read-only; their
            # existing writable source components are normalized later.
            if parsed.local_datetime is not None:
                file_record["capture_candidates"].append(
                    {
                        "date_type": 1,
                        "datetime": parsed.local_datetime,
                        "source": metadata_key,
                        "offset_minutes": parsed.utc_offset_minutes,
                        "kind": (
                            "composite"
                            if groups and groups[0] == "Composite"
                            else "complete"
                        ),
                    }
                )

        mime_is_image = mime_type is not None and mime_type.startswith("image/")
        mime_is_video = mime_type is not None and mime_type.startswith("video/")
        file_record["is_image"] = mime_is_image or extension_is_image
        file_record["is_video"] = mime_is_video or extension_is_video
        file_record["is_capture_media"] = (
            file_record["is_image"] or file_record["is_video"]
        )

        if invalid_capture_values and file_record["is_capture_media"]:
            related_files_metadata["review_reasons"][source_file] = (
                "invalid capture metadata date: " + "; ".join(invalid_capture_values)
            )
            continue

        # --------------------------------------------------------------------
        # READ THE NARROWLY SCOPED FILEMODIFYDATE CONSENSUS CANDIDATE
        #
        # This participates from the start instead of acting as a fallback, so a
        # mismatch with embedded metadata is visible. Only exact-primary-stem
        # image/video files may define consensus through FileModifyDate.
        #
        # ExifTool may display FileModifyDate with a UTC offset, but that offset
        # reflects filesystem/host interpretation rather than camera capture
        # metadata. FAT-to-NTFS copies are especially prone to such shifts. Keep
        # only the local wall-clock value and discard the filesystem offset here.
        # --------------------------------------------------------------------
        if is_primary_stem and file_record["is_capture_media"]:
            try:
                file_modify_results = metadata_reader.get_tags(
                    files=source_file,
                    tags=FILE_MODIFY_DATE_TAGS,
                    params=FILE_MODIFY_DATE_PARAMS,
                )
            except ExifToolException as error:
                print(
                    f"Warning: could not read FileModifyDate from "
                    f"'{source_file.name}': {error}",
                    file=sys.stderr,
                )
                file_modify_results = []

            modification_value = None
            if file_modify_results:
                modification_value = next(
                    (
                        raw_value
                        for _, _, tag_name, raw_value in iterate_exiftool_values(
                            file_modify_results[0]
                        )
                        if tag_name == "FileModifyDate"
                    ),
                    None,
                )
            if modification_value is not None:
                try:
                    parsed_modify = parse_metadata_datetime(modification_value)
                except ValueError as error:
                    print(
                        f"Warning: invalid FileModifyDate in '{source_file.name}': "
                        f"{modification_value!r} ({error})",
                        file=sys.stderr,
                    )
                else:
                    if (
                        parsed_modify is not None
                        and parsed_modify.local_datetime is not None
                    ):
                        file_record["capture_candidates"].append(
                            {
                                "date_type": 0,
                                "datetime": parsed_modify.local_datetime,
                                "source": "File:System:FileModifyDate",
                                "offset_minutes": None,
                                "kind": "file_modify",
                            }
                        )

        related_files_metadata["files"][source_file] = file_record
        related_files_metadata["usable_files"].add(source_file)
        if is_primary_stem and file_record["is_capture_media"]:
            related_files_metadata["primary_capture_media"].append(source_file)

    return related_files_metadata


# ============================================================================
# MAJOR OPERATION 2: DETERMINE THE CAPTURE-TIME CONSENSUS
# ============================================================================


def determine_capture_time_consensus(related_files, related_files_metadata):
    """
    Merge complete candidates and establish one authoritative capture time.

    Candidate values are grouped into threshold-bounded options. Every value in
    one option must fall within ``CAPTURE_TIME_CONFLICT_THRESHOLD_SECONDS`` of
    that option's earliest timestamp; comparing only adjacent values would allow
    a chain of small differences to hide a much larger overall disagreement.

    Each option retains all sources and explicit embedded offsets. Filesystem
    offsets are discarded: FileModifyDate may contribute its local wall-clock
    value, but it cannot act as camera timezone/DST evidence, alter remembered
    correction signatures, or disambiguate a DST fold.

    The exact representative is the existing timestamp supported by the most
    distinct sources, then by the most embedded-metadata sources, then the earliest
    value. One option is accepted silently; multiple options require a user choice.
    Incomplete components do not participate—ExifTool's complete Composite value
    represents known combined fields.
    """
    candidates_by_file = {}

    for source_file, file_record in related_files_metadata["files"].items():
        candidates = list(file_record["capture_candidates"])
        if candidates:
            candidates_by_file[source_file] = candidates

    # ------------------------------------------------------------------------
    # SORT ALL COMPLETE CANDIDATES AND BUILD THRESHOLD-BOUNDED CLUSTERS
    #
    # A new cluster begins only when the candidate is more than the configured
    # threshold after the current cluster's earliest value. The earliest-to-
    # latest span therefore remains bounded even when neighboring timestamps are
    # each close enough to form a misleading chain.
    # ------------------------------------------------------------------------
    sorted_candidate_records = sorted(
        (
            (candidate["datetime"], source_file, candidate)
            for source_file, candidates in candidates_by_file.items()
            for candidate in candidates
        ),
        key=lambda record: (
            record[0],
            record[1].name.casefold(),
            str(record[2]["source"]).casefold(),
        ),
    )

    consensus_clusters = []
    current_cluster = []
    current_cluster_start = None

    for candidate_record in sorted_candidate_records:
        candidate_datetime = candidate_record[0]
        if (
            current_cluster
            and (candidate_datetime - current_cluster_start).total_seconds()
            > CAPTURE_TIME_CONFLICT_THRESHOLD_SECONDS
        ):
            consensus_clusters.append(current_cluster)
            current_cluster = []
            current_cluster_start = None

        if not current_cluster:
            current_cluster_start = candidate_datetime
        current_cluster.append(candidate_record)

    if current_cluster:
        consensus_clusters.append(current_cluster)

    # ------------------------------------------------------------------------
    # BUILD ONE DISPLAY/DECISION OPTION FOR EACH THRESHOLD CLUSTER
    #
    # The representative must be a timestamp that actually exists in metadata.
    # Independent files count first; multiple synonymous or Composite paths from
    # one file do not create extra votes. Embedded metadata then outranks a
    # FileModifyDate-only value, direct complete fields outrank calculated Composite
    # values, and the earliest timestamp resolves the final deterministic tie.
    #
    # Only embedded offsets are retained. Even if a future read path accidentally
    # attaches an offset to a FileModifyDate candidate, this consensus boundary
    # rejects it so filesystem state cannot enter camera timezone/DST analysis.
    # METADATA still outranks OS_DATE only for the descriptive DATETYPE label.
    # ------------------------------------------------------------------------
    consensus_options = {}
    for cluster in consensus_clusters:
        records_by_exact_datetime = {}
        for candidate_datetime, source_file, candidate in cluster:
            records_by_exact_datetime.setdefault(candidate_datetime, []).append(
                (source_file, candidate)
            )

        representative_datetime = min(
            records_by_exact_datetime,
            key=lambda candidate_datetime: (
                -len(
                    {
                        source_file
                        for source_file, _ in records_by_exact_datetime[
                            candidate_datetime
                        ]
                    }
                ),
                -len(
                    {
                        source_file
                        for source_file, candidate in records_by_exact_datetime[
                            candidate_datetime
                        ]
                        if candidate["kind"] != "file_modify"
                    }
                ),
                -len(
                    {
                        source_file
                        for source_file, candidate in records_by_exact_datetime[
                            candidate_datetime
                        ]
                        if candidate["kind"] == "complete"
                    }
                ),
                candidate_datetime,
            ),
        )

        option = {
            "date_type": max(record[2]["date_type"] for record in cluster),
            "sources": [],
            "offset_records": [],
            "member_datetimes": set(records_by_exact_datetime),
            "earliest_datetime": cluster[0][0],
            "latest_datetime": cluster[-1][0],
        }

        for candidate_datetime, source_file, candidate in cluster:
            option["sources"].append((source_file, candidate["source"]))
            if (
                candidate["kind"] != "file_modify"
                and candidate["offset_minutes"] is not None
            ):
                offset_record = (
                    source_file,
                    candidate["source"],
                    candidate["offset_minutes"],
                )
                if offset_record not in option["offset_records"]:
                    option["offset_records"].append(offset_record)

        consensus_options[representative_datetime] = option

    # No complete candidate means review. The script never invents a timestamp
    # from an incomplete component or an unrelated system field.
    if not consensus_options:
        for source_file in related_files_metadata["usable_files"]:
            related_files_metadata["review_reasons"].setdefault(
                source_file,
                "no usable capture date/time",
            )
        return {
            "datetime": None,
            "date_type": None,
            "offset_records": [],
            "preserve_timezone_metadata": False,
            "preferred_utc_offset_minutes": None,
        }

    # ------------------------------------------------------------------------
    # ACCEPT ONE THRESHOLD CLUSTER OR ASK THE USER TO RESOLVE MULTIPLE CLUSTERS
    # ------------------------------------------------------------------------
    if len(consensus_options) == 1:
        selected_datetime, selected_option = next(iter(consensus_options.items()))
    else:
        sorted_options = sorted(consensus_options.items())
        print()
        print("Conflicting capture dates found for these related files:")
        for source_file in related_files:
            print(f"  - {source_file.name}")
        print("Choose the date/time that should be used:")
        for option_number, (candidate_datetime, option) in enumerate(
            sorted_options,
            start=1,
        ):
            cluster_span_seconds = int(
                (
                    option["latest_datetime"] - option["earliest_datetime"]
                ).total_seconds()
            )
            tolerance_label = (
                ""
                if cluster_span_seconds == 0
                else f"; source span {cluster_span_seconds}s"
            )
            print(
                f"  {option_number}. "
                f"{candidate_datetime.strftime('%Y-%m-%d %H:%M:%S')} - "
                f"{format_labels_by_file(option['sources'])}"
                f"{tolerance_label}"
            )

        while True:
            raw_selection = input(
                f"Enter a number from 1 to {len(sorted_options)}: "
            ).strip()
            try:
                selected_number = int(raw_selection)
            except ValueError:
                print("Invalid selection. Enter one of the listed numbers.")
                continue
            if 1 <= selected_number <= len(sorted_options):
                selected_datetime, selected_option = sorted_options[selected_number - 1]
                print(
                    "Selected capture time: "
                    f"{selected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                print()
                break
            print("Invalid selection. Enter one of the listed numbers.")

    # A unique embedded offset attached anywhere in the chosen cluster is retained
    # as the preferred interpretation of an ambiguous autumn fold and as the
    # absolute basis for the FileModifyDate written to copied media. Filesystem
    # offsets were discarded before this point. Conflicting or absent embedded
    # offsets deliberately leave the preference unset so timezone-database
    # ambiguity is not hidden.
    selected_offsets = {
        record[2] for record in selected_option.get("offset_records", [])
    }
    preferred_offset = (
        next(iter(selected_offsets)) if len(selected_offsets) == 1 else None
    )

    return {
        "datetime": selected_datetime,
        "date_type": selected_option["date_type"],
        "offset_records": selected_option["offset_records"],
        "preserve_timezone_metadata": False,
        "preferred_utc_offset_minutes": preferred_offset,
    }


# ============================================================================
# MAJOR OPERATION 3: REVIEW AND CORRECT TIMEZONE/DST
# ============================================================================


def review_and_correct_timezone_and_dst(
    capture_time_consensus,
    related_files_metadata,
    timezone_name,
    classification_timezone,
    automatic_correction_rules,
):
    """
    Validate timezone/DST evidence and update the in-memory consensus if chosen.

    This phase writes no files. Offsets attached to the selected timestamp may
    originate in sidecars. Camera configuration and UTC counterparts are limited
    to exact-primary-stem capture media so derivatives cannot define camera state.

    The selected IANA timezone is an assumption, not proof of a bad camera clock.
    Processing remains silent unless metadata contradicts valid zone states, and
    corrections are offered only when defensible final times can be calculated.
    If contradictory evidence is kept unchanged, that decision is carried into
    the write phase so timezone offsets and DST settings are not normalized later.
    """
    selected_datetime = capture_time_consensus["datetime"]
    if selected_datetime is None:
        return

    valid_states = timezone_states_for_local_time(
        selected_datetime,
        classification_timezone,
    )

    dst_records = []
    offset_records = []
    display_records = []

    # ------------------------------------------------------------------------
    # OFFSETS ATTACHED DIRECTLY TO THE SELECTED CONSENSUS VALUE
    #
    # These are the strongest offset evidence because they belong to the exact
    # timestamp the user selected. They may legitimately originate in a writable
    # XMP sidecar that supplied the selected value.
    # ------------------------------------------------------------------------
    for source_file, source_name, offset_minutes in capture_time_consensus.get(
        "offset_records",
        [],
    ):
        offset_records.append(
            (
                source_file,
                source_name,
                "UTC" + format_offset(offset_minutes),
                offset_minutes,
                "timestamp offset",
            )
        )

    # ------------------------------------------------------------------------
    # CAMERA CONFIGURATION AND UTC REFERENCES FROM PRIMARY CAPTURE MEDIA
    #
    # Camera settings and UTC counterparts are restricted to exact-primary-stem
    # image/video files. Sidecars and derivative media may carry useful capture
    # timestamps, but they cannot define what the original camera was configured
    # to do. Their copied settings may still be normalized after consensus.
    # ------------------------------------------------------------------------
    for source_file in related_files_metadata["primary_capture_media"]:
        file_record = related_files_metadata["files"].get(source_file)
        if file_record is None:
            continue

        values_by_tag = {}
        for source_name, tag_name, raw_value in file_record["timezone_context"]:
            values_by_tag.setdefault(tag_name, []).append((source_name, raw_value))

        for source_name, raw_value in values_by_tag.get("DaylightSavings", []):
            state = parse_daylight_saving_value(raw_value)
            if state is not None:
                dst_records.append((source_file, source_name, state))

        for (
            selector_tag,
            active_profile,
            active_dst_tag,
            context_tags,
        ) in resolve_active_timezone_profiles(values_by_tag):
            for source_name, raw_value in values_by_tag.get(active_dst_tag, []):
                state = parse_daylight_saving_value(raw_value)
                if state is not None:
                    dst_records.append((source_file, source_name, state))

            for context_tag in context_tags:
                for source_name, raw_value in values_by_tag.get(context_tag, []):
                    display_records.append(
                        (
                            source_file,
                            f"{source_name}={raw_value} ({active_profile})",
                        )
                    )
            for source_name, raw_value in values_by_tag.get(selector_tag, []):
                display_records.append(
                    (
                        source_file,
                        f"{source_name}={raw_value} -> {active_profile}",
                    )
                )

        for tag_name in ("TimeZoneCity", "TimeZone"):
            for source_name, raw_value in values_by_tag.get(tag_name, []):
                display_records.append((source_file, f"{source_name}={raw_value}"))

        for tag_name in OFFSET_CONTEXT_TAGS:
            for source_name, raw_value in values_by_tag.get(tag_name, []):
                offset_minutes = parse_metadata_offset_minutes(tag_name, raw_value)
                if offset_minutes is not None:
                    offset_records.append(
                        (
                            source_file,
                            source_name,
                            str(raw_value).strip(),
                            offset_minutes,
                            "metadata offset",
                        )
                    )

        for source_name, utc_datetime in file_record["utc_references"]:
            derived_offset = derive_utc_offset_from_timestamp_pair(
                selected_datetime,
                utc_datetime,
            )
            if derived_offset is not None:
                offset_records.append(
                    (
                        source_file,
                        source_name,
                        utc_datetime.strftime("%Y-%m-%d %H:%M:%S UTC"),
                        derived_offset,
                        "UTC counterpart",
                    )
                )

    # Collapse repeated telemetry without losing distinct filenames or source
    # tags. The full tuples are deduplicated, so identical values from different
    # metadata fields remain visible in the review output.
    dst_records = list(dict.fromkeys(dst_records))
    offset_records = list(dict.fromkeys(offset_records))
    display_records = list(dict.fromkeys(display_records))

    expected_offsets = {state[1] for state in valid_states}
    expected_dst_states = {state[0] for state in valid_states}
    recorded_dst_states = {state for _, _, state in dst_records}
    observed_offsets = {record[3] for record in offset_records}
    mismatched_offsets = observed_offsets - expected_offsets
    mismatched_dst = recorded_dst_states - expected_dst_states

    # Timezone configuration alone is not evidence of an error. Do not
    # prompt or shift the wall clock without contradictory metadata.
    if valid_states and not mismatched_offsets and not mismatched_dst:
        return
    if valid_states and not dst_records and not offset_records:
        return

    # ------------------------------------------------------------------------
    # DISPLAY CONTRADICTORY OR AMBIGUOUS EVIDENCE
    # ------------------------------------------------------------------------
    print()
    print("Timezone/daylight-saving review for these related files:")
    print(f"  Assumed timezone: {timezone_name}")
    print(f"  Selected local time: {selected_datetime:%Y-%m-%d %H:%M:%S}")

    if valid_states:
        expected_text = ", ".join(
            f"{abbreviation or timezone_name} UTC{format_offset(offset_minutes)}, "
            f"DST {'ON' if dst_state else 'OFF'}"
            for dst_state, offset_minutes, abbreviation in valid_states
        )
        print(f"  Expected state: {expected_text}")
    else:
        print(
            "  Expected state: this wall-clock time does not exist in the "
            "selected timezone because it falls in a transition gap."
        )

    if dst_records:
        print(
            "  Camera DST settings: "
            + format_labels_by_file(
                [
                    (
                        source_file,
                        f"{source_name}={'ON' if state else 'OFF'}",
                    )
                    for source_file, source_name, state in dst_records
                ]
            )
        )
    if offset_records:
        print(
            "  Offset/UTC evidence: "
            + format_labels_by_file(
                [
                    (
                        source_file,
                        f"{source_name}={raw_value} -> "
                        f"UTC{format_offset(offset_minutes)} [{evidence_kind}]",
                    )
                    for (
                        source_file,
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

    # ------------------------------------------------------------------------
    # CALCULATE ONLY DEFENSIBLE CORRECTION OPTIONS
    # ------------------------------------------------------------------------
    correction_reasons = {}
    if not valid_states:
        # Find the valid offsets immediately surrounding this nonexistent local
        # time. Their difference is the real transition gap, including unusual
        # half-hour or full-day civil-time jumps.
        offset_before_gap = None
        offset_after_gap = None
        for minute_distance in range(1, 2 * 24 * 60 + 1):
            if offset_before_gap is None:
                before_offsets = {
                    state[1]
                    for state in timezone_states_for_local_time(
                        selected_datetime - timedelta(minutes=minute_distance),
                        classification_timezone,
                    )
                }
                if len(before_offsets) == 1:
                    offset_before_gap = next(iter(before_offsets))

            if offset_after_gap is None:
                after_offsets = {
                    state[1]
                    for state in timezone_states_for_local_time(
                        selected_datetime + timedelta(minutes=minute_distance),
                        classification_timezone,
                    )
                }
                if len(after_offsets) == 1:
                    offset_after_gap = next(iter(after_offsets))

            if offset_before_gap is not None and offset_after_gap is not None:
                break

        if offset_before_gap is not None and offset_after_gap is not None:
            gap_adjustment = timedelta(minutes=offset_after_gap - offset_before_gap)
            corrected_datetime = selected_datetime + gap_adjustment
            if gap_adjustment > timedelta(0) and timezone_states_for_local_time(
                corrected_datetime,
                classification_timezone,
            ):
                correction_reasons.setdefault(corrected_datetime, []).append(
                    "move forward across the actual transition gap"
                )
    else:
        for observed_offset in sorted(mismatched_offsets):
            for expected_offset in sorted(expected_offsets):
                correction_delta = timedelta(minutes=expected_offset - observed_offset)
                corrected_datetime = selected_datetime + correction_delta
                correction_reasons.setdefault(corrected_datetime, []).append(
                    f"convert UTC{format_offset(observed_offset)} evidence "
                    f"to UTC{format_offset(expected_offset)}"
                )

        # A stale DST flag can exist without a usable mismatching offset. In
        # that case, find the nearest real zone state matching the recorded switch
        # and use the offset difference between that state and the selected state.
        if not correction_reasons and len(recorded_dst_states) == 1:
            if len(expected_dst_states) == 1 and mismatched_dst:
                recorded_dst = next(iter(recorded_dst_states))
                expected_offset = next(iter(expected_offsets))
                recorded_offset = None

                for day_distance in range(1, 367):
                    nearby_offsets = set()
                    for direction in (-1, 1):
                        nearby_datetime = selected_datetime + timedelta(
                            days=direction * day_distance
                        )
                        for (
                            dst_state,
                            offset_minutes,
                            _,
                        ) in timezone_states_for_local_time(
                            nearby_datetime,
                            classification_timezone,
                        ):
                            if dst_state == recorded_dst:
                                nearby_offsets.add(offset_minutes)
                    if len(nearby_offsets) == 1:
                        recorded_offset = next(iter(nearby_offsets))
                        break

                if recorded_offset is not None:
                    correction_delta = timedelta(
                        minutes=expected_offset - recorded_offset
                    )
                    if correction_delta != timedelta(0):
                        correction_reasons.setdefault(
                            selected_datetime + correction_delta,
                            [],
                        ).append(
                            "correct the contradictory camera DST setting using "
                            "the nearest matching timezone state"
                        )

    if not correction_reasons:
        print(
            "  The evidence is contradictory or ambiguous, and no single "
            "defensible correction can be calculated. The selected time is kept."
        )
        # The absence of a defensible wall-clock correction must not be treated
        # as permission to rewrite the contradictory timezone/DST metadata later.
        capture_time_consensus["preserve_timezone_metadata"] = True
        print()
        return

    correction_options = sorted(correction_reasons.items())

    # Reuse a previous answer only when exactly one correction option matches
    # the complete evidence signature. Ambiguous/different cases still prompt.
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

    if len(automatic_matches) == 1:
        corrected_datetime, _, correction_delta = automatic_matches[0]
        signed_hours = correction_delta.total_seconds() / 3600
        hour_unit = "hour" if abs(signed_hours) == 1 else "hours"
        print(
            "  Automatically applying the previously approved matching "
            f"correction ({signed_hours:+g} {hour_unit}): "
            f"{corrected_datetime:%Y-%m-%d %H:%M:%S}"
        )
        print()
        capture_time_consensus["datetime"] = corrected_datetime
        capture_time_consensus["date_type"] = 2
        capture_time_consensus["preserve_timezone_metadata"] = False
        return

    print("Choose how these files should be classified:")
    print(f"  1. Keep {selected_datetime:%Y-%m-%d %H:%M:%S}")
    for option_number, (corrected_datetime, reasons) in enumerate(
        correction_options,
        start=2,
    ):
        print(
            f"  {option_number}. Correct to "
            f"{corrected_datetime:%Y-%m-%d %H:%M:%S} "
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
            # "Keep" applies to both the selected wall clock and the existing
            # timezone/DST representation. The later metadata writer must not
            # silently override the user's explicit decision.
            capture_time_consensus["preserve_timezone_metadata"] = True
            print()
            return

        correction_index = selected_number - 2
        if 0 <= correction_index < len(correction_options):
            corrected_datetime = correction_options[correction_index][0]
            correction_delta = corrected_datetime - selected_datetime
            capture_time_consensus["datetime"] = corrected_datetime
            capture_time_consensus["date_type"] = 2
            capture_time_consensus["preserve_timezone_metadata"] = False

            correction_key = build_automatic_correction_key(
                correction_delta,
                recorded_dst_states,
                expected_dst_states,
                observed_offsets,
                expected_offsets,
            )
            while True:
                reuse_selection = (
                    input(
                        "Automatically apply this same correction to later "
                        "matching evidence during this run? [y/N]: "
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
                        "applied automatically to later matches."
                    )
                    break
                print("Invalid selection. Enter y or n.")
            print()
            return

        print("Invalid selection. Enter one of the listed numbers.")


# ============================================================================
# METADATA WRITE/VERIFICATION HELPERS
# ============================================================================
#
# Planned values are absolute final values. Duplicate targets are resolved
# before one combined ExifTool call, then reread and compared semantically so
# harmless separator/numeric normalization is not reported as failure.


def add_correction_operation(operations, skipped, operation):
    """
    Add one absolute write and reject contradictory duplicate targets.

    Duplicate extraction paths may map to one writable target. Identical writes
    collapse; contradictory desired values skip the target rather than choosing
    arbitrarily.
    """
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


def format_metadata_offset_value(tag_name, original_value, offset_minutes):
    """
    Preserve a standalone offset field's representation where practical.

    Numeric TimeZoneOffset values remain numeric hours and are written through
    ExifTool's raw-value target. Text fields retain surrounding maker text when
    it contains one replaceable signed offset.
    """
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


def operation_matches(operation, actual_values):
    """
    Compare read-back values semantically rather than textually.

    ExifTool may normalize separators, raw encodings, or numeric formatting, so
    verification reparses timestamps, offsets, and DST states.
    """
    expected = operation["expected"]
    for actual_value in actual_values:
        try:
            if operation["kind"] == "datetime":
                parsed = parse_metadata_datetime(actual_value)
                if (
                    parsed is not None
                    and (
                        parsed.local_datetime,
                        parsed.utc_offset_minutes,
                    )
                    == expected
                ):
                    return True
            elif operation["kind"] == "date":
                parsed = parse_metadata_datetime(actual_value)
                if parsed is not None and parsed.local_date == expected:
                    return True
            elif operation["kind"] == "time":
                parsed = parse_metadata_datetime(actual_value)
                if (
                    parsed is not None
                    and (
                        parsed.local_time,
                        parsed.utc_offset_minutes,
                    )
                    == expected
                ):
                    return True
            elif operation["kind"] == "offset":
                if (
                    parse_metadata_offset_minutes(
                        operation["tag_name"],
                        actual_value,
                    )
                    == expected
                ):
                    return True
            elif operation["kind"] == "dst":
                if parse_daylight_saving_value(actual_value) == expected:
                    return True
        except (TypeError, ValueError):
            continue
    return False


# ============================================================================
# MAJOR OPERATION 4 SUPPORT: UPDATE A NEWLY CREATED CLASSIFIED COPY
# ============================================================================


def update_copied_file_metadata_and_system_times(
    metadata_reader,
    copied_file,
    file_record,
    capture_time_consensus,
    classification_timezone,
):
    """
    Normalize eligible metadata and FileModifyDate in one newly created copy.

    The final consensus is authoritative. Existing writable complete capture
    timestamps and existing writable date-only/time-only components are aligned
    to it. This is an absolute normalization, not a delta applied to an unrelated
    original value. Composite, System, File, UTC-reference,
    editing/history/runtime, and other excluded fields are never direct targets.
    Missing fields are not made.

    Timestamp-associated offsets may be updated in media or sidecars. Camera-
    global offsets and active DST/profile values are updated only in primary or
    derivative image/video files; inactive profiles remain untouched. If the user
    explicitly keeps contradictory timezone/DST evidence, all existing timezone
    representations are preserved while local capture fields may still follow the
    selected consensus. Ambiguous wall clocks are never assigned a guessed offset.

    Writes are batched, retried individually if one read-only maker field blocks
    the batch, and verified by rereading the copy. FileModifyDate is aligned only
    for copied image/video files.
    """
    selected_datetime = capture_time_consensus["datetime"]
    if selected_datetime is None:
        return [], []

    operations = {}
    skipped = set()
    updated = []
    records = file_record["records"]
    preserve_timezone_metadata = capture_time_consensus.get(
        "preserve_timezone_metadata",
        False,
    )

    # This offset still determines the absolute FileModifyDate written to copied
    # media. It is used to normalize embedded timezone metadata only when review
    # did not explicitly preserve contradictory evidence.
    valid_filesystem_offsets = {
        state[1]
        for state in timezone_states_for_local_time(
            selected_datetime,
            classification_timezone,
        )
    }
    preferred_offset = capture_time_consensus.get("preferred_utc_offset_minutes")
    if preferred_offset in valid_filesystem_offsets:
        expected_offset = preferred_offset
    elif len(valid_filesystem_offsets) == 1:
        expected_offset = next(iter(valid_filesystem_offsets))
    else:
        expected_offset = None

    # Context fields are indexed by tag because their semantics are independent
    # of one storage family; complete capture fields are still handled from their
    # original records so each explicit write target remains available.
    values_by_tag = {}
    for metadata_key, groups, tag_name, raw_value in records:
        values_by_tag.setdefault(tag_name, []).append((metadata_key, groups, raw_value))

    # ------------------------------------------------------------------------
    # ALIGN EVERY WRITABLE ELIGIBLE COMPLETE CAPTURE TIMESTAMP TO CONSENSUS
    #
    # Rejected values and other eligible local capture fields all receive the
    # final selected wall clock. An inline offset is replaced only if that field
    # already had one; naive values remain naive. A separate OffsetTime* value is
    # handled in the later offset section, so timestamp and attached offset reach
    # the same final state without inventing either representation.
    # ------------------------------------------------------------------------
    for metadata_key, groups, tag_name, raw_value in records:
        if (
            tag_name in TIME_CONTEXT_TAGS
            or tag_name in UTC_REFERENCE_TAGS
            or tag_name in EXCLUDED_CAPTURE_TIME_TAGS
            or "System" in groups
        ):
            continue

        write_target = get_metadata_write_target(groups, tag_name)
        if write_target is None:
            # Composite and other calculated/read-only values remain evidence
            # only; their writable components are handled separately below.
            continue

        try:
            parsed = parse_metadata_datetime(raw_value)
        except ValueError:
            skipped.add(f"{metadata_key} (invalid capture timestamp)")
            continue
        if parsed is None or parsed.local_datetime is None:
            continue

        inline_offset = parsed.utc_offset_minutes
        target_offset = (
            (inline_offset if preserve_timezone_metadata else expected_offset)
            if inline_offset is not None
            else None
        )
        if inline_offset is not None and target_offset is None:
            skipped.add(f"{metadata_key} (ambiguous consensus UTC offset)")
            continue

        expected_text = format_complete_datetime(
            raw_value,
            selected_datetime,
            target_offset,
        )
        expected_value = (selected_datetime, target_offset)
        if (
            parsed.local_datetime == selected_datetime
            and parsed.utc_offset_minutes == target_offset
        ):
            continue

        add_correction_operation(
            operations,
            skipped,
            {
                "target": write_target,
                "value": expected_text,
                "kind": "datetime",
                "expected": expected_value,
                "tag_name": tag_name,
            },
        )

    # ------------------------------------------------------------------------
    # ALIGN WRITABLE DATE-ONLY/TIME-ONLY COMPONENTS TO CONSENSUS
    #
    # Components do not become candidates alone, but can feed Composite values.
    # Existing parts are corrected independently, so incomplete pairs are fixed
    # without inventing their missing counterpart. When both parts exist, using
    # the final consensus for each also preserves midnight/day rollover correctly.
    # ------------------------------------------------------------------------
    for metadata_key, groups, tag_name, raw_value in records:
        if (
            tag_name in TIME_CONTEXT_TAGS
            or tag_name in UTC_REFERENCE_TAGS
            or tag_name in EXCLUDED_CAPTURE_TIME_TAGS
            or "System" in groups
        ):
            continue

        write_target = get_metadata_write_target(groups, tag_name)
        if write_target is None:
            continue

        try:
            parsed = parse_metadata_datetime(raw_value)
        except ValueError:
            continue
        if parsed is None or parsed.local_datetime is not None:
            continue

        if parsed.local_date is not None:
            expected_date = selected_datetime.date()
            if parsed.local_date != expected_date:
                add_correction_operation(
                    operations,
                    skipped,
                    {
                        "target": write_target,
                        "value": format_date_only(raw_value, selected_datetime),
                        "kind": "date",
                        "expected": expected_date,
                        "tag_name": tag_name,
                    },
                )

        if parsed.local_time is not None:
            target_offset = (
                (
                    parsed.utc_offset_minutes
                    if preserve_timezone_metadata
                    else expected_offset
                )
                if parsed.utc_offset_minutes is not None
                else None
            )
            if parsed.utc_offset_minutes is not None and target_offset is None:
                skipped.add(f"{metadata_key} (ambiguous consensus UTC offset)")
                continue
            if (
                parsed.local_time != selected_datetime.time()
                or parsed.utc_offset_minutes != target_offset
            ):
                add_correction_operation(
                    operations,
                    skipped,
                    {
                        "target": write_target,
                        "value": format_time_only(
                            raw_value,
                            selected_datetime,
                            target_offset,
                        ),
                        "kind": "time",
                        "expected": (selected_datetime.time(), target_offset),
                        "tag_name": tag_name,
                    },
                )

    # ------------------------------------------------------------------------
    # NORMALIZE EXISTING OFFSETS AGAINST THE FINAL CONSENSUS
    #
    # This occurs after consensus and timezone review. Timestamp-associated
    # offsets follow their local capture fields and may be updated in sidecars;
    # camera-global offsets describe the selected camera state and are limited to
    # media files. Missing offsets are never created, and ambiguity causes a skip
    # instead of a guessed value.
    # ------------------------------------------------------------------------
    if expected_offset is not None and not preserve_timezone_metadata:
        # Timestamp-associated offsets may exist in media or sidecars.
        for tag_name in sorted(ASSOCIATED_OFFSET_FIELDS):
            for metadata_key, groups, raw_value in values_by_tag.get(tag_name, []):
                write_target = get_metadata_write_target(groups, tag_name)
                if write_target is None:
                    continue
                actual_offset = parse_metadata_offset_minutes(tag_name, raw_value)
                if actual_offset == expected_offset:
                    continue
                add_correction_operation(
                    operations,
                    skipped,
                    {
                        "target": write_target,
                        "value": format_metadata_offset_value(
                            tag_name,
                            raw_value,
                            expected_offset,
                        ),
                        "kind": "offset",
                        "expected": expected_offset,
                        "tag_name": tag_name,
                    },
                )

        # Camera-global offsets and DST/profile values belong only to image/video
        # files, including derivative-stem media. Only the selected active
        # Pentax/Ricoh profile can change; inactive profile/city/selector fields
        # remain untouched.
        if file_record["is_capture_media"]:
            for tag_name in sorted(GLOBAL_OFFSET_TAGS):
                for metadata_key, groups, raw_value in values_by_tag.get(
                    tag_name,
                    [],
                ):
                    write_target = get_metadata_write_target(
                        groups,
                        tag_name,
                        raw=(
                            tag_name == "TimeZoneOffset"
                            and isinstance(raw_value, (int, float))
                        ),
                    )
                    if write_target is None:
                        continue
                    actual_offset = parse_metadata_offset_minutes(
                        tag_name,
                        raw_value,
                    )
                    if actual_offset == expected_offset:
                        continue
                    add_correction_operation(
                        operations,
                        skipped,
                        {
                            "target": write_target,
                            "value": format_metadata_offset_value(
                                tag_name,
                                raw_value,
                                expected_offset,
                            ),
                            "kind": "offset",
                            "expected": expected_offset,
                            "tag_name": tag_name,
                        },
                    )

            expected_state = resolve_unambiguous_timezone_state(
                selected_datetime,
                classification_timezone,
            )
            if expected_state is not None:
                expected_dst = expected_state[0]
                dst_tags = set(DIRECT_DST_TAGS)
                dst_tags.update(
                    active_dst_tag
                    for _, _, active_dst_tag, _ in (
                        resolve_active_timezone_profiles(values_by_tag)
                    )
                )

                for tag_name in sorted(dst_tags):
                    for metadata_key, groups, raw_value in values_by_tag.get(
                        tag_name,
                        [],
                    ):
                        write_target = get_metadata_write_target(
                            groups,
                            tag_name,
                        )
                        if write_target is None:
                            continue
                        actual_dst = parse_daylight_saving_value(raw_value)
                        if actual_dst == expected_dst:
                            continue

                        # Write through ExifTool's normal PrintConv path. Preserve
                        # the vocabulary already exposed for this tag while letting
                        # ExifTool perform the tag-specific inverse conversion.
                        dst_vocabulary = str(raw_value).strip().casefold()
                        if dst_vocabulary in {"yes", "no"}:
                            target_dst_value = "Yes" if expected_dst else "No"
                        elif dst_vocabulary in {"enabled", "disabled"}:
                            target_dst_value = "Enabled" if expected_dst else "Disabled"
                        elif dst_vocabulary in {
                            "daylight saving",
                            "standard time",
                        }:
                            target_dst_value = (
                                "Daylight Saving" if expected_dst else "Standard Time"
                            )
                        else:
                            target_dst_value = "On" if expected_dst else "Off"

                        add_correction_operation(
                            operations,
                            skipped,
                            {
                                "target": write_target,
                                "value": target_dst_value,
                                "kind": "dst",
                                "expected": expected_dst,
                                "tag_name": tag_name,
                            },
                        )
    elif not preserve_timezone_metadata:
        for tag_name in sorted(ASSOCIATED_OFFSET_FIELDS | GLOBAL_OFFSET_TAGS):
            if values_by_tag.get(tag_name):
                skipped.add(f"{tag_name} (ambiguous consensus UTC offset)")

    # When review explicitly kept contradictory timezone/DST evidence, reaching
    # this point without offset or DST operations is intentional—not a skipped or
    # failed correction. FileModifyDate remains handled independently below.

    # ------------------------------------------------------------------------
    # EXECUTE EXIFTOOL WRITES, RETRY INDIVIDUALLY, AND VERIFY
    #
    # Mixed files may contain writable standard tags and read-only maker tags.
    # The combined write is attempted first for consistency and efficiency.
    # Individual retries then prevent one unsupported maker-note field from
    # blocking common EXIF/XMP/IPTC/QuickTime updates that ExifTool can write.
    # Every requested target is reread afterward and compared semantically.
    # ------------------------------------------------------------------------
    operation_list = list(operations.values())
    if operation_list:
        write_arguments = list(CORRECTION_WRITE_PARAMS)
        write_arguments.extend(
            f"-{operation['target']}={operation['value']}"
            for operation in operation_list
        )
        write_arguments.append(str(copied_file))
        try:
            metadata_reader.execute(*write_arguments)
        except ExifToolException:
            for operation in operation_list:
                try:
                    metadata_reader.execute(
                        *CORRECTION_WRITE_PARAMS,
                        f"-{operation['target']}={operation['value']}",
                        str(copied_file),
                    )
                except ExifToolException as error:
                    skipped.add(f"{operation['target'].removesuffix('#')} ({error})")

        try:
            verification_results = metadata_reader.get_tags(
                files=copied_file,
                tags=["Time:All", *sorted(TIME_CONTEXT_TAGS)],
                params=["-a", "-ee"],
            )
        except ExifToolException as error:
            raise OSError(
                f"ExifTool could not verify corrected metadata in "
                f"'{copied_file}': {error}"
            ) from error

        actual_by_target = {}
        verification_metadata = verification_results[0] if verification_results else {}
        for _, groups, tag_name, raw_value in iterate_exiftool_values(
            verification_metadata
        ):
            target = get_metadata_write_target(groups, tag_name)
            if target is not None:
                actual_by_target.setdefault(target, []).append(raw_value)

        for operation in operation_list:
            target = operation["target"].removesuffix("#")
            if operation_matches(operation, actual_by_target.get(target, [])):
                updated.append(target)
            else:
                skipped.add(f"{target} (not writable or verification failed)")

    # ------------------------------------------------------------------------
    # ALIGN FILEMODIFYDATE ONLY FOR PRIMARY/DERIVATIVE IMAGE AND VIDEO COPIES
    #
    # FileModifyDate is widely consumed by file browsers and media software, so
    # it is set to the absolute final capture instant—not shifted by a delta from
    # an unrelated source-file modification time. Primary and derivative media
    # copies are aligned; sidecars keep their copied filesystem timestamp. Access
    # time is set to the current time and is not part of the metadata policy.
    # ------------------------------------------------------------------------
    if file_record["is_capture_media"]:
        if expected_offset is None:
            skipped.add("FileModifyDate (ambiguous consensus UTC offset)")
        else:
            aware_selected_datetime = selected_datetime.replace(
                tzinfo=timezone(timedelta(minutes=expected_offset))
            )
            selected_utc_datetime = aware_selected_datetime.astimezone(timezone.utc)
            epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
            epoch_delta = selected_utc_datetime - epoch
            selected_mtime_ns = (
                epoch_delta.days * 86400 + epoch_delta.seconds
            ) * 1_000_000_000 + epoch_delta.microseconds * 1000
            if copied_file.stat().st_mtime_ns != selected_mtime_ns:
                os.utime(
                    copied_file,
                    ns=(time.time_ns(), selected_mtime_ns),
                )
                if copied_file.stat().st_mtime_ns != selected_mtime_ns:
                    raise OSError(
                        "could not verify aligned FileModifyDate for "
                        f"'{copied_file}'"
                    )
                updated.append("FileModifyDate")

    return sorted(set(updated)), sorted(skipped)


# ============================================================================
# MAJOR OPERATION 4: COPY RELATED FILES TO THEIR OUTPUT FOLDERS
# ============================================================================


def copy_related_files_to_output(
    metadata_reader,
    related_files,
    related_files_metadata,
    capture_time_consensus,
    classification_timezone,
    classified_directory,
    unclassified_directory,
    stats,
):
    """
    Copy related files safely, update new classified copies, and report results.

    Review files go to ``unclassified`` and are never modified. A classified
    file is always copied to a new collision-safe path before correction; an
    existing output is never the initial correction target. Only after the new
    copy is corrected/verified is it compared with earlier collision candidates.
    An identical corrected duplicate is deleted and the existing result reused.

    Any failed copy/correction removes the partial new destination while leaving
    the source and all pre-existing outputs unchanged.
    """
    selected_datetime = capture_time_consensus["datetime"]
    review_reasons = related_files_metadata["review_reasons"]
    usable_files = related_files_metadata["usable_files"]

    for source_file in related_files:
        if source_file in review_reasons:
            folder_name = UNCLASSIFIED_FOLDER_NAME
            destination_directory = unclassified_directory
            classified_copy = False
        elif source_file in usable_files and selected_datetime is not None:
            date_folder_name = selected_datetime.strftime("%Y-%m-%d")
            folder_name = f"{CLASSIFIED_FOLDER_NAME}/{date_folder_name}"
            destination_directory = classified_directory / date_folder_name
            classified_copy = True
        else:
            continue

        if destination_directory.exists() and not destination_directory.is_dir():
            print(
                f"Error: '{destination_directory}' is a file. "
                f"Skipping '{source_file.name}'.",
                file=sys.stderr,
            )
            stats["failed"] += 1
            continue

        try:
            destination_directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            print(
                f"Error creating '{destination_directory}': {error}",
                file=sys.stderr,
            )
            stats["failed"] += 1
            continue

        # Record existing collision candidates without modifying them. A
        # classified file is compared only after its new copy has final bytes.
        requested_destination = destination_directory / source_file.name
        existing_candidates = []
        suffix_number = 0
        while True:
            copied_file = (
                requested_destination
                if suffix_number == 0
                else requested_destination.with_name(
                    f"{requested_destination.stem}_{suffix_number}"
                    f"{requested_destination.suffix}"
                )
            )
            if not copied_file.exists():
                break
            if copied_file.is_file():
                existing_candidates.append(copied_file)
                if not classified_copy and files_are_binary_identical(
                    source_file,
                    copied_file,
                ):
                    break
            suffix_number += 1

        copied = False
        renamed = copied_file != requested_destination
        updated_targets = []
        skipped_targets = []
        reused_corrected_duplicate = False

        if not classified_copy and copied_file.exists():
            # Unclassified files are never modified. Reuse an existing exact
            # binary duplicate immediately.
            final_destination = copied_file
        else:
            destination_created = False
            try:
                with source_file.open("rb") as source_stream:
                    with copied_file.open("xb") as destination_stream:
                        destination_created = True
                        shutil.copyfileobj(
                            source_stream,
                            destination_stream,
                            length=COPY_CHUNK_SIZE,
                        )
                shutil.copystat(source_file, copied_file)
                copied = True

                if classified_copy:
                    file_record = related_files_metadata["files"][source_file]
                    updated_targets, skipped_targets = (
                        update_copied_file_metadata_and_system_times(
                            metadata_reader,
                            copied_file,
                            file_record,
                            capture_time_consensus,
                            classification_timezone,
                        )
                    )

                    # Compare only after correction. Existing output files are
                    # never used as the initial correction target.
                    previous_duplicate = next(
                        (
                            candidate
                            for candidate in existing_candidates
                            if files_are_binary_identical(copied_file, candidate)
                        ),
                        None,
                    )
                    if previous_duplicate is not None:
                        copied_file.unlink()
                        final_destination = previous_duplicate
                        copied = False
                        renamed = previous_duplicate != requested_destination
                        reused_corrected_duplicate = True
                    else:
                        final_destination = copied_file
                else:
                    final_destination = copied_file
            except (OSError, ExifToolException) as error:
                if destination_created:
                    try:
                        copied_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                print(
                    f"Error copying or updating '{source_file.name}': {error}",
                    file=sys.stderr,
                )
                stats["failed"] += 1
                continue

        print(f"{source_file.name}\t--->\t{folder_name}\t", end="")
        if copied:
            stats["copied"] += 1
            if renamed:
                stats["renamed"] += 1
                print(f"COPIED AS {final_destination.name}", end="")
            else:
                print("COPIED", end="")
        else:
            stats["duplicates"] += 1
            print(f"DUPLICATE OF {final_destination.name}", end="")

        if reused_corrected_duplicate:
            print("; ALREADY CORRECTED", end="")
        elif updated_targets:
            stats["metadata_updated"] += 1
            print("; UPDATED: " + ", ".join(updated_targets), end="")
        if skipped_targets and not reused_corrected_duplicate:
            stats["metadata_skipped"] += 1
            print("; SKIPPED: " + ", ".join(skipped_targets), end="")

        if source_file in review_reasons:
            stats["review"] += 1
            print(f"; REVIEW: {review_reasons[source_file]}", end="")
        else:
            print(
                f"; {selected_datetime:%Y-%m-%d %H:%M:%S} "
                f"{DATETYPE[capture_time_consensus['date_type']]}",
                end="",
            )
        print()


# ============================================================================
# COMMAND-LINE ENTRY POINT AND COMPLETE PROGRAM FLOW
# ============================================================================


def main():
    """
    Validate input and execute the complete interactive classification flow.

    Only regular, non-symlink files directly inside the source directory are
    processed; subdirectories are deliberately not traversed. One persistent
    ExifTool process serves the run and is terminated in ``finally``.
    """
    # ------------------------------------------------------------------------
    # VALIDATE SOURCE AND OUTPUT DIRECTORIES
    # ------------------------------------------------------------------------
    raw_source_path = input(
        "Please write (or drag) the source directory path: "
    ).strip()
    if (
        len(raw_source_path) >= 2
        and raw_source_path[0] == raw_source_path[-1]
        and raw_source_path[0] in {'"', "'"}
    ):
        raw_source_path = raw_source_path[1:-1]
    source_directory = Path(raw_source_path).expanduser()
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
        # Process only the selected directory. Excluding symlinks and
        # subdirectories prevents the run from escaping that explicit scope.
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

    related_file_sets = group_related_files(source_files)

    # ------------------------------------------------------------------------
    # START ONE PERSISTENT EXIFTOOL PROCESS AND INITIALIZE RUN STATE
    #
    # Stay-open reuse avoids launching ExifTool per file and keeps all reads,
    # writes, and verification in the same explicit group-name mode.
    # ------------------------------------------------------------------------
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
        # Each related set follows one visible linear flow: read evidence,
        # establish consensus, review timezone/DST, then copy/update only new
        # classified destinations.
        for primary_stem, related_files in related_file_sets:
            # ----------------------------------------------------------------
            # 1. READ METADATA FROM THE RELATED SOURCE FILES
            # ----------------------------------------------------------------
            related_files_metadata = read_related_files_metadata(
                metadata_reader,
                primary_stem,
                related_files,
            )
            stats["failed"] += related_files_metadata["failures"]

            # ----------------------------------------------------------------
            # 2. ESTABLISH THE AUTHORITATIVE CAPTURE-TIME CONSENSUS
            # ----------------------------------------------------------------
            capture_time_consensus = determine_capture_time_consensus(
                related_files,
                related_files_metadata,
            )

            # ----------------------------------------------------------------
            # 3. REVIEW AND CORRECT TIMEZONE/DST WHEN JUSTIFIED
            # ----------------------------------------------------------------
            review_and_correct_timezone_and_dst(
                capture_time_consensus,
                related_files_metadata,
                timezone_name,
                classification_timezone,
                automatic_correction_rules,
            )

            # ----------------------------------------------------------------
            # 4. COPY FILES AND UPDATE ONLY NEW CLASSIFIED COPIES
            # ----------------------------------------------------------------
            copy_related_files_to_output(
                metadata_reader,
                related_files,
                related_files_metadata,
                capture_time_consensus,
                classification_timezone,
                classified_directory,
                unclassified_directory,
                stats,
            )
    finally:
        metadata_reader.terminate()

    # ------------------------------------------------------------------------
    # FINAL SUMMARY
    # ------------------------------------------------------------------------
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
        "Corrected copies with skipped/read-only fields: "
        f"{stats['metadata_skipped']}"
    )
    print("Original source files were not modified.")
    input("Press Enter to exit")
    return 2 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
