"""
Classify related files by their best available datetime without modifying sources.

The script scans one selected directory without recursion, groups related files,
reads each complete group through one persistent ExifTool process, resolves
conflicting datetimes interactively, and copies every group member to either a
date-based classified directory or the unclassified review directory. Existing
output files are never overwritten: identical copies are reused and non-identical
name collisions receive numeric suffixes.
"""

import re
import shutil
import sys
from datetime import date, datetime, time
from pathlib import Path

from exiftool import ExifToolHelper
from exiftool.exceptions import ExifToolException, ExifToolExecuteError


# Constants and metadata-reading policy

VERSION = "0.3.3"

# This is an inclusion list, not a broad metadata query followed by filtering.
# Only these reviewed embedded datetime fields may enter classification. New
# fields must be added explicitly after their semantics and priority are agreed.
METADATA_DATETIME_TAGS = [
    "ExifIFD:DateTimeOriginal",
    "ExifIFD:CreateDate",
    "IFD0:ModifyDate",
]

# FileModifyDate is acquired in the same ExifTool call as embedded metadata for
# every successfully inspected file. It remains fallback-only in this version:
# it is used only when that file supplies no approved embedded datetime.
FILE_MODIFY_DATE_TAG = [
    "File:FileModifyDate",
]

# ExifTool-reported errors make the complete related group unreliable. MIME type
# and filename extension are deliberately not used to decide whether a file may
# contribute metadata or use FileModifyDate as its fallback.
EXIFTOOL_STATUS_TAGS = [
    "ExifTool:Error",
]

