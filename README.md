# fileClassify_dateTaken

`fileClassify_dateTaken` is an interactive Python script that classifies files by their best available date while preserving the original source files.

Current version: **0.3.0**

## Requirements

- Python 3
- ExifTool available as the `exiftool` command
- PyExifTool

Install PyExifTool with:

```bash
python -m pip install PyExifTool
```

Install ExifTool for the operating system and ensure the `exiftool` command is available on `PATH`.

## Usage

Run:

```bash
python reclassify_datetaken.py
```

Enter or drag the source directory path when prompted.

## Processing

The script:

- processes regular, non-symlink files located directly inside the selected directory;
- does not recurse into subdirectories;
- groups files with identical stems;
- attaches derivative stems to the longest matching base stem;
- treats an immediate numeric continuation as a separate group;
- reads these EXIF date fields from recognized images with ExifTool:
  - `ExifIFD:DateTimeOriginal`;
  - `ExifIFD:CreateDate`;
  - `IFD0:ModifyDate`;
- retains every valid supported EXIF date found in a file;
- uses the filesystem modification time when no supported EXIF date is available;
- asks the user to select the authoritative date when a related group contains conflicting values;
- copies files with unreadable, damaged, or invalid recognized-image metadata to `unclassified` for review;
- copies files with a selected date to `classified/YYYY-MM-DD`;
- compares existing output files byte-for-byte;
- reuses an identical existing output or adds `_1`, `_2`, and later suffixes when a filename collision contains different data.

Original source files are not modified, moved, renamed, or deleted.

## Output

The script creates the following directories inside the selected source directory:

```text
<source>/
├── classified/
│   └── YYYY-MM-DD/
└── unclassified/
```

Each successfully classified file is copied into the folder matching its selected date. Files requiring manual review are copied into `unclassified`.
