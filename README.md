# fileClassify_dateTaken

`fileClassify_dateTaken` is an interactive Python script that classifies related
files by their best available datetime while preserving every original source
file.

Current version: **0.3.3**

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

Every member of a related group receives the same routing decision and output
directory. A group is either classified with one selected datetime or sent in
full to manual review.

## Metadata reading

One persistent `ExifToolHelper` context is opened after directory validation.
The same stay-open ExifTool process is reused throughout the run and is closed
when the context exits.

Each complete related group is submitted to ExifTool in one call. PyExifTool
returns one result dictionary per requested file, and each dictionary's
`SourceFile` value associates the returned fields with that group member. The
result count and source associations are validated before any datetime evidence
is used.

The script requests only these approved embedded datetime fields:

- `ExifIFD:DateTimeOriginal`
- `ExifIFD:CreateDate`
- `IFD0:ModifyDate`

It also requests:

- `File:FileModifyDate`, acquired with the embedded metadata as the current
  fallback source;
- `ExifTool:Error`, used to reject unreliable group acquisition or metadata.

MIME type and filename extension are not used to decide whether a file may be
processed. ExifTool is expected to inspect every regular group member and to
return at least its filesystem modification datetime when no approved embedded
datetime exists.

ExifTool runs with:

```text
-G:0:1:2:7 -a -e -ee3
```

These arguments retain the selected complete group path, allow duplicate tag
instances, suppress generated Composite fields, and inspect supported embedded
metadata. No global `-n`, `-d`, or `QuickTimeUTC` option is applied.

Scalar and list-valued results are normalized without shortening their returned
paths. Metadata is retained first by source file and then by complete ExifTool
path. Each field stores its path components, terminal tag name, original values,
and synchronized parsed date, time, offset, and UTC lists.

## Datetime parsing

The parser accepts complete datetimes in these forms:

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
remain distinguishable. Version 0.3.3 does not shift the parsed wall-clock date
or time according to either representation.

For each complete ExifTool path, the parser keeps synchronized lists of:

- original ExifTool values;
- parsed dates;
- parsed times;
- numeric offsets;
- explicit UTC flags.

The same list index always describes the same returned occurrence. Parsed values
are appended only after the complete occurrence has been validated.

## FileModifyDate fallback

`FileModifyDate` is always requested in the same ExifTool operation as the
approved embedded fields. It is retained as raw evidence for every successfully
inspected file.

For the current version:

- one or more approved embedded datetimes take priority for that file;
- the retained `FileModifyDate` is not parsed or added as an option when embedded
  datetime evidence exists;
- `FileModifyDate` is parsed and used only when that file supplies no approved
  embedded datetime;
- Python does not perform a separate `stat().st_mtime` fallback read.

## Group review rules

The complete related group is sent to `unclassified` when:

- the group-level ExifTool request cannot inspect every requested file;
- the number of returned file results does not match the group size;
- a result is missing `SourceFile`, identifies an unexpected source, or duplicates
  another source result;
- ExifTool reports an error for any group member;
- any evaluated approved embedded datetime is invalid;
- a file without embedded datetime evidence has a missing or invalid
  `FileModifyDate`.

Partial results are not used to classify the remaining members. One unreliable
member makes the group routing decision unreliable.

Session-level PyExifTool failures outside the expected group execution boundary
remain run-level errors.

## Conflict selection

All usable datetime occurrences are consolidated directly into distinct group
options. The combined `datetime` value is the agreement key. Equal values are
treated as agreement even when they come from different files or complete
metadata paths.

Each option retains every contributing `(source file, qualified ExifTool path)`
pair for reporting. When more than one distinct datetime remains, the script
sorts the values deterministically, lists every numbered option and its exact
sources, and requires the user to select one authoritative datetime for the
complete group.

When exactly one option exists, it is selected automatically without copying or
restructuring the options dictionary.

## Output and copying safety

The script creates or reuses:

```text
<source>/
├── classified/
│   └── YYYY-MM-DD/
└── unclassified/
```

Groups with a selected datetime are copied to `classified/YYYY-MM-DD`. Groups
that require metadata review are copied in full to `unclassified`.

The displayed output location is derived directly from the authoritative
destination path. The script does not maintain a second folder label or review
boolean that could become inconsistent with the routing decision.

For classified files, the report shows the selected datetime followed by the
unique first components of its contributing ExifTool paths, such as `EXIF`,
`XMP`, `QuickTime`, or `File`.

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

- `0`: the run completed without failed output copies;
- `1`: input/output setup or the persistent ExifTool session failed;
- `2`: one or more output copies failed, while other completed classified or
  review copies remain valid.