# The persistent ExifTool process uses the same arguments for every group:
#
# - -G:0:1:2:7 retains the approved group families in each returned key so the
#   complete metadata path remains available for record identity and reporting.
# - -a allows ExifTool to return duplicate tag instances instead of selecting
#   only one value.
# - -e suppresses generated Composite tags so only directly requested evidence
#   is considered.
# - -ee3 examines supported embedded metadata structures at the selected depth.
#
# Print conversion remains enabled. No global -n, -d, or QuickTimeUTC option is
# applied because those options could alter or impose datetime interpretation.
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
METADATA_DATETIME_PATTERN = re.compile(
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


# Metadata parsing, acquisition, and option construction


def parse_metadata_datetime(metadata_value):
    """
    Parse one complete ExifTool datetime into separate semantic components.

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
    datetime_match = METADATA_DATETIME_PATTERN.fullmatch(value_text)

    if datetime_match is None:
        raise ValueError(
            f"unsupported metadata datetime format: {metadata_value!r}"
        )

    # Constructing date and time objects performs calendar and clock validation.
    # Fractional seconds matched by the pattern are intentionally not passed to
    # time(), so every accepted value is normalized to whole-second precision.
    parsed_date = date(
        int(datetime_match.group("year")),
        int(datetime_match.group("month")),
        int(datetime_match.group("day")),
    )
    parsed_time = time(
        int(datetime_match.group("hour")),
        int(datetime_match.group("minute")),
        int(datetime_match.group("second")),
    )

    offset_text = datetime_match.group("offset")
    utc = datetime_match.group("utc") is not None

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
                f"invalid UTC offset in metadata datetime: "
                f"{metadata_value!r}"
            )

        sign = 1 if offset_text[0] == "+" else -1
        offset_minutes = sign * (
            offset_hours * 60 + offset_remainder_minutes
        )

    return parsed_date, parsed_time, offset_minutes, utc


def read_related_files_metadata(metadata_reader, related_files):
    """
    Read and normalize metadata for one complete related-file group.

    ``metadata_reader`` is the persistent ``ExifToolHelper`` owned by ``main``.
    ``related_files`` is one complete group from the fixed, non-recursive source
    snapshot. ExifTool receives every group member in one call and returns one
    dictionary per requested file. Each dictionary's ``SourceFile`` value binds
    the returned fields to their source file.

    Return a dictionary keyed first by source file and then by fully qualified
    ExifTool path. Each field retains its path components, terminal tag name,
    original ExifTool values, and synchronized lists reserved for parsed date,
    time, offset, and UTC values.

    Raise ``ValueError`` when ExifTool returns an incomplete, duplicated, or
    inconsistent group result, or reports an error for any group member.
    ``ExifToolExecuteError`` is allowed to propagate so ``main`` can route the
    complete group to review. Other ``ExifToolException`` failures remain under
    the persistent-session error boundary in ``main``.
    """
    exiftool_results = metadata_reader.get_tags(
        files=related_files,
        tags=(
            METADATA_DATETIME_TAGS
            + FILE_MODIFY_DATE_TAG
            + EXIFTOOL_STATUS_TAGS
        ),
    )

    # PyExifTool always returns a list containing one dictionary for each file
    # passed to get_tags(). Because this call submits the complete related group,
    # the result count must match the requested file count before any result is
    # trusted or parsed.
    if len(exiftool_results) != len(related_files):
        raise ValueError(
            f"ExifTool returned {len(exiftool_results)} file results "
            f"for a related group containing {len(related_files)} files"
        )

    # The requested names are converted once to their terminal tag names because
    # ExifTool returns fully qualified paths whose preceding components vary by
    # metadata family and embedded-document location.
    metadata_datetime_tag_names = {
        requested_tag.rsplit(":", 1)[-1]
        for requested_tag in METADATA_DATETIME_TAGS + FILE_MODIFY_DATE_TAG
    }

    related_files_metadata = {}

    for exiftool_result in exiftool_results:
        # SourceFile identifies which requested file produced this result. It is
        # ExifTool control information rather than datetime evidence, so it is
        # used for association and is not stored as a metadata field.
        source_file_value = exiftool_result.get("SourceFile")

        if source_file_value is None:
            raise ValueError(
                "ExifTool returned a related-file result without SourceFile"
            )

        source_file = Path(source_file_value)

        if source_file not in related_files:
            raise ValueError(
                f"ExifTool returned an unexpected source file: '{source_file}'"
            )

        # The directory snapshot cannot contain the same file twice. This check
        # instead verifies ExifTool's result contract: a duplicate result for one
        # file would mean another requested group member has no result.
        if source_file in related_files_metadata:
            raise ValueError(
                f"ExifTool returned more than one result for '{source_file}'"
            )

        source_file_metadata = {}

        for qualified_metadata_path, returned_value in exiftool_result.items():
            if qualified_metadata_path == "SourceFile":
                continue

            # PyExifTool may represent one value as a scalar and several values
            # as a list. Normalize both forms once at this input boundary so all
            # later processing uses the same ordered occurrence representation.
            returned_values = (
                returned_value
                if isinstance(returned_value, list)
                else [returned_value]
            )
            returned_values = [
                value for value in returned_values if value is not None
            ]

            if not returned_values:
                continue

            path_components = qualified_metadata_path.split(":")
            tag_name = path_components[-1]

            # Any ExifTool-reported error makes the complete related group
            # unreliable. Preserve every non-null reported value in the review
            # explanation rather than silently selecting only the first one.
            if tag_name == "Error":
                error_description = ", ".join(
                    repr(value) for value in returned_values
                )
                raise ValueError(
                    f"ExifTool reported an error for '{source_file.name}' at "
                    f"{qualified_metadata_path}: {error_description}"
                )

            # Only requested datetime evidence enters the normalized structure.
            # No broad result set is retained and filtered again downstream.
            if tag_name not in metadata_datetime_tag_names:
                continue

            source_file_metadata[qualified_metadata_path] = {
                "path_components": path_components[:-1],
                "tag_name": tag_name,
                "exiftool_values": returned_values,
                "dates": [],
                "times": [],
                "offsets": [],
                "utc_flags": [],
            }

        related_files_metadata[source_file] = source_file_metadata

    return related_files_metadata


def build_datetime_options(related_files_metadata):
    """
    Parse group metadata and return distinct classification datetime options.

    ``related_files_metadata`` is the normalized evidence returned by
    ``read_related_files_metadata``. Each source file is evaluated independently.
    Approved embedded datetimes have priority. ``FileModifyDate`` was already
    acquired for the file but is parsed and used only when that file supplies no
    approved embedded datetime.

    Return a dictionary keyed by combined ``datetime``. Each option retains a
    list of ``(source_file, qualified_metadata_path)`` tuples for every field
    occurrence supporting that value. Equal datetimes therefore agree while
    their exact contributing files and paths remain available for reporting.

    Raise ``ValueError`` when an evaluated datetime is invalid or when any source
    file has neither approved embedded datetime evidence nor a usable
    ``FileModifyDate``. Parsing appends all semantic components together so the
    synchronized lists in each metadata field cannot become misaligned.
    """
    # These terminal names identify embedded datetime fields. FileModifyDate is
    # deliberately excluded because it is considered only after this set yields
    # no candidate for the current source file.
    embedded_datetime_tag_names = {
        requested_tag.rsplit(":", 1)[-1]
        for requested_tag in METADATA_DATETIME_TAGS
    }

    datetime_options = {}

    for source_file, source_file_metadata in related_files_metadata.items():
        embedded_datetime_found = False

        # Parse every approved embedded datetime occurrence before considering
        # the fallback. A present invalid value makes the evidence unreliable;
        # it is not silently ignored in favor of another field or FileModifyDate.
        for qualified_metadata_path, metadata_field in source_file_metadata.items():
            if metadata_field["tag_name"] not in embedded_datetime_tag_names:
                continue

            for raw_datetime_value in metadata_field["exiftool_values"]:
                try:
                    (
                        parsed_date,
                        parsed_time,
                        parsed_offset,
                        parsed_utc,
                    ) = parse_metadata_datetime(raw_datetime_value)
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"invalid {qualified_metadata_path} datetime "
                        f"{raw_datetime_value!r} for '{source_file.name}' "
                        f"({error})"
                    ) from error

                # Equal indexes across these lists describe the same ExifTool
                # occurrence. Append only after the complete value has parsed.
                metadata_field["dates"].append(parsed_date)
                metadata_field["times"].append(parsed_time)
                metadata_field["offsets"].append(parsed_offset)
                metadata_field["utc_flags"].append(parsed_utc)

                embedded_datetime_found = True
                datetime_value = datetime.combine(parsed_date, parsed_time)
                datetime_option = datetime_options.setdefault(
                    datetime_value,
                    {"sources": []},
                )

                # Each source has the fixed structure:
                #
                #     (source_file, qualified_metadata_path)
                #
                # The file and complete path are both retained because conflict
                # explanations must identify the exact origin of every value.
                datetime_option["sources"].append(
                    (source_file, qualified_metadata_path)
                )

        if embedded_datetime_found:
            continue

        file_modify_datetime_found = False

        # FileModifyDate was acquired in the same ExifTool operation but remains
        # unused until this point. It is the final fallback only when the current
        # file supplied no approved embedded datetime.
        for qualified_metadata_path, metadata_field in source_file_metadata.items():
            if metadata_field["tag_name"] != "FileModifyDate":
                continue

            for raw_datetime_value in metadata_field["exiftool_values"]:
                try:
                    (
                        parsed_date,
                        parsed_time,
                        parsed_offset,
                        parsed_utc,
                    ) = parse_metadata_datetime(raw_datetime_value)
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"invalid {qualified_metadata_path} datetime "
                        f"{raw_datetime_value!r} for '{source_file.name}' "
                        f"({error})"
                    ) from error

                metadata_field["dates"].append(parsed_date)
                metadata_field["times"].append(parsed_time)
                metadata_field["offsets"].append(parsed_offset)
                metadata_field["utc_flags"].append(parsed_utc)

                file_modify_datetime_found = True
                datetime_value = datetime.combine(parsed_date, parsed_time)
                datetime_option = datetime_options.setdefault(
                    datetime_value,
                    {"sources": []},
                )
                datetime_option["sources"].append(
                    (source_file, qualified_metadata_path)
                )

        if not file_modify_datetime_found:
            raise ValueError(
                f"ExifTool returned no approved embedded datetime or "
                f"FileModifyDate for '{source_file.name}'"
            )

    # Every successfully evaluated file contributes embedded evidence or its
    # FileModifyDate fallback. An empty result would therefore violate the
    # function's contract rather than represent a valid classification state.
    if not datetime_options:
        raise ValueError(
            "the related group contains no usable datetime evidence"
        )

    return datetime_options


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
        # Collect every established base that can own this derivative. The most
        # specific match is selected afterward; stopping at the first match would
        # make dictionary iteration order affect the relationship decision.
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

    related_file_groups = []

    # Sort base stems first so groups have a stable case-insensitive order. Sort
    # each group's files separately so both ordering levels remain explicit
    # instead of being combined in one nested comprehension.
    for base_stem in sorted(groups, key=str.casefold):
        related_files = sorted(
            groups[base_stem],
            key=lambda path: path.name.casefold(),
        )
        related_file_groups.append(related_files)

    return related_file_groups


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
    ``ExifToolHelper`` context for all metadata reads. Each complete related
    group is acquired in one ExifTool call and receives one routing decision:
    either one selected datetime for ``classified/YYYY-MM-DD`` or one review
    reason for ``unclassified``.

    The source files are never moved, renamed, deleted, or opened for writing.
    Return exit code 0 on complete success, 1 when setup or the ExifTool session
    cannot be established, and 2 when one or more output copies fail.
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
    #   unclassified/           complete groups requiring metadata review
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

        # Take one fixed, non-recursive snapshot before classification starts.
        # The generator includes only direct regular, non-symlink files. Sorting
        # case-insensitively keeps grouping, prompts, and reports deterministic.
        # Newly created output copies cannot enter this already-built snapshot.
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

    # Related files are processed as one unit because originals, derivatives,
    # sidecars, and other same-stem companions must receive the same destination.
    # Counters describe completed output actions rather than discovery totals.
    related_file_groups = group_related_files(source_files)

    copied_count = 0
    duplicate_count = 0
    renamed_count = 0
    review_count = 0
    failed_count = 0

    # Persistent metadata session and per-group classification

    # Entering the context starts one stay-open ExifTool process after all setup
    # checks have passed. The same helper handles every group-level acquisition.
    # Leaving the context terminates the process on normal completion or when an
    # exception exits the block, so no manual run()/terminate() pair is needed.
    try:
        with ExifToolHelper(
            common_args=EXIFTOOL_COMMON_ARGS
        ) as metadata_reader:
            # Each group follows one linear sequence: acquire all members,
            # normalize evidence, build datetime options, resolve any conflict,
            # establish one destination, and copy every member.
            for related_files in related_file_groups:
                review_reason = None
                selected_datetime = None

                # Metadata acquisition and datetime-option construction

                try:
                    related_files_metadata = read_related_files_metadata(
                        metadata_reader,
                        related_files,
                    )
                    datetime_options = build_datetime_options(
                        related_files_metadata
                    )

                except ExifToolExecuteError as error:
                    # A batch execution failure means ExifTool could not inspect
                    # the complete group. Partial evidence is deliberately not
                    # used because one inaccessible member makes group routing
                    # unreliable.
                    review_reason = (
                        "ExifTool could not inspect every file in the related "
                        f"group ({error})"
                    )

                except ValueError as error:
                    # Structural result violations, reported metadata errors, and
                    # invalid or missing datetime evidence all require review of
                    # the complete group rather than a per-file fallback.
                    review_reason = str(error)

                # Authoritative datetime selection

                if review_reason is None:
                    if len(datetime_options) == 1:
                        # Iterating over a dictionary returns its keys. Because
                        # there is exactly one option, this retrieves its single
                        # datetime key without copying the dictionary's contents.
                        selected_datetime = next(iter(datetime_options))

                    else:
                        # Sorting the datetime keys keeps the numbered options
                        # deterministic regardless of metadata return order.
                        available_datetimes = sorted(datetime_options)

                        print()
                        print(
                            "Conflicting datetimes found for this related group:"
                        )
                        for related_file in related_files:
                            print(f"  - {related_file.name}")

                        print(
                            "Choose the datetime that should be used for this "
                            "group:"
                        )

                        for option_number, datetime_value in enumerate(
                            available_datetimes,
                            start=1,
                        ):
                            # Each source contains the related file and complete
                            # ExifTool path that supplied this datetime. Join all
                            # contributors into one explanation for the option.
                            datetime_source_description = ", ".join(
                                f"{source_file.name} "
                                f"({qualified_metadata_path})"
                                for (
                                    source_file,
                                    qualified_metadata_path,
                                ) in datetime_options[datetime_value]["sources"]
                            )
                            print(
                                f"  {option_number}. "
                                f"{datetime_value.strftime('%Y-%m-%d %H:%M:%S')} "
                                f"- {datetime_source_description}"
                            )

                        while True:
                            raw_selection = input(
                                "Enter a number from 1 to "
                                f"{len(available_datetimes)}: "
                            ).strip()

                            try:
                                selected_option_index = int(raw_selection) - 1
                            except ValueError:
                                selected_option_index = -1

                            if 0 <= selected_option_index < len(
                                available_datetimes
                            ):
                                selected_datetime = available_datetimes[
                                    selected_option_index
                                ]
                                break

                            print(
                                "Invalid selection. "
                                "Enter one of the listed numbers."
                            )

                        print(
                            "Selected datetime: "
                            f"{selected_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                        print()

                # Destination routing

                # review_reason is both the routing state and the explanation
                # reported to the user. A separate needs_review boolean or
                # display-only folder name would duplicate the same decision.
                if review_reason is not None:
                    destination_directory = unclassified_directory
                    print(
                        "Warning: related group requires review: "
                        f"{review_reason}",
                        file=sys.stderr,
                    )
                else:
                    date_folder_name = selected_datetime.strftime("%Y-%m-%d")
                    destination_directory = (
                        classified_directory / date_folder_name
                    )

                    selected_datetime_source_groups = set()

                    # Each source has the fixed structure:
                    #
                    #     (source_file, qualified_metadata_path)
                    #
                    # The source file is used in conflict explanations. This
                    # final summary needs only the qualified path at index 1;
                    # its first component identifies the broad ExifTool group.
                    for datetime_source in datetime_options[selected_datetime]["sources"]:
                        qualified_metadata_path = datetime_source[1]
                        source_group = qualified_metadata_path.split(":", 1)[0]
                        selected_datetime_source_groups.add(source_group)

                    # Sets express unique unordered source groups. Sorting is
                    # applied only for deterministic presentation.
                    selected_datetime_source_description = ", ".join(
                        sorted(selected_datetime_source_groups)
                    )

                # Non-destructive copying and reporting

                for related_file in related_files:
                    # A file blocking the selected destination directory is an
                    # output failure. It does not justify deleting or replacing
                    # that file, and other group members are still attempted.
                    if (
                        destination_directory.exists()
                        and not destination_directory.is_dir()
                    ):
                        print(
                            f"Error: '{destination_directory}' is a file. "
                            f"Skipping '{related_file.name}'.",
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
                            related_file,
                            destination_directory / related_file.name,
                        )
                    except OSError as error:
                        print(
                            f"Error copying '{related_file.name}': {error}",
                            file=sys.stderr,
                        )
                        failed_count += 1
                        continue

                    # Derive the display path from the authoritative destination
                    # immediately before reporting. No parallel folder label is
                    # maintained that could diverge from destination_directory.
                    displayed_destination = destination_directory.relative_to(
                        directory
                    )
                    print(
                        f"{related_file.name}\t--->\t"
                        f"{displayed_destination}\t",
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

                    if review_reason is not None:
                        review_count += 1
                        print(
                            f"; REVIEW: {review_reason}",
                            end="",
                        )
                    else:
                        print(
                            "; "
                            f"{selected_datetime.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"{selected_datetime_source_description}",
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
        # Expected group execution errors are handled inside the group loop.
        print(f"Error during ExifTool processing: {error}", file=sys.stderr)
        input("Press Enter to exit")
        return 1

    # Final report and exit status

    # Counts describe completed output decisions. Failed copies are reported
    # separately and cause exit code 2, while successful classified and review
    # copies remain valid. The final statement reiterates source immutability.
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
