"""
Classify files by their best available timestamp without modifying the sources.

The script scans one selected directory without recursion, groups related files,
reads an explicit set of metadata timestamps through one persistent ExifTool
process, resolves conflicting timestamps interactively, and copies each file to
either a date-based classified directory or an unclassified review directory.
Existing output files are never overwritten: identical copies are reused and
non-identical name collisions receive numeric suffixes.
"""

import re
import shutil
import sys
from datetime import date, datetime, time
from pathlib import Path

from exiftool import ExifToolHelper
from exiftool.exceptions import ExifToolException, ExifToolExecuteError


# Constants and metadata-reading policy

VERSION = "0.3.2"

# A larger value represents a stronger timestamp source when several sources
# agree on the same datetime. Filesystem modification time remains a fallback;
# approved embedded metadata is considered authoritative when available.
DATETYPE = {0: "OS_DATE", 1: "EXIF"}

# This is an inclusion list, not a broad metadata query followed by filtering.
# Only these reviewed fields may enter timestamp comparison and classification.
# Additional fields must be added explicitly when their semantics are approved.
METADATA_TIME_TAGS = [
    "ExifIFD:DateTimeOriginal",
    "ExifIFD:CreateDate",
    "IFD0:ModifyDate",
]

# These non-timestamp fields determine whether ExifTool identified an image and
# whether it reported a metadata-reading problem. They preserve the distinction
# between an ordinary non-image file, which may use filesystem modification
# time, and a damaged or unreadable recognized image, which requires review.
EXIFTOOL_FILE_STATUS_TAGS = [
    "File:MIMEType",
    "ExifTool:Error",
]

# The persistent ExifTool process uses the same arguments for every file:
#
# - -G:0:1:2:7 retains the approved group families in each returned key so the
#   complete metadata path remains available for record identity and conflict
#   reporting.
# - -a allows ExifTool to return duplicate tag instances instead of selecting
#   only one value.
# - -e suppresses generated Composite tags so only directly requested evidence
#   is considered.
# - -ee3 examines supported embedded metadata structures at the selected depth.
#
# Print conversion remains enabled. No global -n, -d, or QuickTimeUTC option is
# applied because those options could alter or impose timestamp interpretation.
EXIFTOOL_COMMON_ARGS = [
    "-G:0:1:2:7",
    "-a",
    "-e",
    "-ee3",
]

# Accepted metadata values contain a complete date and time, optional fractional
# seconds, and either an optional numeric offset or explicit Z/z UTC notation.
# Fractional seconds are deliberately non-capturing because classification uses
# whole-second precision. The offset and UTC groups remain distinct so +00:00
# is not collapsed into an explicit Z representation.
METADATA_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<year>\d{4}):"
    r"(?P<month>\d{2}):"
    r"(?P<day>\d{2}) "
    r"(?P<hour>\d{2}):"
    r"(?P<minute>\d{2}):"
    r"(?P<second>\d{2})"
    r"(?:\.\d+)?"
    r"(?: ?(?:(?P<offset>[+-]\d{2}:\d{2})|(?P<utc>[Zz])))?$"
)

CLASSIFIED_FOLDER_NAME = "classified"
UNCLASSIFIED_FOLDER_NAME = "unclassified"

# Binary comparison and copying use bounded chunks so large files are never
# loaded fully into memory.
COPY_CHUNK_SIZE = 1024 * 1024

