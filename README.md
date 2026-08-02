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

The parser defines one-second resolution as the canonical classification precision. This is applied while the raw timestamp is parsed, before candidates are grouped or displayed. A whole-second EXIF value such as `2024:09:14 00:40:47` and a higher-precision XMP or Composite value such as `2024-09-14T00:40:47.37` therefore represent one classification timestamp rather than two visually identical choices.

The remaining timestamps may come from EXIF, XMP, IPTC, QuickTime, maker notes, composite tags, and other metadata families supported by ExifTool. `-G0:1:4` preserves the metadata type, exact storage location, and duplicate instance number.

When distinct complete embedded timestamps are found anywhere in a related group, the script lists their full ExifTool source paths and asks the user to choose one. Each filename is displayed once per option, followed by all matching fields for that file. Duplicate occurrences of the same field are collapsed.

## Timezone and daylight-saving validation

At startup, the script asks for an IANA timezone used to interpret local capture times. Pressing Enter selects `Europe/Berlin`, which represents Central European Time and Central European Summer Time (`CET`/`CEST`) with the applicable historical EU transition rules. A different IANA name such as `Europe/London` or `America/New_York` may be entered for files photographed elsewhere.

In addition to `Time:All`, ExifTool is asked for camera timezone context including `DaylightSavings`, `TimeZone`, `TimeZoneCity`, `OffsetTimeOriginal`, `OffsetTimeDigitized`, and `TimeZoneOffset`. These fields are never treated as independent classification dates.

After one metadata timestamp has been selected for a related group, the script checks `DaylightSavings` only on base image files. Sidecars and suffix-derived images do not control the check. The selected date is evaluated against the timezone database rather than a fixed month/day approximation, so historical rule changes, the spring transition gap, and the repeated autumn hour can be distinguished.

When the camera's daylight-saving setting contradicts the selected timezone's unambiguous state, the script shows:

- the camera setting and its metadata source;
- the expected timezone abbreviation and UTC offset;
- any explicit UTC offsets carried by the selected timestamp fields;
- the original time and the timezone-derived corrected time.

The user then chooses whether to keep the recorded wall-clock time or apply the daylight-saving adjustment for classification. The original image and its metadata are never rewritten. A corrected value is reported as `METADATA_DST_CORRECTED`.

An explicit timestamp offset is useful corroborating evidence. For example, an XMP timestamp carrying `+02:00` already agrees with `CEST`; if a maker-note `DaylightSavings` flag says Off, the flag may be stale while the recorded wall-clock time is already correct. The script therefore presents the evidence and leaves the correction decision to the user instead of changing the time automatically.

## Filesystem fallback

`FileModifyDate` is requested in a separate ExifTool call only when the complete related group contains no usable embedded timestamp.

The fallback is restricted to base image files: the file must be identified as an image and its stem must exactly match the base stem selected for the related group. Sidecars and suffix-derived files such as `.xmp`, `.pp3`, or `IMG_0001-edit.jpg` are never used as filesystem-date sources.

`FileCreateDate` and `FileAccessDate` are never requested for fallback selection.

## Requirements

- Python 3.9 or newer
- Phil Harvey's ExifTool 12.15 or newer, available as `exiftool` or `exiftool.exe` on the system `PATH`
- PyExifTool 0.5.4 or newer
- `tzdata` 2024.1 or newer, providing IANA timezone data on systems such as Windows that do not normally include it

Install the Python dependencies with:

```bash
python -m pip install -r requirements.txt
```

PyExifTool is the Python wrapper imported by the script as `exiftool`. The separate ExifTool executable must also be installed; installing the Python package does not install that executable.
