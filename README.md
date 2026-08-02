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

### Metadata updates in corrected copies

When the user accepts a timezone or daylight-saving correction, the script first creates the normal collision-safe copy in `classified` and then edits that copy in place. No separate application-level temporary copy is built, and the original source file remains untouched. ExifTool may still use its own internal safe-write mechanism while replacing metadata.

The correction pass reads existing `Time:All` values and applies the approved correction by semantics rather than by camera brand:

- complete local timestamps without an offset are shifted by the approved delta;
- timestamps carrying an inline UTC offset keep their wall-clock value and receive the corrected offset;
- EXIF timestamps paired with `OffsetTime`, `OffsetTimeOriginal`, or `OffsetTimeDigitized` keep their wall-clock value while the existing offset field is corrected;
- paired IPTC date-only/time-only fields are corrected together, including midnight rollover;
- known UTC references such as `GPSDateTime` and `DateTimeUTC` are not altered;
- embedded-resource and application-history fields such as ICC profile dates, Photoshop layer dates, FlashPix extension dates, `MetadataDate`, and `HistoryWhen` are not treated as camera-local clock values.

The same generic pass covers writable EXIF, XMP, IPTC, QuickTime, maker-note, image, video, and sidecar fields. Read-only or unsupported fields are reported as skipped instead of preventing other writable fields from being corrected.

Existing standalone offset fields such as `OffsetTimeOriginal`, `OffsetTimeDigitized`, `OffsetTime`, `TimeZoneOffset`, `TimeZone`, and `TimeOffset` are updated when their representation can be interpreted safely. Missing metadata fields are not created.

Existing camera daylight-saving settings are also updated when ExifTool can write them. This includes direct `DaylightSavings` fields and the active Pentax/Ricoh hometown or destination DST field selected by `WorldTimeLocation`. The raw ON encoding is preserved when present; Canon's documented 60-minute ON representation is used when a zero-valued Canon field must be enabled. Unsupported or read-only maker-note fields are left unchanged and reported.

The filesystem `FileModifyDate` is set from the source file's timestamp plus the approved correction on every corrected copy. This includes groups classified from the system fallback and makes repeated runs idempotent. `FileCreateDate` is also corrected when the operating system exposes it and ExifTool can write it.

After writing, the script reads the classified copy back and verifies writable metadata semantically. If a newly created copy cannot be corrected safely, it is removed. After correction, collision candidates are compared again so repeated runs recognize an existing corrected output rather than retaining an unnecessary numbered duplicate.

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