# This static set is the case-folded equivalent of the extension keys returned
# by PIL.Image.registered_extensions() in version 0.2.0. It is intentionally
# retained as a compatibility boundary: if ExifTool cannot identify a file with
# one of these extensions, the file is treated as a potentially damaged image
# and sent for review instead of being classified as an ordinary non-image file.
IMAGE_EXTENSIONS = {
    ".apng",
    ".avif",
    ".avifs",
    ".blp",
    ".bmp",
    ".bufr",
    ".bw",
    ".cur",
    ".dcx",
    ".dds",
    ".dib",
    ".emf",
    ".eps",
    ".fit",
    ".fits",
    ".flc",
    ".fli",
    ".ftc",
    ".ftu",
    ".gbr",
    ".gif",
    ".grib",
    ".h5",
    ".hdf",
    ".icb",
    ".icns",
    ".ico",
    ".iim",
    ".im",
    ".j2c",
    ".j2k",
    ".jfif",
    ".jp2",
    ".jpc",
    ".jpe",
    ".jpeg",
    ".jpf",
    ".jpg",
    ".jpx",
    ".mpeg",
    ".mpg",
    ".mpo",
    ".msp",
    ".palm",
    ".pbm",
    ".pcd",
    ".pcx",
    ".pdf",
    ".pfm",
    ".pgm",
    ".png",
    ".pnm",
    ".ppm",
    ".ps",
    ".psd",
    ".pxr",
    ".qoi",
    ".ras",
    ".rgb",
    ".rgba",
    ".sgi",
    ".tga",
    ".tif",
    ".tiff",
    ".vda",
    ".vst",
    ".webp",
    ".wmf",
    ".xbm",
    ".xpm",
}

# Metadata parsing and reading


def parse_metadata_timestamp(metadata_value):
    """
    Parse one complete ExifTool timestamp into separate semantic components.

    ``metadata_value`` may contain whole seconds, any number of fractional
    digits, an optional signed ``HH:MM`` offset, or explicit ``Z``/``z`` UTC
    notation. Fractional digits are accepted but discarded. Numeric offsets are
    returned as signed minutes; explicit UTC is returned separately with no
    numeric offset so ``+00:00`` and ``Z`` remain distinguishable.

    Return ``(parsed_date, parsed_time, offset_minutes, utc)``. Raise
    ``ValueError`` when the syntax, calendar date, clock time, or offset is
    invalid. The function performs no filesystem access and changes no metadata.
    """
    value_text = str(metadata_value).strip()
    timestamp_match = METADATA_TIMESTAMP_PATTERN.fullmatch(value_text)

    if timestamp_match is None:
        raise ValueError(
            f"unsupported metadata timestamp format: {metadata_value!r}"
        )

    # Constructing date and time objects performs calendar and clock validation.
    # Fractional seconds matched by the pattern are intentionally not passed to
    # time(), so every accepted value is normalized to whole-second precision.
    parsed_date = date(
        int(timestamp_match.group("year")),
        int(timestamp_match.group("month")),
        int(timestamp_match.group("day")),
    )
    parsed_time = time(
        int(timestamp_match.group("hour")),
        int(timestamp_match.group("minute")),
        int(timestamp_match.group("second")),
    )

    offset_text = timestamp_match.group("offset")
    utc = timestamp_match.group("utc") is not None

    # Missing timezone information and explicit UTC are both represented with
    # offset_minutes=None, but the utc flag distinguishes those two meanings.
    if offset_text is None:
        offset_minutes = None
    else:
        offset_hours = int(offset_text[1:3])
        offset_remainder_minutes = int(offset_text[4:6])

        # The regular expression validates shape only. Check numeric ranges
        # before converting the signed offset into total minutes.
        if offset_hours > 23 or offset_remainder_minutes > 59:
            raise ValueError(
                f"invalid UTC offset in metadata timestamp: "
                f"{metadata_value!r}"
            )

        sign = 1 if offset_text[0] == "+" else -1
        offset_minutes = sign * (
            offset_hours * 60 + offset_remainder_minutes
        )

    return parsed_date, parsed_time, offset_minutes, utc


