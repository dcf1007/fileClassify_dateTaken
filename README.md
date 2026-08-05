# fileClassify_dateTaken

`fileClassify_dateTaken` is an interactive Python script that classifies files
by their best available timestamp while preserving every original source file.

Current version: **0.3.2**

## Requirements

- Python 3
- ExifTool available as the `exiftool` command
- PyExifTool

Install PyExifTool with:

```bash
python -m pip install PyExifTool
```

Install ExifTool for the operating system and ensure that the `exiftool`
command is available on `PATH`.

## Usage

Run:

```bash
python reclassify_datetaken.py
```

Enter or drag the source directory path when prompted.

## Input scope

The script takes one fixed snapshot of the selected directory before
classification begins. It processes only regular, non-symlink files located
directly inside that directory. It does not recurse into subdirectories, so
existing or newly created output copies cannot re-enter the same run.

## Related-file grouping

Files with the same stem are grouped together, including files with different
extensions such as an image and its sidecar. A longer derivative stem is
attached to the longest matching shorter stem when the first additional
character is not an ASCII digit.

For example:

```text
IMG_0001.jpg
IMG_0001.xmp
IMG_0001-edit.jpg
IMG_0001pano.jpg
```

belong to one related group, while `IMG_00010.jpg` remains separate from
`IMG_0001.jpg`.

Every usable file in a related group receives the same selected classification
date.

## Metadata reading

One persistent `ExifToolHelper` context is opened after directory validation.
The same stay-open ExifTool process is reused for every file and is closed when
the context exits.

The script requests only these approved timestamp fields:

- `ExifIFD:DateTimeOriginal`
- `ExifIFD:CreateDate`
- `IFD0:ModifyDate`

It also requests `File:MIMEType` and `ExifTool:Error` to distinguish ordinary
non-image files from recognized images whose metadata is damaged or unreadable.

ExifTool runs with:

```text
-G:0:1:2:7 -a -e -ee3
```

These arguments retain the selected complete group path, allow duplicate tag
instances, suppress generated Composite fields, and inspect supported embedded
metadata. No global `-n`, `-d`, or `QuickTimeUTC` option is applied.

Scalar and list-valued results are normalized without shortening their returned
paths. Every non-null occurrence of an approved timestamp is retained under its
complete ExifTool path.

## Timestamp parsing

The parser accepts complete timestamps in these forms:

```text
YYYY:mm:dd HH:MM:SS
YYYY:mm:dd HH:MM:SS.<fractional digits>
YYYY:mm:dd HH:MM:SS +HH:MM
YYYY:mm:dd HH:MM:SS-HH:MM
YYYY:mm:dd HH:MM:SSZ
YYYY:mm:dd HH:MM:SSz
```

Fractional seconds may contain any number of digits but are intentionally
discarded; classification uses whole-second precision.

Numeric offsets are retained as signed minutes. Explicit `Z` or `z` notation is
retained as a separate UTC flag, with no numeric offset, so `+00:00` and `Z`
remain distinguishable. Version 0.3.2 does not shift the parsed wall-clock date
or time according to either representation.

For each complete ExifTool path, the parser keeps synchronized lists of:

- original ExifTool values;
- parsed dates;
- parsed times;
- numeric offsets;
- explicit UTC flags.

The same list index always describes the same returned occurrence.

## Fallback and review rules

The filesystem modification time is used when:

- an ordinary non-image file is processed;
- an identified image contains none of the approved metadata timestamps.

A recognized image is sent to `unclassified` for manual review when:

- the file cannot be inspected reliably;
- ExifTool reports a metadata error;
- ExifTool cannot identify content with a recognized image extension;
- any present approved timestamp has invalid syntax or values.

A permission failure or other unrecoverable processing error is reported as a
failed file instead of being converted into a review decision.

## Conflict selection

All usable timestamp occurrences are collected before a related group is
classified. Equal datetime values are treated as agreement even when they come
from different files or metadata paths. Their complete sources are retained for
reporting.

When more than one distinct datetime remains, the script lists every option and
its contributing file and metadata paths, then requires the user to select one
numbered authoritative date for the group.

## Output and copying safety

The script creates or reuses:

```text
<source>/
├── classified/
│   └── YYYY-MM-DD/
└── unclassified/
```

Files with a selected date are copied to `classified/YYYY-MM-DD`. Files that
require manual metadata review are copied to `unclassified`.

Existing destinations are compared byte-for-byte in bounded chunks:

- a binary-identical file is reused as a duplicate;
- a different file with the same name causes `_1`, `_2`, and later suffixes to
  be tried before the extension;
- destination files are created exclusively, so concurrent activity cannot
  silently overwrite an existing file;
- a partially written destination is removed after a copy failure.

File metadata and filesystem times are copied to new outputs with
`shutil.copystat`.

Original source files are never modified, moved, renamed, or deleted.

## Exit codes

- `0`: the run completed without failed files;
- `1`: input/output setup or the persistent ExifTool session failed;
- `2`: one or more individual files failed, while other completed copies remain
  valid.
