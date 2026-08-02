import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from exiftool import ExifToolHelper
from exiftool.exceptions import ExifToolException, ExifToolExecuteError


DATETYPE = {0: "OS_DATE", 1: "METADATA"}

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
COMPLETE_DATE_TIME_PATTERN = re.compile(
    r"^\d{4}[:-]\d{2}[:-]\d{2}[ T]"
    r"\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?$"
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


def parse_complete_metadata_datetime(metadata_value):
    """
    Parse one complete ExifTool date/time value without inventing components.

    The embedded metadata pass keeps ExifTool's raw values. Date-only values
    such as IPTC DateCreated and time-only values such as IPTC TimeCreated do
    not match COMPLETE_DATE_TIME_PATTERN and return None. Composite fields that
    combine both components remain eligible.

    Timezone offsets and fractional seconds are accepted. The classifier keeps
    its existing wall-clock comparison and folder behavior by removing timezone
    information after parsing.
    """
    date_text = str(metadata_value).strip()
    if not COMPLETE_DATE_TIME_PATTERN.fullmatch(date_text):
        return None

    normalized_date_text = date_text

    # datetime.fromisoformat expects hyphens in the calendar portion. ExifTool
    # commonly uses EXIF's YYYY:MM:DD form, so normalize only those separators.
    if normalized_date_text[4] == ":" and normalized_date_text[7] == ":":
        normalized_date_text = (
            normalized_date_text[:4]
            + "-"
            + normalized_date_text[5:7]
            + "-"
            + normalized_date_text[8:]
        )

    if normalized_date_text[10] == " ":
        normalized_date_text = (
            normalized_date_text[:10]
            + "T"
            + normalized_date_text[11:]
        )

    if normalized_date_text.endswith("Z"):
        normalized_date_text = normalized_date_text[:-1] + "+00:00"

    # Accept offsets written as +HHMM in addition to +HH:MM.
    if re.search(r"[+-]\d{4}$", normalized_date_text):
        normalized_date_text = (
            normalized_date_text[:-2]
            + ":"
            + normalized_date_text[-2:]
        )

    parsed_date = datetime.fromisoformat(normalized_date_text)
    return parsed_date.replace(tzinfo=None)


def get_dates(metadata_reader, filename):
    """
    Return (date_candidates, review_reason, file_is_image).

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
            error_message = error.stderr.strip() or str(error)
            return (
                [],
                f"ExifTool could not inspect the image ({error_message})",
                True,
            )

        # Sidecars and other related files may legitimately contain no ExifTool
        # metadata. They remain usable members of the group but provide no date.
        return [], None, False
    except ExifToolException as error:
        raise OSError(
            f"ExifTool could not read metadata from '{filename}': {error}"
        ) from error

    if not metadata_results:
        if extension_is_image:
            return [], "ExifTool returned no metadata for the image", True

        return [], None, False

    metadata = metadata_results[0]
    file_is_image = is_image_file(filename, metadata)
    date_candidates = []
    invalid_date_messages = []

    for metadata_key, groups, tag_name, metadata_value in iter_metadata_values(
        metadata
    ):
        # These tags support file identification but are not dates.
        if tag_name in {"FileType", "MIMEType"}:
            continue

        # The ExifTool request already excludes the family-1 System group and
        # the semantic exclusions above. Retain both checks as safety invariants
        # in case a future ExifTool version returns an explicitly excluded tag.
        if "System" in groups or tag_name in EXCLUDED_TIME_TAGS:
            continue

        try:
            metadata_date = parse_complete_metadata_datetime(metadata_value)
        except ValueError:
            # A value with the complete date/time shape but invalid calendar
            # data remains a review case. Partial date/time values return None
            # and are simply not standalone candidates.
            invalid_date_messages.append(
                f"{metadata_key}={metadata_value!r}"
            )
            continue

        if metadata_date is None:
            continue

        date_candidates.append(
            (1, metadata_date, metadata_key)
        )

    if invalid_date_messages and file_is_image:
        return (
            [],
            "invalid metadata date: " + "; ".join(invalid_date_messages),
            True,
        )

    return date_candidates, None, file_is_image


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
    review_reasons = {}
    selected_file_date = None

    # First pass: collect only embedded metadata timestamps from every related
    # file. Sidecars may contribute embedded XMP/IPTC metadata, but no file in
    # this pass contributes filesystem timestamps.
    for same_stem_file in same_stem_files:
        try:
            date_candidates, review_reason, file_is_image = get_dates(
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
        for date_type, date_value, source_name in date_candidates:
            date_option = date_options.setdefault(
                date_value,
                {"date_type": date_type, "sources": []},
            )
            date_option["date_type"] = max(
                date_option["date_type"],
                date_type,
            )
            date_option["sources"].append(
                (same_stem_file, source_name)
            )

    if len(date_options) == 1:
        # Every available field agrees on one timestamp, so no user interaction
        # is required. Use the strongest source associated with that timestamp.
        date_value, date_option = next(iter(date_options.items()))
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