def get_dates(metadata_reader, filename):
    """
    Read one file and return timestamps eligible for current classification.

    ``metadata_reader`` is the persistent ``ExifToolHelper`` owned by ``main``;
    ``filename`` is a regular source file from the non-recursive input snapshot.
    The function reads but never modifies the file or its metadata.

    Return ``(classification_timestamps, review_reason)``. Each classification
    timestamp is ``(date_type, datetime_value, source_name)``, where
    ``source_name`` is the complete ExifTool path. A non-``None`` review reason
    means the file must be copied to ``unclassified`` rather than classified.

    Every returned occurrence of an approved metadata field is first stored in
    ``metadata_timestamps``. Each path owns parallel lists; equal indexes across
    ``exiftool_values``, ``dates``, ``times``, ``offsets``, and ``utc_flags``
    describe the same ExifTool occurrence. Parsing finishes before appending so
    the lists cannot become desynchronized after an invalid value.

    The filesystem modification time is used only when no approved embedded
    timestamp is available or when the file is an ordinary non-image. A damaged,
    unreadable, or invalid recognized image is returned for manual review instead
    of being classified from potentially misleading fallback information.
    """
    # Extension recognition is independent from ExifTool MIME detection. It is
    # used only as a compatibility safeguard when metadata inspection fails or
    # cannot identify content that has a historically recognized image suffix.
    recognized_extension = filename.suffix.casefold() in IMAGE_EXTENSIONS

    # Phase 1: verify that the operating system permits direct source reads.
    # Permission failures are processing errors. Other read failures on a known
    # image extension require review; an ordinary non-image may still use its
    # filesystem modification time, preserving established fallback behavior.
    try:
        with filename.open("rb"):
            pass
    except PermissionError as error:
        raise OSError(f"cannot read '{filename}': {error}") from error
    except OSError as error:
        if recognized_extension:
            return [], f"could not inspect image metadata ({error})"
        modification_date = datetime.fromtimestamp(filename.stat().st_mtime)
        return [(0, modification_date, DATETYPE[0])], None

    # Phase 2: request only the approved timestamp and status fields through the
    # already-running ExifTool process. Execute errors may describe malformed
    # content; general helper errors indicate that metadata could not be read.
    try:
        metadata_results = metadata_reader.get_tags(
            files=filename,
            tags=METADATA_TIME_TAGS + EXIFTOOL_FILE_STATUS_TAGS,
        )
    except ExifToolExecuteError as error:
        if recognized_extension:
            return [], f"could not inspect image metadata ({error})"
        modification_date = datetime.fromtimestamp(filename.stat().st_mtime)
        return [(0, modification_date, DATETYPE[0])], None
    except ExifToolException as error:
        raise OSError(
            f"could not read metadata from '{filename}': {error}"
        ) from error

    # No result means ExifTool supplied no usable identification. Recognized
    # image extensions are reviewed; other files retain the filesystem fallback.
    if not metadata_results:
        if recognized_extension:
            return [], "ExifTool could not identify the image"
        modification_date = datetime.fromtimestamp(filename.stat().st_mtime)
        return [(0, modification_date, DATETYPE[0])], None

    # Phase 3: normalize the single-file ExifTool result without shortening any
    # returned path. PyExifTool values may be scalars or lists. Store one record
    # per non-null occurrence so duplicate values remain separate and ordered.
    metadata_records = []
    mime_type = None
    metadata_error = None

    for metadata_path, metadata_value in metadata_results[0].items():
        if metadata_path == "SourceFile":
            continue

        tag_name = metadata_path.rsplit(":", 1)[-1]
        metadata_values = (
            metadata_value
            if isinstance(metadata_value, list)
            else [metadata_value]
        )

        for value in metadata_values:
            if value is None:
                continue

            metadata_records.append((metadata_path, tag_name, value))

            # Status fields are matched by terminal name because every returned
            # dictionary key retains the complete selected ExifTool group path.
            # The first non-null status value is sufficient for routing.
            if tag_name == "MIMEType":
                if mime_type is None and isinstance(value, str):
                    mime_type = value
            elif tag_name == "Error" and metadata_error is None:
                metadata_error = value

    identified_image = (
        isinstance(mime_type, str)
        and mime_type.casefold().startswith("image/")
    )

    # ExifTool-reported errors route recognized images to review. For ordinary
    # non-images, the same error does not make embedded image metadata relevant,
    # so classification continues with filesystem modification time.
    if metadata_error is not None:
        if identified_image or recognized_extension:
            return [], f"could not inspect image metadata ({metadata_error})"
        modification_date = datetime.fromtimestamp(filename.stat().st_mtime)
        return [(0, modification_date, DATETYPE[0])], None

    # A known image extension without an image MIME type is treated as damaged
    # or unreadable. A file that is neither identified nor named as an image is
    # an ordinary non-image and uses filesystem modification time.
    if not identified_image:
        if recognized_extension:
            return [], "ExifTool could not identify the image"
        modification_date = datetime.fromtimestamp(filename.stat().st_mtime)
        return [(0, modification_date, DATETYPE[0])], None

    # Phase 4: parse all approved timestamp occurrences into one canonical
    # per-path representation. Terminal names select the approved fields, while
    # complete returned paths remain the authoritative identity and display name.
    metadata_time_names = {
        metadata_path.rsplit(":", 1)[-1]
        for metadata_path in METADATA_TIME_TAGS
    }
    metadata_timestamps = {}

    for metadata_path, tag_name, metadata_value in metadata_records:
        if tag_name not in metadata_time_names:
            continue

        try:
            (
                parsed_date,
                parsed_time,
                parsed_offset,
                parsed_utc,
            ) = parse_metadata_timestamp(metadata_value)
        except (TypeError, ValueError) as error:
            return [], (
                f"invalid {metadata_path} date "
                f"{metadata_value!r} ({error})"
            )

        path_parts = metadata_path.split(":")
        metadata_timestamp = metadata_timestamps.setdefault(
            metadata_path,
            {
                "path_components": path_parts[:-1],
                "tag_name": path_parts[-1],
                "exiftool_values": [],
                "dates": [],
                "times": [],
                "offsets": [],
                "utc_flags": [],
            },
        )

        # These parallel lists form one explicit invariant: the same index in
        # every list refers to the same occurrence returned for this exact path.
        metadata_timestamp["exiftool_values"].append(metadata_value)
        metadata_timestamp["dates"].append(parsed_date)
        metadata_timestamp["times"].append(parsed_time)
        metadata_timestamp["offsets"].append(parsed_offset)
        metadata_timestamp["utc_flags"].append(parsed_utc)

    # Phase 5: adapt complete metadata timestamps to the current classifier's
    # established tuple interface. Offsets and explicit UTC state are preserved
    # in metadata_timestamps but deliberately do not shift the local datetime.
    # Classification therefore uses the represented wall-clock date and time.
    classification_timestamps = []

    for metadata_path, metadata_timestamp in metadata_timestamps.items():
        for candidate_index, candidate_date in enumerate(
            metadata_timestamp["dates"]
        ):
            candidate_time = metadata_timestamp["times"][candidate_index]

            # The current classifier accepts only complete date-and-time values.
            # Any incomplete occurrence is not eligible for classification.
            if candidate_date is None or candidate_time is None:
                continue

            classification_timestamps.append(
                (
                    1,
                    datetime.combine(candidate_date, candidate_time),
                    metadata_path,
                )
            )

    if classification_timestamps:
        return classification_timestamps, None

    # An identified image with no approved embedded timestamp is valid rather
    # than damaged, so filesystem modification time remains its final fallback.
    modification_date = datetime.fromtimestamp(filename.stat().st_mtime)
    return [(0, modification_date, DATETYPE[0])], None


