# fileClassify_dateTaken

Python script that takes the files directly inside a selected directory and copies them into date-based subdirectories according to supported EXIF date fields. If none of those EXIF fields is available, the filesystem modification time is used.

The script does not scan subdirectories. Classified copies are written to `classified/YYYY-MM-DD`, while damaged files or files with unreliable image metadata are copied to `unclassified` for manual review. Original files are not moved or overwritten.

## Requirements

- Python 3.8 or newer
- Phil Harvey's ExifTool 12.15 or newer, available as `exiftool` or `exiftool.exe` on the system `PATH`
- PyExifTool 0.5.4 or newer

Install the Python dependency with:

```bash
python -m pip install -r requirements.txt
```

PyExifTool is the Python wrapper imported by the script as `exiftool`. The separate ExifTool executable must also be installed; installing the Python package does not install that executable.
