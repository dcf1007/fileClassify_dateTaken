import shutil
import sys
from datetime import datetime
from pathlib import Path

from exiftool import ExifToolHelper
from exiftool.exceptions import ExifToolException, ExifToolExecuteError


VERSION = "0.3.0"

DATETYPE = {0: "OS_DATE", 1: "EXIF"}

# These are the three standard EXIF date fields used as timestamp candidates.
# DateTimeOriginal normally describes when the picture was taken, CreateDate
# corresponds to DateTimeDigitized, and ModifyDate describes when the image
# metadata was last changed.
EXIF_DATE_FIELDS = [
    "ExifIFD:DateTimeOriginal",
    "ExifIFD:CreateDate",
    "IFD0:ModifyDate",
]

# These fields identify image content and expose errors reported by ExifTool.
# They control whether an unreadable file uses the filesystem-date fallback or
# is sent to unclassified as a recognized image requiring review.
EXIFTOOL_FILE_FIELDS = [
    "File:MIMEType",
    "ExifTool:Error",
]

EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
CLASSIFIED_FOLDER_NAME = "classified"
UNCLASSIFIED_FOLDER_NAME = "unclassified"
COPY_CHUNK_SIZE = 1024 * 1024

# This static set is the case-folded equivalent of the extension keys returned
# by PIL.Image.registered_extensions() in version 0.2.0. Keeping the same
# extensions preserves backward-compatible handling of damaged or unreadable
# images: recognized image files are sent for review instead of being treated
# as ordinary non-image files.
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


def get_dates(filename):
    """
    Return (date_candidates, review_reason).

    Each date candidate is stored as:

        (date_type, date_value, source_name)

    A single file may contain several EXIF date fields. All valid values are
    returned so the main processing loop can detect a disagreement within one
    file in exactly the same way as a disagreement between related files.

    If no supported EXIF date exists, the filesystem modification time is used
    as the fallback. The modification time is not added when EXIF dates are
    present, because it remains a last resort rather than an equal alternative
    to embedded metadata.

    A damaged image, unreadable metadata, or any present EXIF date that cannot
    be parsed returns a review reason. Such a file is copied to unclassified
    rather than classified using a timestamp that may be unreliable.
    """
    recognized_extension = filename.suffix.casefold() in IMAGE_EXTENSIONS

    # Check file readability before invoking ExifTool so permission failures
    # remain processing failures instead of being mistaken for damaged images.
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

    # Read only the three established date fields and the file-status fields
    # needed to preserve the existing image-versus-non-image fallback behavior.
    # The context manager guarantees that the ExifTool subprocess is terminated
    # after this file has been inspected, including when metadata reading fails.
    try:
        with ExifToolHelper(common_args=["-G1", "-n"]) as metadata_reader:
            metadata_results = metadata_reader.get_tags(
                files=filename,
                tags=EXIF_DATE_FIELDS + EXIFTOOL_FILE_FIELDS,
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

    if not metadata_results:
        if recognized_extension:
            return [], "ExifTool could not identify the image"
        modification_date = datetime.fromtimestamp(filename.stat().st_mtime)
        return [(0, modification_date, DATETYPE[0])], None

    metadata = metadata_results[0]
    mime_type = metadata.get("File:MIMEType")
    metadata_error = metadata.get("ExifTool:Error")
    identified_image = (
        isinstance(mime_type, str)
        and mime_type.casefold().startswith("image/")
    )

    if metadata_error is not None:
        if identified_image or recognized_extension:
            return [], f"could not inspect image metadata ({metadata_error})"
        modification_date = datetime.fromtimestamp(filename.stat().st_mtime)
        return [(0, modification_date, DATETYPE[0])], None

    # A recognized image extension without an image MIME type represents the
    # same damaged-or-unreadable case that was previously detected by Pillow.
    if not identified_image:
        if recognized_extension:
            return [], "ExifTool could not identify the image"
        modification_date = datetime.fromtimestamp(filename.stat().st_mtime)
        return [(0, modification_date, DATETYPE[0])], None

    date_candidates = []

    # Parse every supported EXIF field that is present. Keeping the fully
    # qualified tag name with the timestamp identifies its exact metadata path
    # when conflicting dates are displayed to the user.
    for tag_name in EXIF_DATE_FIELDS:
        exif_date_text = metadata.get(tag_name)
        if exif_date_text is None:
            continue

        try:
            exif_date = datetime.strptime(
                str(exif_date_text),
                EXIF_DATE_FORMAT,
            )
        except (TypeError, ValueError) as error:
            return [], (
                f"invalid {tag_name} date "
                f"{exif_date_text!r} ({error})"
            )

        date_candidates.append((1, exif_date, tag_name))

    if date_candidates:
        return date_candidates, None

    # A recognized image without one of the supported EXIF date fields is not
    # damaged and therefore uses the filesystem modification-time fallback.
    modification_date = datetime.fromtimestamp(filename.stat().st_mtime)
    return [(0, modification_date, DATETYPE[0])], None


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
        sorted(group, key=lambda path: path.name.casefold())
        for _, group in sorted(
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


# Request the source directory interactively. The script deliberately examines
# only regular files located directly inside this directory. It never walks
# into existing subdirectories.
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

copied_count = 0
duplicate_count = 0
renamed_count = 0
review_count = 0
failed_count = 0

# Determine one preferred date for each related group, then copy the files to
# that group's date directory.
for same_stem_files in same_stem_groups:
    file_dates = {}
    review_reasons = {}
    selected_file_date = None

    # Read every usable date before selecting the date for the group. Collecting
    # all candidates first allows conflicting values to be shown to the user.
    for same_stem_file in same_stem_files:
        try:
            date_candidates, review_reason = get_dates(same_stem_file)
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

        # Store every candidate returned for this file. A file with conflicting
        # EXIF fields can therefore produce several date options even when it
        # is the only file in its related group.
        file_dates[same_stem_file] = date_candidates

    # Group identical timestamp values together, whether they came from
    # separate files or separate fields inside one file. The source is not
    # part of the key: fields containing the exact same timestamp agree and do
    # not require a prompt. Their file and field names are retained for display.
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
        # More than one distinct timestamp was found in the related group. The
        # disagreement may be between files, between fields in one file, or
        # both. List every source and require a numbered user choice.
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
            same_stem_file in file_dates
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