# Related-file grouping


def stems_are_related(base_stem, longer_stem):
    """
    Return whether ``longer_stem`` is a derivative of ``base_stem``.

    Comparison is case-insensitive. A derivative must begin with the complete
    base stem and add at least one character. Its first added character may be
    anything except an ASCII digit; this prevents numbered files such as
    ``IMG_00010`` from being attached to ``IMG_0001`` while allowing names such
    as ``IMG_0001pano`` and ``IMG_0001-edit``.
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
    Return deterministic groups of files that represent one related basename.

    ``files`` is the fixed, non-recursive source snapshot. Files sharing an
    exact stem begin in the same group. Longer derivative stems are then attached
    to the longest valid shorter base, preventing a broad prefix from capturing
    a more specific relationship. Both groups and files within each group are
    returned in case-insensitive name order. The input paths are not modified.
    """
    # First preserve exact-stem relationships, including sidecars and files
    # with different extensions but the same basename.
    files_by_stem = {}
    for filename in files:
        files_by_stem.setdefault(filename.stem, []).append(filename)

    # Process shorter stems first so every derivative can be compared against
    # all already-established base groups.
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
            # The longest match is the most specific base and avoids attaching a
            # derivative to an earlier, broader prefix.
            groups[max(matching_bases, key=len)].extend(files_by_stem[stem])
        else:
            groups[stem] = list(files_by_stem[stem])

    # Stable ordering keeps prompts and reports reproducible across runs.
    return [
        sorted(group, key=lambda path: path.name.casefold())
        for _, group in sorted(
            groups.items(),
            key=lambda item: item[0].casefold(),
        )
    ]


