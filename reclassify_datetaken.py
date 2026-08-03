"""
Classify related media files by their best-supported capture state.

The source directory is immutable: originals are never written, renamed, moved,
or used as metadata-correction targets. Exact-stem files and recognized derivative
stems are processed together so original media, derived media, and sidecars share
one authoritative decision.

A capture-state choice consists of the exact local timestamp—including available
subseconds—plus compatible explicit UTC-offset and daylight-saving evidence.
Nearby timestamps may share one option only when their complete earliest-to-latest
span stays within the configured threshold and their known offset/DST states do
not contradict one another. Unknown state supports a compatible explicit state;
it never erases a genuine conflict.

One persistent ExifTool process reads complete embedded timestamps, calculated
Composite timestamps, timezone/DST evidence, and a narrowly scoped filesystem
``FileModifyDate`` candidate. Filesystem offsets are discarded. Complete Composite
values may participate in consensus, but calculated/read-only fields are never
written. Existing writable complete, date-only, and time-only fields are normalized
only in a newly created copy so ExifTool can recalculate Composite values.

After consensus and any explicit timezone/DST review, invalid writable capture
fields may be repaired only with a valid final capture state and explicit user
approval. Files are then copied into ``classified/YYYY-MM-DD`` or ``unclassified``.
Existing outputs are considered reusable only after the new copy has reached its
final embedded-metadata and filesystem-time state.
"""

import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
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

# Candidate timestamps may share one temporal option only when the exact
# earliest-to-latest span—including all available fractional digits—does not
# exceed this threshold. Explicitly different UTC offsets or DST states still
# create separate choices even when their timestamps are otherwise identical.
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
    fraction: str | None = None

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


