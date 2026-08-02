# fileClassify_dateTaken

Python script that takes the files directly inside a selected directory and copies them into date-based subdirectories according to metadata timestamps. If no complete embedded timestamp is available, the filesystem modification time of the base image is used.

The script does not scan subdirectories. Classified copies are written to `classified/YYYY-MM-DD`, while damaged files or files with unreliable image metadata are copied to `unclassified` for manual review. Original files are not moved or overwritten.

## Metadata extraction

The first pass uses PyExifTool to request ExifTool's complete `Time:All` group with duplicate and embedded metadata enabled:

```text
-G0:1:4 -a -ee -Time:All
```

The request excludes the family-1 `System` group before ExifTool returns results, so filesystem pseudo-tags such as `FileModifyDate`, `FileCreateDate`, `FileAccessDate`, and `FileInodeChangeDate` do not enter the embedded-metadata choices.

It also excludes timestamps whose semantics do not describe original image or recording creation:

```text
PowerUpTime
TimeSincePowerOn
RunTimeSincePowerUp
ShotNumberSincePowerUp
MetadataDate
HistoryWhen
ModifyDate
SubSecModifyDate
MediaModifyDate
TrackModifyDate
LastModifyDate
DateTimeEnd
EndTime
ProfileDateTime
LayerModifyDates
```

These cover camera/device operation, metadata history, resource modification, recording-end events, embedded color-profile creation, and Photoshop layer editing. Capture-oriented maker-note fields such as `SonyDateTime`, `SonyDateTime2`, `PanasonicDateTime`, and Olympus `DateTimeUTC` remain eligible.

The embedded pass preserves ExifTool's raw date/time values instead of applying a global output format. This prevents partial IPTC fields from becoming artificial full timestamps: `IPTC:DateCreated` remains date-only, `IPTC:TimeCreated` remains time-only, and neither is offered independently. Composite fields such as `Composite:DateTimeCreated`, which genuinely combine the date and time components, remain eligible. Fractional seconds and timezone suffixes on complete timestamps are accepted.

The remaining timestamps may come from EXIF, XMP, IPTC, QuickTime, maker notes, composite tags, and other metadata families supported by ExifTool. `-G0:1:4` preserves the metadata type, exact storage location, and duplicate instance number.

When distinct complete embedded timestamps are found anywhere in a related group, the script lists their full ExifTool source paths and asks the user to choose one. Each filename is displayed once per option, followed by all matching fields for that file. Duplicate occurrences of the same field are collapsed.

## Filesystem fallback

`FileModifyDate` is requested in a separate ExifTool call only when the complete related group contains no usable embedded timestamp.

The fallback is restricted to base image files: the file must be identified as an image and its stem must exactly match the base stem selected for the related group. Sidecars and suffix-derived files such as `.xmp`, `.pp3`, or `IMG_0001-edit.jpg` are never used as filesystem-date sources.

`FileCreateDate` and `FileAccessDate` are never requested for fallback selection.

## Requirements

- Python 3.8 or newer
- Phil Harvey's ExifTool 12.15 or newer, available as `exiftool` or `exiftool.exe` on the system `PATH`
- PyExifTool 0.5.4 or newer

Install the Python dependency with:

```bash
python -m pip install -r requirements.txt
```

PyExifTool is the Python wrapper imported by the script as `exiftool`. The separate ExifTool executable must also be installed; installing the Python package does not install that executable.
