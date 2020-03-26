import glob
import os
import shutil
import pathlib
import PIL.Image
from datetime import datetime

directory = pathlib.Path(input("Please write(or drag) the path to process: "))

def get_date(filename):
	#The EXIF index to the DateOriginal code is 36867 (0x9003)
	#The EXIF time is always in the following format: "YYYY:MM:DD HH:MM:SS" 
	try:
		with PIL.Image.open(filename) as image:
			image.verify()
			return datetime.strptime(image._getexif()[36867], "%Y:%m:%d %H:%M:%S")
	except:
		return datetime.fromtimestamp(os.path.getmtime(filename))

for File in directory.iterdir():
	if os.path.isfile(File):
		FileDate = get_date(File)
		print(FileDate.strftime("%Y-%m-%d %H:%M:%S") + "\t" + str(File.name))
		NewFolder = str(File.parent) + os.path.sep + FileDate.strftime("%Y-%m-%d")
		if os.path.exists(NewFolder) == False:
			os.mkdir(NewFolder)
			shutil.move(File, NewFolder + os.path.sep + str(File.name))
		elif os.path.isfile(NewFolder) == True:
				print(FileDate.strftime("%Y-%m-%d") + " exists as a file. Not moving " + str(File.name))
		else:
			shutil.move(File, NewFolder + os.path.sep + str(File.name))
input("Press any key to exit")