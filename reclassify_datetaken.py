import glob
import os
import shutil
import pathlib

directory = pathlib.Path("./example_files")
for File in directory.iterdir():
	print(File)
input("Press any key to exit")