def parse_metadata_datetime(date_value):
    """
    Parse one complete, date-only, or time-only metadata value.

    ``None`` means the representation is not recognized. Recognized but
    impossible calendar, clock, or offset values raise ``ValueError``. Missing
    date or time components remain ``None`` and are never invented. Explicit
    offsets remain separate from the naive local wall clock because folder
    classification follows the camera's local capture date.

    Fractional seconds are retained exactly in ``fraction`` and represented to
    microsecond precision in ``local_time`` for ordinary civil-time operations.
    Consensus threshold arithmetic separately uses the complete fraction string.
    Separate date/time fields are not paired here; ExifTool Composite values are
    the complete consensus candidates, while writable components are updated only
    after the final consensus is selected.
    """
    value_text = str(date_value).strip()

    complete_match = COMPLETE_DATE_TIME_PATTERN.fullmatch(value_text)
    if complete_match is not None:
        fraction = complete_match.group("fraction")
        microsecond = int(((fraction or "") + "000000")[:6] or "0")
        parsed_date = date(
            int(complete_match.group("year")),
            int(complete_match.group("month")),
            int(complete_match.group("day")),
        )
        parsed_time = datetime_time(
            int(complete_match.group("hour")),
            int(complete_match.group("minute")),
            int(complete_match.group("second")),
            microsecond,
        )
        return ParsedMetadataDateTime(
            parsed_date,
            parsed_time,
            parse_utc_offset_minutes(complete_match.group("timezone")),
            fraction,
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
        fraction = time_match.group("fraction")
        microsecond = int(((fraction or "") + "000000")[:6] or "0")
        return ParsedMetadataDateTime(
            None,
            datetime_time(
                int(time_match.group("hour")),
                int(time_match.group("minute")),
                int(time_match.group("second")),
                microsecond,
            ),
            parse_utc_offset_minutes(time_match.group("timezone")),
            fraction,
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


def format_consensus_datetime(local_datetime, fraction=None):
    """Format a selected timestamp without discarding its exact subseconds."""
    formatted = local_datetime.strftime("%Y-%m-%d %H:%M:%S")
    if fraction:
        formatted += f".{fraction}"
    elif local_datetime.microsecond:
        formatted += f".{local_datetime.microsecond:06d}".rstrip("0")
    return formatted


def fit_fraction_to_precision(fraction, microsecond, precision):
    """Represent the selected fraction using an existing field's precision."""
    selected_fraction = fraction
    if selected_fraction is None:
        selected_fraction = f"{microsecond:06d}"
    return (selected_fraction + "0" * precision)[:precision]


def format_complete_datetime(
    original_value,
    local_datetime,
    offset_minutes=None,
    fraction=None,
):
    """Preserve a complete field's syntax/precision while replacing its value."""
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
    original_fraction = date_match.group("fraction")
    if original_fraction is not None:
        formatted += "." + fit_fraction_to_precision(
            fraction,
            local_datetime.microsecond,
            len(original_fraction),
        )

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


def format_time_only(
    original_value,
    local_datetime,
    offset_minutes=None,
    fraction=None,
):
    """Preserve a time field's syntax/precision while replacing its value."""
    original_text = str(original_value).strip()
    time_match = TIME_ONLY_PATTERN.fullmatch(original_text)
    if time_match is None:
        raise ValueError(f"not a time-only value: {original_value!r}")

    formatted = (
        f"{local_datetime.hour:02d}:{local_datetime.minute:02d}:"
        f"{local_datetime.second:02d}"
    )
    original_fraction = time_match.group("fraction")
    if original_fraction is not None:
        formatted += "." + fit_fraction_to_precision(
            fraction,
            local_datetime.microsecond,
            len(original_fraction),
        )

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
    consensus candidates. UTC counterparts, associated offsets, camera-global
    offsets, and DST/profile settings are routed to distinct evidence collections.
    Associated OffsetTime* values retain their ExifTool family/namespace so they
    can be attached only to the timestamp representation they actually describe.
    Date-only and time-only values are not combined into candidates; raw records
    remain available so existing writable components can follow the final state.

    Recognized but impossible local, UTC-reference, and filesystem timestamps are
    retained as invalid records, never as candidates. When a target is writable,
    repair may be explicitly approved only after a valid final capture state and
    timezone/DST decision exist; otherwise the original value remains untouched.

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
            "global_context_records": [],
            "associated_offset_records": [],
            "utc_references": [],
            "invalid_capture_records": [],
            "warnings": [],
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

                # Valid GPS date-only/time-only components are incomplete UTC
                # evidence, not errors. They remain untouched and do not become
                # standalone candidates now that explicit component pairing has
                # been removed. Every other invalid UTC representation is retained
                # as a repairable record when its ExifTool target is writable.
                if parsed_utc is not None and (
                    (tag_name == "GPSDateStamp" and parsed_utc.local_date is not None)
                    or (
                        tag_name == "GPSTimeStamp" and parsed_utc.local_time is not None
                    )
                ):
                    continue

                if tag_name == "GPSDateStamp":
                    invalid_kind = "utc_date"
                elif tag_name == "GPSTimeStamp":
                    invalid_kind = "utc_time"
                else:
                    invalid_kind = "utc_datetime"
                file_record["invalid_capture_records"].append(
                    {
                        "metadata_key": metadata_key,
                        "groups": groups,
                        "tag_name": tag_name,
                        "raw_value": raw_value,
                        "kind": invalid_kind,
                        "repair_approved": False,
                    }
                )
                continue

            # Offset, city, profile, and DST fields describe context; they
            # are evidence rather than capture-time choices.
            if tag_name in TIME_CONTEXT_TAGS:
                context_record = (metadata_key, tag_name, raw_value)
                if context_record not in file_record["timezone_context"]:
                    file_record["timezone_context"].append(context_record)

                # File-global camera configuration retains its provenance. The
                # broad metadata family and concrete namespace identify one real
                # configuration bundle; Copy* extraction labels are intentionally
                # ignored because they are duplicate views, not another camera.
                if groups:
                    family_zero = groups[0]
                    family_one = next(
                        (
                            group_name
                            for group_name in groups[1:]
                            if not group_name.casefold().startswith("copy")
                        ),
                        family_zero,
                    )
                    global_context_record = (
                        (family_zero, family_one),
                        metadata_key,
                        tag_name,
                        raw_value,
                    )
                    if (
                        global_context_record
                        not in file_record["global_context_records"]
                    ):
                        file_record["global_context_records"].append(
                            global_context_record
                        )

                # Separate OffsetTime* fields are associated only with a
                # timestamp in the same ExifTool family/namespace. Copy labels
                # are extraction artifacts and do not define another namespace.
                if tag_name in ASSOCIATED_OFFSET_FIELDS and groups:
                    family_zero = groups[0]
                    family_one = next(
                        (
                            group_name
                            for group_name in groups[1:]
                            if not group_name.casefold().startswith("copy")
                        ),
                        family_zero,
                    )
                    associated_record = (
                        (family_zero, family_one),
                        metadata_key,
                        tag_name,
                        raw_value,
                    )
                    if (
                        associated_record
                        not in file_record["associated_offset_records"]
                    ):
                        file_record["associated_offset_records"].append(
                            associated_record
                        )
                continue

            if "System" in groups or tag_name in EXCLUDED_CAPTURE_TIME_TAGS:
                continue

            try:
                parsed = parse_metadata_datetime(raw_value)
            except ValueError:
                invalid_kind = None
                if COMPLETE_DATE_TIME_PATTERN.fullmatch(str(raw_value).strip()):
                    invalid_kind = "datetime"
                elif DATE_ONLY_PATTERN.fullmatch(str(raw_value).strip()):
                    invalid_kind = "date"
                elif TIME_ONLY_PATTERN.fullmatch(str(raw_value).strip()):
                    invalid_kind = "time"
                if invalid_kind is not None:
                    file_record["invalid_capture_records"].append(
                        {
                            "metadata_key": metadata_key,
                            "groups": groups,
                            "tag_name": tag_name,
                            "raw_value": raw_value,
                            "kind": invalid_kind,
                            "repair_approved": False,
                        }
                    )
                continue
            if parsed is None:
                continue

            # Only complete values become candidates. Complete Composite
            # timestamps are valid evidence but are marked read-only; their
            # existing writable source components are normalized later.
            if parsed.local_datetime is not None:
                association_scope = None
                if groups:
                    family_zero = groups[0]
                    family_one = next(
                        (
                            group_name
                            for group_name in groups[1:]
                            if not group_name.casefold().startswith("copy")
                        ),
                        family_zero,
                    )
                    association_scope = (family_zero, family_one)

                file_record["capture_candidates"].append(
                    {
                        "date_type": 1,
                        "datetime": parsed.local_datetime,
                        "source": metadata_key,
                        "tag_name": tag_name,
                        "association_scope": association_scope,
                        "offset_minutes": parsed.utc_offset_minutes,
                        "fraction": parsed.fraction,
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
                except ValueError:
                    # FileModifyDate is a filesystem pseudo-tag rather than an
                    # embedded ExifTool target. Keep the invalid value as a
                    # structured repair record so the user may explicitly approve
                    # whole-second system-time alignment after final consensus.
                    file_record["invalid_capture_records"].append(
                        {
                            "metadata_key": "File:System:FileModifyDate",
                            "groups": ("File", "System"),
                            "tag_name": "FileModifyDate",
                            "raw_value": modification_value,
                            "kind": "file_modify",
                            "repair_approved": False,
                        }
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
                                "tag_name": "FileModifyDate",
                                "association_scope": None,
                                "offset_minutes": None,
                                "fraction": parsed_modify.fraction,
                                "kind": "file_modify",
                            }
                        )
                    else:
                        # A known FileModifyDate tag with an unrecognized value is
                        # still invalid, even when it does not match one of the
                        # supported date/time syntaxes closely enough to raise a
                        # calendar/clock ValueError.
                        file_record["invalid_capture_records"].append(
                            {
                                "metadata_key": "File:System:FileModifyDate",
                                "groups": ("File", "System"),
                                "tag_name": "FileModifyDate",
                                "raw_value": modification_value,
                                "kind": "file_modify",
                                "repair_approved": False,
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
    Establish one capture-time choice from timestamp, offset, and DST evidence.

    Timestamps remain exact—including parsed subseconds—while the configured
    threshold decides whether nearby values may share one temporal option. Two
    otherwise-close values are separate choices when their explicit UTC offsets
    or DST states differ. Missing state is treated as unknown rather than a
    contradiction, so a naive sidecar or FileModifyDate may support any compatible
    explicit state but cannot erase a genuine metadata conflict.

    Inline offsets belong directly to their timestamp. Separate OffsetTime* fields
    are paired only inside the same ExifTool family/namespace. Camera-global state
    is represented as real provenance-preserving bundles and may supplement naive
    timestamps anywhere in the same primary file. Partial bundles borrow a missing
    value only when file-wide evidence is unique; explicit alternatives remain
    separate. UTC counterparts derive offset-only evidence from the clock difference.
    """
    primary_capture_media = set(related_files_metadata["primary_capture_media"])
    candidate_variants = []

    for source_file, file_record in related_files_metadata["files"].items():
        associated_offsets_by_scope_and_tag = {}
        for (
            association_scope,
            source_name,
            tag_name,
            raw_value,
        ) in file_record["associated_offset_records"]:
            associated_offsets_by_scope_and_tag.setdefault(
                (association_scope, tag_name),
                [],
            ).append((source_name, raw_value))

        # --------------------------------------------------------------------
        # BUILD REAL FILE-GLOBAL CAMERA-STATE BUNDLES
        #
        # A maker-note timezone and DST switch describe the camera clock for the
        # physical file, even when DateTimeOriginal itself lives in EXIF. Values
        # from one metadata family are therefore kept together. Partial bundles
        # may borrow a missing dimension from another family only when that value
        # is unique across the file; explicit alternatives are never cross-multiplied.
        # --------------------------------------------------------------------
        global_bundles = []
        if source_file in primary_capture_media:
            records_by_scope = {}
            for (
                provenance_scope,
                source_name,
                tag_name,
                raw_value,
            ) in file_record.get("global_context_records", []):
                records_by_scope.setdefault(provenance_scope, []).append(
                    (source_name, tag_name, raw_value)
                )

            for provenance_scope, scoped_records in records_by_scope.items():
                values_by_tag = {}
                for source_name, tag_name, raw_value in scoped_records:
                    values_by_tag.setdefault(tag_name, []).append(
                        (source_name, raw_value)
                    )

                offsets_by_value = {}
                for tag_name in GLOBAL_OFFSET_TAGS:
                    for source_name, raw_value in values_by_tag.get(tag_name, []):
                        offset_minutes = parse_metadata_offset_minutes(
                            tag_name,
                            raw_value,
                        )
                        if offset_minutes is None:
                            continue
                        offsets_by_value.setdefault(offset_minutes, []).append(
                            (
                                source_file,
                                source_name,
                                str(raw_value).strip(),
                                offset_minutes,
                                "metadata offset",
                            )
                        )

                active_profiles = resolve_active_timezone_profiles(values_by_tag)
                active_dst_tags = set(DIRECT_DST_TAGS)
                active_dst_tags.update(
                    active_dst_tag for _, _, active_dst_tag, _ in active_profiles
                )
                dst_by_state = {}
                for tag_name in active_dst_tags:
                    for source_name, raw_value in values_by_tag.get(tag_name, []):
                        dst_state = parse_daylight_saving_value(raw_value)
                        if dst_state is None:
                            continue
                        dst_by_state.setdefault(dst_state, []).append(
                            (source_file, source_name, dst_state)
                        )

                descriptive_context = []
                for source_name, raw_value in values_by_tag.get(
                    "TimeZoneCity",
                    [],
                ):
                    descriptive_context.append(
                        (source_file, f"{source_name}={raw_value}")
                    )
                for (
                    selector_tag,
                    active_profile,
                    _,
                    context_tags,
                ) in active_profiles:
                    for context_tag in context_tags:
                        for source_name, raw_value in values_by_tag.get(
                            context_tag,
                            [],
                        ):
                            descriptive_context.append(
                                (
                                    source_file,
                                    f"{source_name}={raw_value} ({active_profile})",
                                )
                            )
                    for source_name, raw_value in values_by_tag.get(
                        selector_tag,
                        [],
                    ):
                        descriptive_context.append(
                            (
                                source_file,
                                f"{source_name}={raw_value} -> {active_profile}",
                            )
                        )
                descriptive_context = list(dict.fromkeys(descriptive_context))

                offset_values = sorted(offsets_by_value)
                dst_values = sorted(dst_by_state)
                if len(offset_values) <= 1 or len(dst_values) <= 1:
                    # One unique value in either dimension can safely accompany
                    # every explicit alternative in the other dimension.
                    for offset_minutes in offset_values or [None]:
                        for dst_state in dst_values or [None]:
                            if offset_minutes is None and dst_state is None:
                                continue
                            global_bundles.append(
                                {
                                    "offset_minutes": offset_minutes,
                                    "dst_state": dst_state,
                                    "offset_records": list(
                                        offsets_by_value.get(offset_minutes, [])
                                    ),
                                    "dst_records": list(
                                        dst_by_state.get(dst_state, [])
                                    ),
                                    "context_records": list(descriptive_context),
                                    "provenance": ("global", provenance_scope),
                                }
                            )
                else:
                    # Several offsets and several DST values in one family have
                    # no reliable pairing information. Preserve them as partial
                    # alternatives instead of inventing every combination.
                    for offset_minutes in offset_values:
                        global_bundles.append(
                            {
                                "offset_minutes": offset_minutes,
                                "dst_state": None,
                                "offset_records": list(
                                    offsets_by_value[offset_minutes]
                                ),
                                "dst_records": [],
                                "context_records": list(descriptive_context),
                                "provenance": ("global", provenance_scope),
                            }
                        )
                    for dst_state in dst_values:
                        global_bundles.append(
                            {
                                "offset_minutes": None,
                                "dst_state": dst_state,
                                "offset_records": [],
                                "dst_records": list(dst_by_state[dst_state]),
                                "context_records": list(descriptive_context),
                                "provenance": ("global", provenance_scope),
                            }
                        )

            # A missing dimension may be supplied file-wide only when all global
            # evidence agrees on one value. This supports cameras that split
            # timezone and DST across families without hiding real conflicts.
            unique_global_offsets = {
                bundle["offset_minutes"]
                for bundle in global_bundles
                if bundle["offset_minutes"] is not None
            }
            unique_global_dst_states = {
                bundle["dst_state"]
                for bundle in global_bundles
                if bundle["dst_state"] is not None
            }
            all_offset_records = [
                record
                for bundle in global_bundles
                for record in bundle["offset_records"]
            ]
            all_dst_records = [
                record for bundle in global_bundles for record in bundle["dst_records"]
            ]
            for bundle in global_bundles:
                if bundle["offset_minutes"] is None and len(unique_global_offsets) == 1:
                    bundle["offset_minutes"] = next(iter(unique_global_offsets))
                    bundle["offset_records"] = list(dict.fromkeys(all_offset_records))
                if bundle["dst_state"] is None and len(unique_global_dst_states) == 1:
                    bundle["dst_state"] = next(iter(unique_global_dst_states))
                    bundle["dst_records"] = list(dict.fromkeys(all_dst_records))

            merged_global_bundles = {}
            for bundle in global_bundles:
                state_key = (bundle["offset_minutes"], bundle["dst_state"])
                merged = merged_global_bundles.setdefault(
                    state_key,
                    {
                        "offset_minutes": bundle["offset_minutes"],
                        "dst_state": bundle["dst_state"],
                        "offset_records": [],
                        "dst_records": [],
                        "context_records": [],
                        "provenance": [],
                    },
                )
                for collection_name in (
                    "offset_records",
                    "dst_records",
                    "context_records",
                ):
                    for record in bundle[collection_name]:
                        if record not in merged[collection_name]:
                            merged[collection_name].append(record)
                if bundle["provenance"] not in merged["provenance"]:
                    merged["provenance"].append(bundle["provenance"])
            global_bundles = list(merged_global_bundles.values())

        for candidate in file_record["capture_candidates"]:
            candidate_specific_bundles = []

            if (
                candidate["kind"] != "file_modify"
                and candidate["offset_minutes"] is not None
            ):
                candidate_specific_bundles.append(
                    {
                        "offset_minutes": candidate["offset_minutes"],
                        "dst_state": None,
                        "offset_records": [
                            (
                                source_file,
                                candidate["source"],
                                "UTC" + format_offset(candidate["offset_minutes"]),
                                candidate["offset_minutes"],
                                "timestamp offset",
                            )
                        ],
                        "dst_records": [],
                        "context_records": [],
                        "provenance": ("timestamp", candidate["source"]),
                    }
                )

            associated_offset_tag = ASSOCIATED_OFFSET_TAGS.get(candidate["tag_name"])
            if associated_offset_tag is not None:
                for source_name, raw_value in associated_offsets_by_scope_and_tag.get(
                    (candidate["association_scope"], associated_offset_tag),
                    [],
                ):
                    offset_minutes = parse_metadata_offset_minutes(
                        associated_offset_tag,
                        raw_value,
                    )
                    if offset_minutes is None:
                        continue
                    candidate_specific_bundles.append(
                        {
                            "offset_minutes": offset_minutes,
                            "dst_state": None,
                            "offset_records": [
                                (
                                    source_file,
                                    source_name,
                                    str(raw_value).strip(),
                                    offset_minutes,
                                    "associated timestamp offset",
                                )
                            ],
                            "dst_records": [],
                            "context_records": [],
                            "provenance": (
                                "associated",
                                candidate["association_scope"],
                            ),
                        }
                    )

            if source_file in primary_capture_media:
                for source_name, utc_datetime in file_record["utc_references"]:
                    derived_offset = int(
                        (candidate["datetime"] - utc_datetime).total_seconds() / 60
                    )
                    if -12 * 60 <= derived_offset <= 14 * 60:
                        candidate_specific_bundles.append(
                            {
                                "offset_minutes": derived_offset,
                                "dst_state": None,
                                "offset_records": [
                                    (
                                        source_file,
                                        source_name,
                                        utc_datetime.strftime("%Y-%m-%d %H:%M:%S UTC"),
                                        derived_offset,
                                        "UTC counterpart",
                                    )
                                ],
                                "dst_records": [],
                                "context_records": [],
                                "provenance": ("utc", source_name),
                            }
                        )

            # Equivalent timestamp-specific offset evidence is one state with
            # several supporting records, not several duplicate variants.
            merged_specific_bundles = {}
            for bundle in candidate_specific_bundles:
                state_key = (bundle["offset_minutes"], bundle["dst_state"])
                merged = merged_specific_bundles.setdefault(
                    state_key,
                    {
                        "offset_minutes": bundle["offset_minutes"],
                        "dst_state": bundle["dst_state"],
                        "offset_records": [],
                        "dst_records": [],
                        "context_records": [],
                        "provenance": [],
                    },
                )
                for record in bundle["offset_records"]:
                    if record not in merged["offset_records"]:
                        merged["offset_records"].append(record)
                if bundle["provenance"] not in merged["provenance"]:
                    merged["provenance"].append(bundle["provenance"])
            candidate_specific_bundles = list(merged_specific_bundles.values())

            candidate_state_bundles = []
            if candidate_specific_bundles and global_bundles:
                for specific_bundle in candidate_specific_bundles:
                    compatible_globals = [
                        global_bundle
                        for global_bundle in global_bundles
                        if (
                            specific_bundle["offset_minutes"] is None
                            or global_bundle["offset_minutes"] is None
                            or specific_bundle["offset_minutes"]
                            == global_bundle["offset_minutes"]
                        )
                        and (
                            specific_bundle["dst_state"] is None
                            or global_bundle["dst_state"] is None
                            or specific_bundle["dst_state"]
                            == global_bundle["dst_state"]
                        )
                    ]
                    if compatible_globals:
                        for global_bundle in compatible_globals:
                            candidate_state_bundles.append(
                                {
                                    "offset_minutes": (
                                        specific_bundle["offset_minutes"]
                                        if specific_bundle["offset_minutes"] is not None
                                        else global_bundle["offset_minutes"]
                                    ),
                                    "dst_state": (
                                        specific_bundle["dst_state"]
                                        if specific_bundle["dst_state"] is not None
                                        else global_bundle["dst_state"]
                                    ),
                                    "offset_records": list(
                                        dict.fromkeys(
                                            specific_bundle["offset_records"]
                                            + global_bundle["offset_records"]
                                        )
                                    ),
                                    "dst_records": list(
                                        dict.fromkeys(
                                            specific_bundle["dst_records"]
                                            + global_bundle["dst_records"]
                                        )
                                    ),
                                    "context_records": list(
                                        global_bundle["context_records"]
                                    ),
                                }
                            )
                    else:
                        candidate_state_bundles.append(specific_bundle)

                # An incompatible global configuration is real conflicting
                # evidence for the same physical file and remains selectable.
                for global_bundle in global_bundles:
                    if not any(
                        (
                            specific_bundle["offset_minutes"] is None
                            or global_bundle["offset_minutes"] is None
                            or specific_bundle["offset_minutes"]
                            == global_bundle["offset_minutes"]
                        )
                        and (
                            specific_bundle["dst_state"] is None
                            or global_bundle["dst_state"] is None
                            or specific_bundle["dst_state"]
                            == global_bundle["dst_state"]
                        )
                        for specific_bundle in candidate_specific_bundles
                    ):
                        candidate_state_bundles.append(global_bundle)
            elif candidate_specific_bundles:
                candidate_state_bundles = candidate_specific_bundles
            elif global_bundles:
                candidate_state_bundles = global_bundles
            else:
                candidate_state_bundles = [
                    {
                        "offset_minutes": None,
                        "dst_state": None,
                        "offset_records": [],
                        "dst_records": [],
                        "context_records": [],
                    }
                ]

            merged_candidate_states = {}
            for bundle in candidate_state_bundles:
                state_key = (bundle["offset_minutes"], bundle["dst_state"])
                merged = merged_candidate_states.setdefault(
                    state_key,
                    {
                        "offset_minutes": bundle["offset_minutes"],
                        "dst_state": bundle["dst_state"],
                        "offset_records": [],
                        "dst_records": [],
                        "context_records": [],
                    },
                )
                for collection_name in (
                    "offset_records",
                    "dst_records",
                    "context_records",
                ):
                    for record in bundle.get(collection_name, []):
                        if record not in merged[collection_name]:
                            merged[collection_name].append(record)

            whole_datetime = candidate["datetime"].replace(microsecond=0)
            epoch = datetime(1970, 1, 1)
            elapsed = whole_datetime - epoch
            whole_seconds = elapsed.days * 86400 + elapsed.seconds
            exact_fraction = candidate.get("fraction")
            if exact_fraction is None and candidate["datetime"].microsecond:
                exact_fraction = f"{candidate['datetime'].microsecond:06d}"
            candidate_time_value = Decimal(whole_seconds)
            if exact_fraction:
                candidate_time_value += Decimal(f"0.{exact_fraction}")

            for bundle in merged_candidate_states.values():
                candidate_variants.append(
                    {
                        **candidate,
                        "source_file": source_file,
                        "time_value": candidate_time_value,
                        "state_offset_minutes": bundle["offset_minutes"],
                        "state_dst": bundle["dst_state"],
                        "offset_records": bundle["offset_records"],
                        "dst_records": bundle["dst_records"],
                        "context_records": bundle["context_records"],
                    }
                )

    sorted_variants = sorted(
        candidate_variants,
        key=lambda variant: (
            variant["time_value"],
            variant["source_file"].name.casefold(),
            str(variant["source"]).casefold(),
            (
                variant["state_offset_minutes"]
                if variant["state_offset_minutes"] is not None
                else 10**9
            ),
            variant["state_dst"] if variant["state_dst"] is not None else 2,
        ),
    )

    temporal_clusters = []
    current_cluster = []
    current_cluster_start = None
    for variant in sorted_variants:
        if current_cluster and variant["time_value"] - current_cluster_start > Decimal(
            CAPTURE_TIME_CONFLICT_THRESHOLD_SECONDS
        ):
            temporal_clusters.append(current_cluster)
            current_cluster = []
            current_cluster_start = None
        if not current_cluster:
            current_cluster_start = variant["time_value"]
        current_cluster.append(variant)
    if current_cluster:
        temporal_clusters.append(current_cluster)

    consensus_options = []
    kind_priority = {"complete": 0, "composite": 1, "file_modify": 2}
    for temporal_cluster in temporal_clusters:
        actual_state_choices = []
        for variant in temporal_cluster:
            state_choice = (
                variant["state_offset_minutes"],
                variant["state_dst"],
            )
            if state_choice not in actual_state_choices:
                actual_state_choices.append(state_choice)

        explicit_state_choices = [
            state_choice
            for state_choice in actual_state_choices
            if state_choice != (None, None)
        ]
        state_choices = explicit_state_choices or [(None, None)]

        # A partial state is supporting evidence for a more complete actual
        # state when their known dimensions agree. It must not become a separate
        # option, but two unrelated partial states are never combined into a new
        # complete state merely because they are mutually compatible.
        reduced_state_choices = []
        for state_choice in state_choices:
            known_dimensions = sum(value is not None for value in state_choice)
            if any(
                other_choice != state_choice
                and sum(value is not None for value in other_choice) > known_dimensions
                and all(
                    value is None or other_value is None or value == other_value
                    for value, other_value in zip(state_choice, other_choice)
                )
                for other_choice in state_choices
            ):
                continue
            reduced_state_choices.append(state_choice)

        for offset_minutes, dst_state in reduced_state_choices:
            compatible_variants = [
                variant
                for variant in temporal_cluster
                if (
                    variant["state_offset_minutes"] is None
                    or offset_minutes is None
                    or variant["state_offset_minutes"] == offset_minutes
                )
                and (
                    variant["state_dst"] is None
                    or dst_state is None
                    or variant["state_dst"] == dst_state
                )
            ]
            if not compatible_variants:
                continue

            records_by_exact_time = {}
            for variant in compatible_variants:
                records_by_exact_time.setdefault(
                    variant["time_value"],
                    [],
                ).append(variant)

            representative_time = min(
                records_by_exact_time,
                key=lambda candidate_time: (
                    -len(
                        {
                            variant["source_file"]
                            for variant in records_by_exact_time[candidate_time]
                        }
                    ),
                    -len(
                        {
                            variant["source_file"]
                            for variant in records_by_exact_time[candidate_time]
                            if variant["kind"] != "file_modify"
                        }
                    ),
                    -len(
                        {
                            variant["source_file"]
                            for variant in records_by_exact_time[candidate_time]
                            if variant["kind"] == "complete"
                        }
                    ),
                    candidate_time,
                ),
            )
            representative_variant = min(
                records_by_exact_time[representative_time],
                key=lambda variant: (
                    kind_priority[variant["kind"]],
                    variant["source_file"].name.casefold(),
                    str(variant["source"]).casefold(),
                    variant.get("fraction") or "",
                ),
            )

            option = {
                "datetime": representative_variant["datetime"],
                "fraction": representative_variant.get("fraction"),
                "date_type": max(
                    variant["date_type"] for variant in compatible_variants
                ),
                "offset_minutes": offset_minutes,
                "dst_state": dst_state,
                "sources": [],
                "offset_records": [],
                "dst_records": [],
                "context_records": [],
                "earliest_time_value": min(
                    variant["time_value"] for variant in compatible_variants
                ),
                "latest_time_value": max(
                    variant["time_value"] for variant in compatible_variants
                ),
            }
            for variant in compatible_variants:
                source_record = (
                    variant["source_file"],
                    variant["source"],
                )
                if source_record not in option["sources"]:
                    option["sources"].append(source_record)
                for collection_name in (
                    "offset_records",
                    "dst_records",
                    "context_records",
                ):
                    for record in variant.get(collection_name, []):
                        if record not in option[collection_name]:
                            option[collection_name].append(record)
            consensus_options.append(option)

    if not consensus_options:
        for source_file in related_files_metadata["usable_files"]:
            related_files_metadata["review_reasons"].setdefault(
                source_file,
                "no usable capture date/time",
            )
        return {
            "datetime": None,
            "fraction": None,
            "date_type": None,
            "offset_records": [],
            "dst_records": [],
            "context_records": [],
            "preserve_timezone_metadata": False,
            "preferred_utc_offset_minutes": None,
        }

    sorted_options = sorted(
        consensus_options,
        key=lambda option: (
            option["datetime"],
            option["offset_minutes"] if option["offset_minutes"] is not None else 10**9,
            option["dst_state"] if option["dst_state"] is not None else 2,
        ),
    )
    if len(sorted_options) == 1:
        selected_option = sorted_options[0]
    else:
        print()
        print("=" * 72)
        print("CAPTURE-STATE CONFLICT")
        print("=" * 72)
        print("Related files:")
        for source_file in related_files:
            print(f"  - {source_file.name}")
        print("\nSelect the authoritative timestamp, UTC offset, and DST state:")
        for option_number, option in enumerate(sorted_options, start=1):
            state_labels = []
            if option["offset_minutes"] is not None:
                state_labels.append("UTC" + format_offset(option["offset_minutes"]))
            if option["dst_state"] is not None:
                state_labels.append("DST " + ("ON" if option["dst_state"] else "OFF"))
            state_text = " " + ", ".join(state_labels) if state_labels else ""
            cluster_span = option["latest_time_value"] - option["earliest_time_value"]
            tolerance_label = (
                "" if cluster_span == 0 else f"; source span {cluster_span:g}s"
            )
            print(
                f"  {option_number}. "
                f"{format_consensus_datetime(option['datetime'], option['fraction'])}"
                f"{state_text} - {format_labels_by_file(option['sources'])}"
                f"{tolerance_label}"
            )

        while True:
            raw_selection = input(f"Selection [1-{len(sorted_options)}]: ").strip()
            try:
                selected_number = int(raw_selection)
            except ValueError:
                print("Invalid selection. Enter one of the listed numbers.")
                continue
            if 1 <= selected_number <= len(sorted_options):
                selected_option = sorted_options[selected_number - 1]
                selected_state_labels = []
                if selected_option["offset_minutes"] is not None:
                    selected_state_labels.append(
                        "UTC" + format_offset(selected_option["offset_minutes"])
                    )
                if selected_option["dst_state"] is not None:
                    selected_state_labels.append(
                        "DST " + ("ON" if selected_option["dst_state"] else "OFF")
                    )
                selected_state_suffix = (
                    " | " + ", ".join(selected_state_labels)
                    if selected_state_labels
                    else ""
                )
                print(
                    "\nSelected capture state: "
                    + format_consensus_datetime(
                        selected_option["datetime"],
                        selected_option["fraction"],
                    )
                    + selected_state_suffix
                )
                print()
                break
            print("Invalid selection. Enter one of the listed numbers.")

    return {
        "datetime": selected_option["datetime"],
        "fraction": selected_option["fraction"],
        "date_type": selected_option["date_type"],
        "offset_records": selected_option["offset_records"],
        "dst_records": selected_option["dst_records"],
        "context_records": selected_option["context_records"],
        "preserve_timezone_metadata": False,
        "preferred_utc_offset_minutes": selected_option["offset_minutes"],
    }


# ============================================================================
# MAJOR OPERATION 3: REVIEW AND CORRECT TIMEZONE/DST
# ============================================================================


def review_and_correct_timezone_and_dst(
    capture_time_consensus,
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

    dst_records = list(capture_time_consensus.get("dst_records", []))
    offset_records = list(capture_time_consensus.get("offset_records", []))
    display_records = list(capture_time_consensus.get("context_records", []))

    # Consensus already selected one provenance-preserving evidence bundle.
    # Review consumes that immutable state directly and never reopens raw file
    # metadata, so rejected alternatives cannot leak back into the prompt.

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
    # ------------------------------------------------------------------------
    # DISPLAY CONTRADICTORY OR AMBIGUOUS EVIDENCE
    # ------------------------------------------------------------------------
    print()
    print("=" * 72)
    print("TIMEZONE / DAYLIGHT-SAVING REVIEW")
    print("=" * 72)
    print(f"  Assumed IANA timezone: {timezone_name}")
    print(
        "  Selected local time: "
        + format_consensus_datetime(
            selected_datetime, capture_time_consensus.get("fraction")
        )
    )

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
            f"{format_consensus_datetime(corrected_datetime, capture_time_consensus.get('fraction'))}"
        )
        print()
        capture_time_consensus["datetime"] = corrected_datetime
        capture_time_consensus["date_type"] = 2
        capture_time_consensus["preserve_timezone_metadata"] = False
        return

    print("\nChoose an action:")
    print(
        "  1. Keep "
        + format_consensus_datetime(
            selected_datetime, capture_time_consensus.get("fraction")
        )
        + " and preserve all existing timezone/DST metadata"
    )
    for option_number, (corrected_datetime, reasons) in enumerate(
        correction_options,
        start=2,
    ):
        print(
            f"  {option_number}. Correct to "
            f"{format_consensus_datetime(corrected_datetime, capture_time_consensus.get('fraction'))} "
            f"({'; '.join(reasons)})"
        )

    while True:
        raw_selection = input(f"Selection [1-{len(correction_options) + 1}]: ").strip()
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
    Normalize one newly created classified copy from one final capture state.

    Every writable ExifTool target is grouped before any command is formatted.
    Duplicate extraction views therefore become one semantic target with one role
    and one final value. A target that appears to have incompatible semantic roles
    indicates an internal mapping error; correction of the new copy aborts instead
    of guessing, silently skipping, or asking the user to resolve implementation
    details after the authoritative capture state has already been selected.

    Existing local capture fields follow the final wall clock. Existing UTC fields
    are changed only when an invalid value was explicitly approved for repair, and
    then receive the corresponding UTC instant. Associated and camera-global
    offsets/DST settings are normalized only when timezone review did not preserve
    them. Missing fields are never created. Composite, File, System, inactive
    profile, editing/history/runtime, and other read-only/excluded fields are not
    direct ExifTool targets.

    One combined ExifTool write is attempted first, with individual retries when a
    read-only maker field blocks the batch. Every operation is reread and verified
    semantically. FileModifyDate is handled independently through ``os.utime``.
    """
    selected_datetime = capture_time_consensus["datetime"]
    if selected_datetime is None:
        return [], []
    selected_fraction = capture_time_consensus.get("fraction")
    preserve_timezone_metadata = capture_time_consensus.get(
        "preserve_timezone_metadata",
        False,
    )

    updated = []
    skipped = set()
    records = file_record["records"]

    valid_final_states = timezone_states_for_local_time(
        selected_datetime,
        classification_timezone,
    )
    valid_final_offsets = {state[1] for state in valid_final_states}
    preferred_offset = capture_time_consensus.get("preferred_utc_offset_minutes")
    if preferred_offset in valid_final_offsets:
        expected_offset = preferred_offset
    elif len(valid_final_offsets) == 1:
        expected_offset = next(iter(valid_final_offsets))
    else:
        expected_offset = None
    expected_state = valid_final_states[0] if len(valid_final_states) == 1 else None

    values_by_tag = {}
    for metadata_key, groups, tag_name, raw_value in records:
        values_by_tag.setdefault(tag_name, []).append((metadata_key, groups, raw_value))

    active_dst_tags = set(DIRECT_DST_TAGS)
    active_dst_tags.update(
        active_dst_tag
        for _, _, active_dst_tag, _ in resolve_active_timezone_profiles(values_by_tag)
    )

    invalid_by_metadata_key = {
        invalid_record["metadata_key"]: invalid_record
        for invalid_record in file_record.get("invalid_capture_records", [])
    }

    # ------------------------------------------------------------------------
    # GROUP PHYSICAL WRITABLE TARGETS BEFORE DERIVING ANY WRITE COMMAND
    # ------------------------------------------------------------------------
    target_groups = {}
    for metadata_key, groups, tag_name, raw_value in records:
        target = get_metadata_write_target(groups, tag_name)
        if target is None:
            continue
        target_key = target.removesuffix("#")
        target_groups.setdefault(target_key, []).append(
            {
                "metadata_key": metadata_key,
                "groups": groups,
                "tag_name": tag_name,
                "raw_value": raw_value,
                "target": target,
                "invalid_record": invalid_by_metadata_key.get(metadata_key),
            }
        )

    operations = []
    for target_key in sorted(target_groups):
        target_records = target_groups[target_key]
        tag_names = {record["tag_name"] for record in target_records}
        if len(tag_names) != 1:
            raise OSError(
                "internal metadata write-plan error: target "
                f"'{target_key}' maps to several tag roles: "
                + ", ".join(sorted(tag_names))
            )
        tag_name = next(iter(tag_names))

        # Prefer the richest extracted representation when duplicate views expose
        # different formatting. This affects only serialization; semantic values
        # always come from the final selected capture state.
        def representation_rank(record):
            text = str(record["raw_value"]).strip()
            date_match = COMPLETE_DATE_TIME_PATTERN.fullmatch(text)
            time_match = TIME_ONLY_PATTERN.fullmatch(text)
            match = date_match or time_match
            has_offset = bool(match and match.group("timezone") is not None)
            fraction_length = len(match.group("fraction") or "") if match else 0
            return (
                0 if has_offset else 1,
                -fraction_length,
                record["metadata_key"].casefold(),
            )

        representative = min(target_records, key=representation_rank)
        raw_value = representative["raw_value"]
        operation = None

        # --------------------------------------------------------------------
        # INVALID UTC REFERENCES: EXPLICIT REPAIR TO THE FINAL UTC INSTANT
        # --------------------------------------------------------------------
        if tag_name in UTC_REFERENCE_TAGS:
            approved_invalid_records = [
                record["invalid_record"]
                for record in target_records
                if record["invalid_record"] is not None
                and record["invalid_record"].get("repair_approved")
            ]
            if not approved_invalid_records or preserve_timezone_metadata:
                continue
            if expected_offset is None:
                skipped.add(f"{target_key} (ambiguous final UTC offset)")
                continue

            invalid_kinds = {
                invalid_record["kind"] for invalid_record in approved_invalid_records
            }
            if len(invalid_kinds) != 1:
                raise OSError(
                    "internal metadata write-plan error: target "
                    f"'{target_key}' has incompatible UTC component roles"
                )
            invalid_kind = next(iter(invalid_kinds))
            invalid_record = min(
                approved_invalid_records,
                key=lambda record: record["metadata_key"].casefold(),
            )
            raw_value = invalid_record["raw_value"]

            aware_selected = selected_datetime.replace(
                tzinfo=timezone(timedelta(minutes=expected_offset))
            )
            selected_utc_datetime = aware_selected.astimezone(timezone.utc).replace(
                tzinfo=None
            )

            try:
                if invalid_kind == "utc_datetime":
                    complete_match = COMPLETE_DATE_TIME_PATTERN.fullmatch(
                        str(raw_value).strip()
                    )
                    if complete_match is not None:
                        target_offset = (
                            0 if complete_match.group("timezone") is not None else None
                        )
                        expected_text = format_complete_datetime(
                            raw_value,
                            selected_utc_datetime,
                            target_offset,
                            selected_fraction,
                        )
                    else:
                        expected_text = selected_utc_datetime.strftime(
                            "%Y:%m:%d %H:%M:%S"
                        )
                        if selected_fraction:
                            expected_text += "." + selected_fraction
                    expected_parsed = parse_metadata_datetime(expected_text)
                    expected = (
                        expected_parsed.local_datetime,
                        expected_parsed.utc_offset_minutes,
                    )
                    operation_kind = "datetime"
                elif invalid_kind == "utc_date":
                    if DATE_ONLY_PATTERN.fullmatch(str(raw_value).strip()) is not None:
                        expected_text = format_date_only(
                            raw_value, selected_utc_datetime
                        )
                    else:
                        expected_text = selected_utc_datetime.strftime("%Y:%m:%d")
                    expected = selected_utc_datetime.date()
                    operation_kind = "date"
                elif invalid_kind == "utc_time":
                    time_match = TIME_ONLY_PATTERN.fullmatch(str(raw_value).strip())
                    if time_match is not None:
                        target_offset = 0 if time_match.group("timezone") else None
                        expected_text = format_time_only(
                            raw_value,
                            selected_utc_datetime,
                            target_offset,
                            selected_fraction,
                        )
                    else:
                        expected_text = selected_utc_datetime.strftime("%H:%M:%S")
                        if selected_fraction:
                            expected_text += "." + selected_fraction
                    expected_parsed = parse_metadata_datetime(expected_text)
                    expected = (
                        expected_parsed.local_time,
                        expected_parsed.utc_offset_minutes,
                    )
                    operation_kind = "time"
                else:
                    raise ValueError(f"unsupported UTC repair kind: {invalid_kind}")
            except (TypeError, ValueError):
                skipped.add(f"{target_key} (could not format approved UTC repair)")
                continue

            operation = {
                "target": representative["target"],
                "value": expected_text,
                "kind": operation_kind,
                "expected": expected,
                "tag_name": tag_name,
            }

        # --------------------------------------------------------------------
        # OFFSETS AND DST SETTINGS: ONE FILE/TARGET-LEVEL FINAL VALUE
        # --------------------------------------------------------------------
        elif tag_name in ASSOCIATED_OFFSET_FIELDS or tag_name in GLOBAL_OFFSET_TAGS:
            if preserve_timezone_metadata:
                continue
            if tag_name in GLOBAL_OFFSET_TAGS and not file_record["is_capture_media"]:
                continue
            if expected_offset is None:
                skipped.add(f"{target_key} (ambiguous consensus UTC offset)")
                continue

            actual_offsets = {
                parse_metadata_offset_minutes(tag_name, record["raw_value"])
                for record in target_records
            }
            if actual_offsets == {expected_offset}:
                continue

            raw_target = tag_name == "TimeZoneOffset" and isinstance(
                raw_value, (int, float)
            )
            operation = {
                "target": get_metadata_write_target(
                    representative["groups"],
                    tag_name,
                    raw=raw_target,
                ),
                "value": format_metadata_offset_value(
                    tag_name,
                    raw_value,
                    expected_offset,
                ),
                "kind": "offset",
                "expected": expected_offset,
                "tag_name": tag_name,
            }

        elif tag_name in DIRECT_DST_TAGS or tag_name in PROFILE_DST_TAGS:
            if (
                preserve_timezone_metadata
                or not file_record["is_capture_media"]
                or tag_name not in active_dst_tags
            ):
                continue
            if expected_state is None:
                skipped.add(f"{target_key} (ambiguous final DST state)")
                continue

            expected_dst = expected_state[0]
            actual_dst_states = {
                parse_daylight_saving_value(record["raw_value"])
                for record in target_records
            }
            if actual_dst_states == {expected_dst}:
                continue

            dst_vocabulary = str(raw_value).strip().casefold()
            if dst_vocabulary in {"yes", "no"}:
                target_dst_value = "Yes" if expected_dst else "No"
            elif dst_vocabulary in {"enabled", "disabled"}:
                target_dst_value = "Enabled" if expected_dst else "Disabled"
            elif dst_vocabulary in {"daylight saving", "standard time"}:
                target_dst_value = (
                    "Daylight Saving" if expected_dst else "Standard Time"
                )
            else:
                target_dst_value = "On" if expected_dst else "Off"

            operation = {
                "target": representative["target"],
                "value": target_dst_value,
                "kind": "dst",
                "expected": expected_dst,
                "tag_name": tag_name,
            }

        elif tag_name in TIME_CONTEXT_TAGS or tag_name in EXCLUDED_CAPTURE_TIME_TAGS:
            continue

        # --------------------------------------------------------------------
        # LOCAL CAPTURE FIELDS: ONE ROLE AND ONE VALUE PER PHYSICAL TARGET
        # --------------------------------------------------------------------
        else:
            valid_representations = []
            approved_invalid_representations = []
            unapproved_invalid_present = False

            for record in target_records:
                invalid_record = record["invalid_record"]
                try:
                    parsed = parse_metadata_datetime(record["raw_value"])
                except ValueError:
                    parsed = None

                if parsed is not None:
                    if parsed.local_datetime is not None:
                        role = "datetime"
                    elif parsed.local_date is not None:
                        role = "date"
                    elif parsed.local_time is not None:
                        role = "time"
                    else:
                        continue
                    valid_representations.append((role, record, parsed))
                    continue

                if invalid_record is None:
                    continue
                if (
                    invalid_record.get("repair_approved")
                    and not preserve_timezone_metadata
                ):
                    invalid_role = invalid_record["kind"]
                    if invalid_role in {"datetime", "date", "time"}:
                        approved_invalid_representations.append(
                            (invalid_role, record, invalid_record)
                        )
                else:
                    unapproved_invalid_present = True

            roles = {
                role
                for role, _, _ in (
                    valid_representations + approved_invalid_representations
                )
            }
            if not roles:
                # Declined/read-only invalid values remain warnings, not failed
                # correction operations.
                continue
            if len(roles) != 1:
                raise OSError(
                    "internal metadata write-plan error: target "
                    f"'{target_key}' has incompatible local time roles: "
                    + ", ".join(sorted(roles))
                )
            role = next(iter(roles))

            role_valid = [entry for entry in valid_representations if entry[0] == role]
            role_invalid = [
                entry for entry in approved_invalid_representations if entry[0] == role
            ]
            if role_valid:
                _, representative, parsed = min(
                    role_valid,
                    key=lambda entry: representation_rank(entry[1]),
                )
                raw_value = representative["raw_value"]
            else:
                _, representative, _ = min(
                    role_invalid,
                    key=lambda entry: representation_rank(entry[1]),
                )
                raw_value = representative["raw_value"]
                parsed = None

            try:
                if role == "datetime":
                    match = COMPLETE_DATE_TIME_PATTERN.fullmatch(str(raw_value).strip())
                    if match is None:
                        skipped.add(
                            f"{target_key} (could not preserve datetime syntax)"
                        )
                        continue
                    existing_offset = (
                        parsed.utc_offset_minutes if parsed is not None else None
                    )
                    has_inline_offset = match.group("timezone") is not None
                    target_offset = (
                        (
                            existing_offset
                            if preserve_timezone_metadata
                            else expected_offset
                        )
                        if has_inline_offset
                        else None
                    )
                    if has_inline_offset and target_offset is None:
                        skipped.add(f"{target_key} (ambiguous consensus UTC offset)")
                        continue
                    expected_text = format_complete_datetime(
                        raw_value,
                        selected_datetime,
                        target_offset,
                        selected_fraction,
                    )
                    expected_parsed = parse_metadata_datetime(expected_text)
                    expected = (
                        expected_parsed.local_datetime,
                        expected_parsed.utc_offset_minutes,
                    )
                    if parsed is not None and (
                        parsed.local_datetime,
                        parsed.utc_offset_minutes,
                        parsed.fraction,
                    ) == (
                        expected_parsed.local_datetime,
                        expected_parsed.utc_offset_minutes,
                        expected_parsed.fraction,
                    ):
                        continue

                elif role == "date":
                    expected_text = format_date_only(raw_value, selected_datetime)
                    expected = selected_datetime.date()
                    if parsed is not None and parsed.local_date == expected:
                        continue

                else:
                    match = TIME_ONLY_PATTERN.fullmatch(str(raw_value).strip())
                    if match is None:
                        skipped.add(f"{target_key} (could not preserve time syntax)")
                        continue
                    existing_offset = (
                        parsed.utc_offset_minutes if parsed is not None else None
                    )
                    has_inline_offset = match.group("timezone") is not None
                    target_offset = (
                        (
                            existing_offset
                            if preserve_timezone_metadata
                            else expected_offset
                        )
                        if has_inline_offset
                        else None
                    )
                    if has_inline_offset and target_offset is None:
                        skipped.add(f"{target_key} (ambiguous consensus UTC offset)")
                        continue
                    expected_text = format_time_only(
                        raw_value,
                        selected_datetime,
                        target_offset,
                        selected_fraction,
                    )
                    expected_parsed = parse_metadata_datetime(expected_text)
                    expected = (
                        expected_parsed.local_time,
                        expected_parsed.utc_offset_minutes,
                    )
                    if parsed is not None and (
                        parsed.local_time,
                        parsed.utc_offset_minutes,
                        parsed.fraction,
                    ) == (
                        expected_parsed.local_time,
                        expected_parsed.utc_offset_minutes,
                        expected_parsed.fraction,
                    ):
                        continue
            except (TypeError, ValueError):
                skipped.add(f"{target_key} (could not format final capture value)")
                continue

            operation = {
                "target": representative["target"],
                "value": expected_text,
                "kind": role,
                "expected": expected,
                "tag_name": tag_name,
            }

        if operation is not None:
            if operation["target"] is None:
                raise OSError(
                    "internal metadata write-plan error: no writable target for "
                    f"'{target_key}'"
                )
            operations.append(operation)

    # One grouped target can emit at most one operation by construction.
    operation_targets = [
        operation["target"].removesuffix("#") for operation in operations
    ]
    if len(operation_targets) != len(set(operation_targets)):
        raise OSError(
            "internal metadata write-plan error: a target was planned more than once"
        )

    # ------------------------------------------------------------------------
    # EXECUTE, RETRY INDIVIDUALLY, AND VERIFY SEMANTIC RESULTS
    # ------------------------------------------------------------------------
    if operations:
        write_arguments = list(CORRECTION_WRITE_PARAMS)
        write_arguments.extend(
            f"-{operation['target']}={operation['value']}" for operation in operations
        )
        write_arguments.append(str(copied_file))
        try:
            metadata_reader.execute(*write_arguments)
        except ExifToolException:
            for operation in operations:
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

        for operation in operations:
            target = operation["target"].removesuffix("#")
            matched = False
            expected = operation["expected"]
            for actual_value in actual_by_target.get(target, []):
                try:
                    if operation["kind"] == "datetime":
                        parsed = parse_metadata_datetime(actual_value)
                        expected_datetime, expected_offset_value = expected
                        matched = (
                            parsed is not None
                            and parsed.local_datetime is not None
                            and parsed.local_datetime.replace(microsecond=0)
                            == expected_datetime.replace(microsecond=0)
                            and parsed.utc_offset_minutes == expected_offset_value
                        )
                    elif operation["kind"] == "date":
                        parsed = parse_metadata_datetime(actual_value)
                        matched = parsed is not None and parsed.local_date == expected
                    elif operation["kind"] == "time":
                        parsed = parse_metadata_datetime(actual_value)
                        expected_time, expected_offset_value = expected
                        matched = (
                            parsed is not None
                            and parsed.local_time is not None
                            and parsed.local_time.replace(microsecond=0)
                            == expected_time.replace(microsecond=0)
                            and parsed.utc_offset_minutes == expected_offset_value
                        )
                    elif operation["kind"] == "offset":
                        matched = (
                            parse_metadata_offset_minutes(
                                operation["tag_name"],
                                actual_value,
                            )
                            == expected
                        )
                    elif operation["kind"] == "dst":
                        matched = parse_daylight_saving_value(actual_value) == expected
                except (TypeError, ValueError):
                    matched = False
                if matched:
                    break

            if matched:
                updated.append(target)
            else:
                skipped.add(f"{target} (not writable or verification failed)")

    # ------------------------------------------------------------------------
    # ALIGN FILEMODIFYDATE FOR CLASSIFIED MEDIA, WITH INVALID-VALUE CONSENT
    # ------------------------------------------------------------------------
    invalid_file_modify_records = [
        invalid_record
        for invalid_record in file_record.get("invalid_capture_records", [])
        if invalid_record["kind"] == "file_modify"
    ]
    file_modify_repair_allowed = not invalid_file_modify_records or (
        not preserve_timezone_metadata
        and any(
            invalid_record.get("repair_approved")
            for invalid_record in invalid_file_modify_records
        )
    )
    if file_record["is_capture_media"] and file_modify_repair_allowed:
        if expected_offset is None:
            skipped.add("FileModifyDate (ambiguous consensus UTC offset)")
        else:
            filesystem_fraction = selected_fraction
            if filesystem_fraction is None and selected_datetime.microsecond:
                filesystem_fraction = f"{selected_datetime.microsecond:06d}"
            fraction_nanoseconds = int(
                ((filesystem_fraction or "") + "000000000")[:9] or "0"
            )
            aware_selected_datetime = selected_datetime.replace(
                microsecond=0,
                tzinfo=timezone(timedelta(minutes=expected_offset)),
            )
            selected_utc_datetime = aware_selected_datetime.astimezone(timezone.utc)
            epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
            epoch_delta = selected_utc_datetime - epoch
            selected_mtime_ns = (
                epoch_delta.days * 86400 + epoch_delta.seconds
            ) * 1_000_000_000 + fraction_nanoseconds
            if (
                copied_file.stat().st_mtime_ns // 1_000_000_000
                != selected_mtime_ns // 1_000_000_000
            ):
                os.utime(
                    copied_file,
                    ns=(time.time_ns(), selected_mtime_ns),
                )
                if (
                    copied_file.stat().st_mtime_ns // 1_000_000_000
                    != selected_mtime_ns // 1_000_000_000
                ):
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
        file_record = related_files_metadata["files"].get(source_file)
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
                            and copied_file.stat().st_mtime_ns // 1_000_000_000
                            == candidate.stat().st_mtime_ns // 1_000_000_000
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

        embedded_updates = [
            target for target in updated_targets if target != "FileModifyDate"
        ]
        system_time_aligned = "FileModifyDate" in updated_targets

        if copied:
            stats["copied"] += 1
            if renamed:
                stats["renamed"] += 1
                copy_result = f"copied as {final_destination.name}"
            else:
                copy_result = "copied"
        else:
            stats["duplicates"] += 1
            copy_result = f"existing duplicate reused: {final_destination.name}"

        if embedded_updates and not reused_corrected_duplicate:
            stats["metadata_updated"] += 1
        if system_time_aligned and not reused_corrected_duplicate:
            stats["system_time_updated"] += 1
        if skipped_targets and not reused_corrected_duplicate:
            stats["metadata_skipped"] += 1

        warning_messages = list(file_record.get("warnings", [])) if file_record else []
        invalid_records = (
            file_record.get("invalid_capture_records", []) if file_record else []
        )
        if invalid_records:
            invalid_labels = ", ".join(
                invalid_record["metadata_key"] for invalid_record in invalid_records
            )
            approved_targets = {
                target.removesuffix("#")
                for invalid_record in invalid_records
                if invalid_record.get("repair_approved")
                for target in [
                    (
                        "FileModifyDate"
                        if invalid_record["kind"] == "file_modify"
                        else get_metadata_write_target(
                            invalid_record["groups"],
                            invalid_record["tag_name"],
                        )
                    )
                ]
                if target is not None
            }
            if approved_targets and approved_targets.issubset(set(updated_targets)):
                invalid_status = "repaired in the classified copy"
            elif approved_targets:
                invalid_status = "repair requested; unresolved targets are listed below"
            else:
                invalid_status = "left unchanged"
            warning_messages.append(
                f"invalid date/time metadata ({invalid_labels}); {invalid_status}"
            )

        print()
        report_label = "REVIEW" if source_file in review_reasons else "CLASSIFIED"
        print(f"[{report_label}] {source_file.name}")
        print(f"  Destination: {folder_name}/{final_destination.name}")
        print(f"  Copy result: {copy_result}")
        if reused_corrected_duplicate:
            print("  Correction state: existing corrected duplicate reused")
        else:
            if embedded_updates:
                print("  Embedded metadata updated: " + ", ".join(embedded_updates))
            if system_time_aligned:
                print("  System time aligned: FileModifyDate")
            if skipped_targets:
                print("  Skipped/read-only: " + ", ".join(skipped_targets))

        if warning_messages:
            stats["warnings"] += 1
            print("  Warnings:")
            for warning_message in warning_messages:
                print(f"    - {warning_message}")

        if source_file in review_reasons:
            stats["review"] += 1
            print(f"  Review reason: {review_reasons[source_file]}")
        else:
            print(
                "  Capture state: "
                + format_consensus_datetime(
                    selected_datetime,
                    capture_time_consensus.get("fraction"),
                )
                + f" ({DATETYPE[capture_time_consensus['date_type']]})"
            )


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
    while True:
        timezone_name = (
            input(
                f"Timezone for timezone/DST checks [{DEFAULT_TIMEZONE_NAME}]: "
            ).strip()
            or DEFAULT_TIMEZONE_NAME
        )
        try:
            classification_timezone = ZoneInfo(timezone_name)
            break
        except ZoneInfoNotFoundError:
            print(
                f"Unknown timezone '{timezone_name}'. Enter an IANA name such "
                "as Europe/Berlin, Europe/London, or America/New_York."
            )

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
        "system_time_updated": 0,
        "metadata_skipped": 0,
        "warnings": 0,
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
                timezone_name,
                classification_timezone,
                automatic_correction_rules,
            )

            # ----------------------------------------------------------------
            # 3B. OFFER REPAIR OF INVALID WRITABLE FIELDS USING THE FINAL STATE
            #
            # An invalid value never becomes a consensus candidate. Repair is
            # offered only when another valid capture state exists and only after
            # timezone review has established the final timestamp. This applies to
            # local, UTC-reference, and FileModifyDate values. A Keep decision
            # leaves every invalid field untouched, including inline offset text.
            # ----------------------------------------------------------------
            if capture_time_consensus["datetime"] is not None:
                for source_file in related_files:
                    file_record = related_files_metadata["files"].get(source_file)
                    if file_record is None:
                        continue
                    invalid_records = file_record["invalid_capture_records"]
                    if not invalid_records:
                        continue

                    print()
                    print("=" * 72)
                    print("INVALID CAPTURE METADATA")
                    print("=" * 72)
                    print(f"  File: {source_file.name}")
                    print(
                        "  Final capture state: "
                        + format_consensus_datetime(
                            capture_time_consensus["datetime"],
                            capture_time_consensus.get("fraction"),
                        )
                    )
                    print("  Invalid fields:")
                    for invalid_record in invalid_records:
                        print(
                            f"    - {invalid_record['metadata_key']}="
                            f"{invalid_record['raw_value']!r}"
                        )

                    writable_records = [
                        invalid_record
                        for invalid_record in invalid_records
                        if (
                            invalid_record["kind"] == "file_modify"
                            and file_record["is_capture_media"]
                        )
                        or get_metadata_write_target(
                            invalid_record["groups"],
                            invalid_record["tag_name"],
                        )
                        is not None
                    ]
                    if capture_time_consensus.get("preserve_timezone_metadata"):
                        print(
                            "  Action: left unchanged because Keep was selected "
                            "for the timezone/DST review."
                        )
                        continue
                    if not writable_records:
                        print("  Action: no invalid field has a writable target.")
                        continue

                    while True:
                        repair_selection = (
                            input(
                                "  Repair writable invalid fields in the classified "
                                "copy using the final capture state? [y/N]: "
                            )
                            .strip()
                            .casefold()
                        )
                        if repair_selection in {"", "n", "no"}:
                            print("  Action: invalid fields will remain unchanged.")
                            break
                        if repair_selection in {"y", "yes"}:
                            for invalid_record in writable_records:
                                invalid_record["repair_approved"] = True
                            print("  Action: repair approved for the classified copy.")
                            break
                        print("  Invalid selection. Enter y or n.")

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
    print(f"Files with metadata warnings: {stats['warnings']}")
    print(f"Copies with embedded metadata updates: {stats['metadata_updated']}")
    print(f"Copies with FileModifyDate aligned: {stats['system_time_updated']}")
    print("Copies with skipped/read-only corrections: " f"{stats['metadata_skipped']}")
    print("Original source files were not modified.")
    input("Press Enter to exit")
    return 2 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
