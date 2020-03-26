import glob
import os
import shutil
import pathlib

directory = pathlib.Path("./example_files")
for File in directory.iterdir():
	if os.path.isfile(File):
		print(File)
	else:
		print(str(File) + " <--- Folder: ignored")
input("Press any key to exit")