# Safe output copying


def files_are_binary_identical(first_file, second_file):
    """
    Return whether two regular files contain exactly the same bytes.

    The size check avoids unnecessary reads when lengths differ. Equal-sized
    files are compared in bounded chunks and neither file is loaded fully into
    memory. ``False`` is returned when ``second_file`` is not a regular file;
    filesystem read errors are allowed to propagate to the caller.
    """
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
    Copy one source file without overwriting any existing destination.

    Existing binary-identical files are reused as duplicates. A non-identical
    collision causes ``_1``, ``_2``, and later suffixes to be tried before the
    extension. The destination is opened in exclusive-create mode so another
    process cannot be overwritten after the preliminary existence check.

    Return ``(actual_destination, copied, renamed)``. ``copied`` is false only
    when an identical output already exists; ``renamed`` reports whether a
    numeric suffix was needed. Metadata and filesystem times are copied with
    ``shutil.copystat``. If copying fails after creating the destination, the
    incomplete output is removed while the source remains untouched.
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

        # Resolve a pre-existing name before attempting exclusive creation.
        # Identical content is a completed result, not a collision to duplicate.
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
            # Open the source read-only and the destination exclusively. The
            # exclusive mode is the final protection against race-condition
            # overwrites if another process creates the same name concurrently.
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
            # Another process claimed this candidate after the existence check.
            # Re-evaluate it on the next loop as either a duplicate or collision.
            continue

        except OSError:
            # A partially written destination must never be mistaken for a valid
            # prior copy on the next run. Cleanup failure is secondary to the
            # original copy error, which is re-raised after the best-effort unlink.
            if destination_created:
                try:
                    destination_file.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        return destination_file, True, suffix_number > 0


