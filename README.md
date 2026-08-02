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
ExtensionCreateDate
ExtensionModifyDate
```

These cover camera/device operation, metadata history, resource modification, recording-end events, embedded color-profile creation, Photoshop layer editing, and FlashPix extension-object creation or modification. Capture-oriented local fields such as `SonyDateTime`, `SonyDateTime2`, `PanasonicDateTime`, `RicohDate`, and ordinary EXIF/XMP creation dates remain eligible.

UTC counterparts such as `DateTimeUTC` and `GPSDateTime` are not offered as competing local creation dates. They are retained separately as timezone evidence.

The embedded pass preserves ExifTool's raw date/time values instead of applying a global output format. This prevents partial IPTC fields from becoming artificial full timestamps: `IPTC:DateCreated` remains date-only, `IPTC:TimeCreated` remains time-only, and neither is offered independently. Composite fields such as `Composite:DateTimeCreated`, which genuinely combine the date and time components, remain eligible. Fractional seconds and timezone suffixes on complete timestamps are accepted.

The parser defines one-second resolution as the canonical classification precision. This is applied while the raw timestamp is parsed, before candidates are grouped or displayed. A whole-second EXIF value such as `2024:09:14 00:40:47` and a higher-precision XMP or Composite value such as `2024-09-14T00:40:47.37` therefore represent one classification timestamp rather than two visually identical choices.

The remaining timestamps may come from EXIF, XMP, IPTC, QuickTime, maker notes, composite tags, and other metadata families supported by ExifTool. `-G0:1:4` preserves the metadata type, exact storage location, and duplicate instance number.

When distinct complete embedded timestamps are found anywhere in a related group, the script lists their full ExifTool source paths and asks the user to choose one. Each filename is displayed once per option, followed by all matching fields for that file. Duplicate occurrences of the same field are collapsed.

## Timezone and daylight-saving validation

At startup, the script asks for an IANA timezone used to interpret local capture times. Pressing Enter selects `Europe/Berlin`, which represents Central European Time and Central European Summer Time (`CET`/`CEST`) with the applicable historical transition rules. A different IANA name such as `Europe/London` or `America/New_York` may be entered for files photographed elsewhere.

Timezone handling is evidence-based rather than dispatched by camera brand. The same logic applies to Canon, Nikon, Sony, Fujifilm, Kodak, Olympus/OM System, Pentax, Ricoh, Panasonic/Lumix, Leica, GoPro, DJI, Phase One, Sigma, Hasselblad, phones, legacy cameras, and unknown or future devices whenever ExifTool exposes equivalent metadata.

The script collects three general forms of evidence:

1. Explicit offsets attached to timestamps or stored in fields such as `OffsetTimeOriginal`, `OffsetTimeDigitized`, `TimeZoneOffset`, `TimeZone`, or `TimeOffset`.
2. UTC counterparts such as `DateTimeUTC` and `GPSDateTime`, from which the effective local UTC offset can be derived.
3. Camera configuration such as `DaylightSavings`, or the active Pentax/Ricoh hometown/destination profile selected through `WorldTimeLocation`.

Only the profile relationship requires special handling; it is resolved from tag semantics and does not check the camera make or model.

Camera configuration and UTC-reference evidence are accepted only from exact-base image or video files. A selected XMP sidecar may still contribute an explicit offset attached directly to its selected timestamp. Sidecars and suffix-derived media do not define the originating camera's configuration.

The selected local time is evaluated with the IANA timezone database rather than a fixed month/day approximation. This handles historical rules, nonexistent times during the spring transition, and the repeated hour during the autumn transition.

The default timezone alone is an assumption and never proves that the camera clock was wrong. The script prompts only when available offset, UTC, or DST metadata contradicts the selected timezone. It shows the expected state, the conflicting evidence, and any defensible corrected time. The user may keep the recorded wall-clock time or apply a correction for classification.

A corrected classification value is reported as `METADATA_TIMEZONE_CORRECTED`.

### EXIF updates in corrected copies

When the user accepts a timezone or daylight-saving correction, the script applies the same exact time shift to existing common EXIF timestamp fields in every classified image copy in that related group:

```text
DateTimeOriginal
CreateDate
ModifyDate
```

Each field is shifted from its own existing value. The script does not replace all three fields with one timestamp, so legitimate differences between capture, digitization, and modification times are preserved. Duplicate EXIF locations are handled independently. Missing EXIF date fields are not created, and separate subsecond fields remain unchanged.

For every shifted timestamp type, the corresponding standard EXIF UTC-offset field is set to the corrected IANA-zone offset when that local time has one unambiguous offset:

```text
OffsetTimeOriginal
OffsetTimeDigitized
OffsetTime
```

The repeated autumn hour can have two valid UTC offsets. In that ambiguous case, the timestamp is shifted as selected, but the script does not guess an offset value.

Metadata is written only to a temporary output copy. ExifTool then reads the temporary file back, and the script verifies every shifted EXIF timestamp and every intended offset before collision-safe placement in `classified`. If the metadata write or verification fails, the temporary file is removed and no partially corrected destination is retained. Binary-duplicate comparison uses the final corrected bytes, so a repeated run can recognize an already-corrected copy.

Vendor-specific maker-note daylight-saving settings are not rewritten because their encodings and writability vary by manufacturer. GPS and UTC-reference fields are also left unchanged because they represent the absolute instant used to validate the correction. This write step is deliberately limited to EXIF: XMP, IPTC, QuickTime, and sidecar timestamps are not changed.

Original source files and their metadata are never modified.

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
