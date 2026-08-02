# fileClassify_dateTaken

Python script that takes the files directly inside a selected directory and copies them into date-based subdirectories according to metadata timestamps. If no complete embedded timestamp is available, the filesystem modification time is used.

The script does not scan subdirectories. Classified copies are written to `classified/YYYY-MM-DD`, while damaged files or files with unreliable image metadata are copied to `unclassified` for manual review. Original files are not moved or overwritten.

## Metadata extraction

The script uses PyExifTool to request ExifTool's complete `Time:All` group. Extraction is performed with:

```text
-G0:1:4 -a -ee -Time:All
```

This reads timestamps from EXIF, XMP, IPTC, QuickTime, maker notes, composite tags, and other metadata families supported by ExifTool. The group levels preserve the metadata type, exact storage location, and duplicate instance number, so non-standard or duplicate tags are not hidden.

The normal classification pass does not request ExifTool's `Validate`, `Warning`, or `Error` pseudo-tags. Validation performs additional metadata-conformance checks and can report non-standard tag placement even when the timestamp is readable. Timestamp discovery does not require those checks.

Filesystem pseudo-tags returned by `Time:All`, including `FileModifyDate`, `FileCreateDate`, and `FileAccessDate`, are excluded from the metadata choices. The filesystem modification time is used only as a fallback when no complete embedded timestamp exists.

When distinct complete embedded timestamps are found, the script displays their full ExifTool source paths and asks the user to choose the date for the related file group.

## Requirements

- Python 3.8 or newer
- Phil Harvey's ExifTool 12.15 or newer, available as `exiftool` or `exiftool.exe` on the system `PATH`
- PyExifTool 0.5.4 or newer

Install the Python dependency with:

```bash
python -m pip install -r requirements.txt
```

PyExifTool is the Python wrapper imported by the script as `exiftool`. The separate ExifTool executable must also be installed; installing the Python package does not install that executable.