def main():
    """
    Run the interactive, non-destructive classification workflow.

    The function prompts for one source directory, validates and snapshots its
    direct regular files, groups related names, and opens one persistent
    ``ExifToolHelper`` context for all metadata reads. Each group is assigned one
    selected timestamp before its files are copied to ``classified/YYYY-MM-DD``;
    unreliable recognized images are copied to ``unclassified`` for review.

    The source files are never moved, renamed, deleted, or opened for writing.
    Return exit code 0 on complete success, 1 when setup or the ExifTool session
    cannot be established, and 2 when one or more individual files fail.
    """
    # Source-directory input and validation

    # The user may type a path or drag it from a graphical file manager. Only a
    # single matching outer quote pair is removed; all interior characters are
    # preserved so spaces and quote-like characters remain part of the path.
    raw_directory = input(
        "Please write (or drag) the source directory path: "
    ).strip()

    if (
        len(raw_directory) >= 2
        and raw_directory[0] == raw_directory[-1]
        and raw_directory[0] in {'"', "'"}
    ):
        raw_directory = raw_directory[1:-1]

    directory = Path(raw_directory).expanduser()

    # Resolve only after confirming that the path exists and is a directory.
    # Invalid input is a setup error and no output location is created.
    if not directory.exists() or not directory.is_dir():
        print(f"Error: source directory is invalid: '{directory}'")
        input("Press Enter to exit")
        return 1

    directory = directory.resolve()

    # Output-directory validation and source snapshot

    # The script writes copies only below these two fixed destinations:
    #
    #   classified/YYYY-MM-DD/  successfully classified copies
    #   unclassified/           files requiring manual metadata review
    #
    # Existing directories are reused. An ordinary file occupying either name
    # is a setup conflict because the required directory cannot be created.
    classified_directory = directory / CLASSIFIED_FOLDER_NAME
    unclassified_directory = directory / UNCLASSIFIED_FOLDER_NAME

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

        # Take one fixed, non-recursive snapshot before classification begins.
        # Output directories and every other subdirectory are excluded because
        # only direct regular, non-symlink files pass this filter. Newly created
        # copies therefore cannot enter the same run as new inputs.
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
        return 1

    # Related-file grouping and run counters

    # Related files are processed as a unit because image derivatives and
    # sidecars must receive the same selected date directory. Counters describe
    # output actions rather than source discovery totals.
    same_stem_groups = group_related_files(source_files)

    copied_count = 0
    duplicate_count = 0
    renamed_count = 0
    review_count = 0
    failed_count = 0

    # Persistent metadata session and per-group classification

    # Entering the context starts one stay-open ExifTool process after all setup
    # checks have passed. The same helper is passed to every get_dates() call.
    # Leaving the context terminates the process on normal completion or when an
    # exception exits the block, so no manual run()/terminate() pair is needed.
    try:
        with ExifToolHelper(
            common_args=EXIFTOOL_COMMON_ARGS
        ) as metadata_reader:
            # Each group follows one linear sequence: collect evidence, combine
            # equal timestamps, resolve conflicts, select destinations, and copy.
            for same_stem_files in same_stem_groups:
                file_dates = {}
                review_reasons = {}
                selected_file_date = None

                # Metadata collection for the complete related group

                # Read every file before deciding the group timestamp. This
                # exposes disagreements between different files and between
                # multiple approved fields inside a single file.
                for same_stem_file in same_stem_files:
                    try:
                        classification_timestamps, review_reason = get_dates(
                            metadata_reader,
                            same_stem_file,
                        )
                    except OSError as error:
                        # A processing failure prevents this file from being
                        # copied because neither classification nor review
                        # routing can be trusted. Other files continue.
                        print(
                            f"Error reading '{same_stem_file.name}': {error}",
                            file=sys.stderr,
                        )
                        failed_count += 1
                        continue

                    if review_reason is not None:
                        # Review files are excluded from timestamp consensus but
                        # retained for copying into the unclassified directory.
                        print(
                            f"Warning: '{same_stem_file.name}' requires review: "
                            f"{review_reason}",
                            file=sys.stderr,
                        )
                        review_reasons[same_stem_file] = review_reason
                        continue

                    # Preserve every eligible occurrence returned for the file.
                    # A single file may therefore contribute several distinct
                    # options when its approved metadata fields disagree.
                    file_dates[same_stem_file] = classification_timestamps

                # Timestamp consolidation across files and metadata fields

                # Use the datetime itself as the consensus key. Sources are not
                # part of that key: two fields containing the same timestamp
                # agree and should not force a prompt. Their file names and full
                # metadata paths are retained solely for explanation to the user.
                date_options = {}
                for same_stem_file, classification_timestamps in file_dates.items():
                    for (
                        date_type,
                        date_value,
                        source_name,
                    ) in classification_timestamps:
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

                # Authoritative timestamp selection

                if len(date_options) == 1:
                    # Complete agreement requires no interaction. If the same
                    # datetime came from both filesystem and metadata sources,
                    # retain the strongest source type for the final report.
                    date_value, date_option = next(iter(date_options.items()))
                    selected_file_date = (
                        date_option["date_type"],
                        date_value,
                    )

                elif len(date_options) > 1:
                    # Conflicts may be within one file, between related files, or
                    # both. Present one option per distinct datetime and list all
                    # contributing paths before accepting a bounded numeric choice.
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
                        date_sources = ", ".join(
                            f"{filename.name} ({source_name})"
                            for filename, source_name in date_option["sources"]
                        )
                        print(
                            f"  {option_number}. "
                            f"{date_value.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"- {date_sources}"
                        )

                    while True:
                        raw_selection = input(
                            "Enter a number from 1 to "
                            f"{len(sorted_date_options)}: "
                        ).strip()

                        try:
                            selected_option_number = int(raw_selection)
                        except ValueError:
                            print(
                                "Invalid selection. "
                                "Enter one of the listed numbers."
                            )
                            continue

                        if not 1 <= selected_option_number <= len(
                            sorted_date_options
                        ):
                            print(
                                "Invalid selection. "
                                "Enter one of the listed numbers."
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

                # Destination routing and non-destructive copying

                # Review routing has priority for the affected file. Other usable
                # files in the group share the one selected group timestamp. A
                # file that failed metadata reading is absent from both mappings
                # and is skipped because no safe destination was established.
                for same_stem_file in same_stem_files:
                    if same_stem_file in review_reasons:
                        folder_name = UNCLASSIFIED_FOLDER_NAME
                        destination_directory = unclassified_directory
                    elif (
                        same_stem_file in file_dates
                        and selected_file_date is not None
                    ):
                        date_folder_name = selected_file_date[1].strftime(
                            "%Y-%m-%d"
                        )
                        folder_name = (
                            f"{CLASSIFIED_FOLDER_NAME}/{date_folder_name}"
                        )
                        destination_directory = (
                            classified_directory / date_folder_name
                        )
                    else:
                        continue

                    # A file blocking a date-directory path is a per-file output
                    # failure. It does not justify changing or deleting that file.
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
                        destination_directory.mkdir(
                            parents=True,
                            exist_ok=True,
                        )
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

                    # Report the physical output action first, then append the
                    # review reason or selected timestamp used for routing.
                    print(
                        f"{same_stem_file.name}\t--->\t{folder_name}\t",
                        end="",
                    )

                    if copied:
                        copied_count += 1
                        if renamed:
                            renamed_count += 1
                            print(
                                f"COPIED AS {destination_file.name}",
                                end="",
                            )
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
                            "; "
                            f"{selected_file_date[1].strftime('%Y-%m-%d %H:%M:%S')} "
                            f"{DATETYPE[selected_file_date[0]]}",
                            end="",
                        )
                    print()

    except FileNotFoundError as error:
        # A missing executable can fail while entering the context, before the
        # classification body begins. Keeping this handler outside the ``with``
        # block lets startup failure be reported without catching unrelated
        # filesystem errors raised later by the classification workflow.
        print(f"Error starting ExifTool: {error}", file=sys.stderr)
        input("Press Enter to exit")
        return 1

    except ExifToolException as error:
        # PyExifTool may raise while entering, using, or leaving the persistent
        # session. The context manager remains responsible for process cleanup.
        # Expected per-file execution errors are handled inside get_dates().
        print(f"Error during ExifTool processing: {error}", file=sys.stderr)
        input("Press Enter to exit")
        return 1

    # Final report and exit status

    # Counts describe completed output decisions. Failed files are reported
    # separately and cause exit code 2, while successful and review copies remain
    # valid. The final statement reiterates the central source immutability rule.
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

    return 2 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
