import shutil
import sys
from datetime import datetime
from pathlib import Path

import PIL.Image
from PIL import UnidentifiedImageError


VERSION = "0.1.1"

# Preserve the original support for very large RAW-derived images and panoramas.
# This setting will be reviewed later with the metadata modernization.
PIL.Image.MAX_IMAGE_PIXELS = None
PIL.Image.init()

DATETYPE = {0: "OS_DATE", 1: "EXIF"}
EXIF_DATE_TAG = 36867
EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
REVIEW_FOLDER_NAME = "to be reviewed"
COPY_CHUNK_SIZE = 1024 * 1024
IMAGE_EXTENSIONS = {
    extension.casefold()
    for extension in PIL.Image.registered_extensions()
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


def get_date(filename):
    """
    Return (date_type, date_value, review_reason).

    This keeps the original priority: EXIF DateTimeOriginal first, filesystem
    modification time second. A damaged image or invalid EXIF date is marked
    for review instead of silently receiving its modification date.
    """
    exif_date_text = None

    try:
        with PIL.Image.open(filename) as image:
            # Keep the original JPEG-style EXIF access until the later
            # metadata modernization changes this in isolation.
            legacy_exif_reader = getattr(image, "_getexif", None)
            if callable(legacy_exif_reader):
                try:
                    exif_data = legacy_exif_reader()
                except (AttributeError, IndexError, KeyError, OSError,
                        TypeError, ValueError) as error:
                    return None, None, f"could not read EXIF metadata ({error})"

                if exif_data:
                    exif_date_text = exif_data.get(EXIF_DATE_TAG)

            # Keep the original TIFF-specific fallback for the same reason.
            if exif_date_text is None:
                tiff_tags = getattr(image, "tag", None)
                if tiff_tags is not None:
                    try:
                        exif_date_text = tiff_tags.get(EXIF_DATE_TAG)
                    except (AttributeError, IndexError, KeyError, OSError,
                            TypeError, ValueError) as error:
                        return None, None, f"could not read TIFF metadata ({error})"

            # An image that opens but fails verification is not trusted.
            try:
                image.verify()
            except (OSError, SyntaxError, ValueError) as error:
                return None, None, f"image verification failed ({error})"

    except UnidentifiedImageError as error:
        # Unknown non-image files retain the original modification-time
        # fallback. A recognized image extension is routed for review.
        if filename.suffix.casefold() in IMAGE_EXTENSIONS:
            return None, None, f"Pillow could not identify the image ({error})"
        return 0, datetime.fromtimestamp(filename.stat().st_mtime), None

    except PermissionError as error:
        raise OSError(f"cannot read '{filename}': {error}") from error

    except (OSError, SyntaxError, ValueError) as error:
        if filename.suffix.casefold() in IMAGE_EXTENSIONS:
            return None, None, f"could not inspect image metadata ({error})"
        return 0, datetime.fromtimestamp(filename.stat().st_mtime), None

    # TIFF metadata may return a one-item sequence instead of a plain value.
    if isinstance(exif_date_text, (list, tuple)):
        exif_date_text = exif_date_text[0] if exif_date_text else None

    if isinstance(exif_date_text, bytes):
        try:
            exif_date_text = exif_date_text.decode("ascii")
        except UnicodeDecodeError as error:
            return None, None, f"EXIF date is not valid ASCII ({error})"

    if exif_date_text is not None:
        try:
            exif_date = datetime.strptime(exif_date_text, EXIF_DATE_FORMAT)
        except (TypeError, ValueError) as error:
            return None, None, f"invalid EXIF date {exif_date_text!r} ({error})"
        return 1, exif_date, None

    # A valid image without DateTimeOriginal is not damaged.
    return 0, datetime.fromtimestamp(filename.stat().st_mtime), None


def stems_are_related(base_stem, longer_stem):
    """
    A derivative must begin with the complete base stem. Its first added
    character may be anything except an ASCII digit.

    IMG_0001pano and IMG_0001-edit match IMG_0001.
    IMG_00010 does not match IMG_0001.
    """
    base_stem = base_stem.casefold()
    longer_stem = longer_stem.casefold()

    if len(longer_stem) <= len(base_stem) or not longer_stem.startswith(base_stem):
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
            groups[max(matching_bases, key=len)].extend(files_by_stem[stem])
        else:
            groups[stem] = list(files_by_stem[stem])

    return [
        sorted(group, key=lambda path: path.name.casefold())
        for _, group in sorted(groups.items(), key=lambda item: item[0].casefold())
    ]


def files_are_binary_identical(first_file, second_file):
    """Compare files byte-for-byte without loading either file fully."""
    if not second_file.is_file():
        return False
    if first_file.stat().st_size != second_file.stat().st_size:
        return False

    with first_file.open("rb") as first_stream, second_file.open("rb") as second_stream:
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
            if files_are_binary_identical(source_file, destination_file):
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


# Request source and output paths in the interactive style of the original.
directory = clean_input_path(
    input("Please write (or drag) the source directory path: ")
)

if not directory.exists() or not directory.is_dir():
    print(f"Error: source directory is invalid: '{directory}'")
    input("Press Enter to exit")
    raise SystemExit(1)

default_output_directory = directory.parent / f"{directory.name}_classified"
raw_output_directory = input(
    "Please write (or drag) the output directory path "
    f"[{default_output_directory}]: "
)
output_directory = (
    clean_input_path(raw_output_directory)
    if raw_output_directory.strip()
    else default_output_directory
)

directory = directory.resolve()
output_directory = output_directory.resolve()

# The output must be separate and outside the source tree, ensuring that the
# source remains unchanged and generated files cannot enter the input scan.
if output_directory == directory:
    print("Error: output and source directories must be different.")
    input("Press Enter to exit")
    raise SystemExit(1)

try:
    output_directory.relative_to(directory)
except ValueError:
    pass
else:
    print("Error: the output directory cannot be inside the source directory.")
    input("Press Enter to exit")
    raise SystemExit(1)

if output_directory.exists() and not output_directory.is_dir():
    print(f"Error: output path exists as a file: '{output_directory}'")
    input("Press Enter to exit")
    raise SystemExit(1)

try:
    output_directory.mkdir(parents=True, exist_ok=True)

    # Snapshot regular, non-symlink files once. Directories can therefore
    # neither join a stem group nor be copied unintentionally.
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

# Follow the original processing flow: determine one preferred date for each
# related group, then copy the files to that group's date directory.
for same_stem_files in same_stem_groups:
    file_dates = {}
    review_reasons = {}
    selected_file_date = None

    for same_stem_file in same_stem_files:
        try:
            date_type, date_value, review_reason = get_date(same_stem_file)
        except OSError as error:
            print(f"Error reading '{same_stem_file.name}': {error}", file=sys.stderr)
            failed_count += 1
            continue

        if review_reason is not None:
            print(
                f"Warning: '{same_stem_file.name}' requires review: {review_reason}",
                file=sys.stderr,
            )
            review_reasons[same_stem_file] = review_reason
            continue

        current_file_date = (date_type, date_value)
        file_dates[same_stem_file] = current_file_date

        if selected_file_date is None:
            selected_file_date = current_file_date

        # Preserve the original rule: oldest date when the sources match;
        # otherwise an EXIF date replaces an OS-derived date.
        elif (
            selected_file_date[0] == current_file_date[0]
            and selected_file_date[1] > current_file_date[1]
        ):
            selected_file_date = current_file_date
        elif selected_file_date[0] < current_file_date[0]:
            selected_file_date = current_file_date

    for same_stem_file in same_stem_files:
        if same_stem_file in review_reasons:
            folder_name = REVIEW_FOLDER_NAME
            destination_directory = output_directory / REVIEW_FOLDER_NAME
        elif same_stem_file in file_dates and selected_file_date is not None:
            folder_name = selected_file_date[1].strftime("%Y-%m-%d")
            destination_directory = output_directory / folder_name
        else:
            continue

        if destination_directory.exists() and not destination_directory.is_dir():
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
            print(f"Error copying '{same_stem_file.name}': {error}", file=sys.stderr)
            failed_count += 1
            continue

        print(f"{same_stem_file.name}\t--->\t{folder_name}\t", end="")

        if copied:
            copied_count += 1
            if renamed:
                renamed_count += 1
                print(f"COPIED AS {destination_file.name}", end="")
            else:
                print("COPIED", end="")
        else:
            duplicate_count += 1
            print(f"DUPLICATE OF {destination_file.name}", end="")

        if same_stem_file in review_reasons:
            review_count += 1
            print(f"; REVIEW: {review_reasons[same_stem_file]}", end="")
        else:
            print(
                f"; {selected_file_date[1].strftime('%Y-%m-%d %H:%M:%S')} "
                f"{DATETYPE[selected_file_date[0]]}",
                end="",
            )
        print()

print()
print(f"Output directory: {output_directory}")
print(f"Copied files: {copied_count}")
print(f"Binary duplicates: {duplicate_count}")
print(f"Renamed collision copies: {renamed_count}")
print(f"Files sent for review: {review_count}")
print(f"Failed files: {failed_count}")
print("The source directory was not modified.")

input("Press Enter to exit")

if failed_count:
    raise SystemExit(2)
