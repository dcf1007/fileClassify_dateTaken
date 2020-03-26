import glob
import os
import shutil
import pathlib
import PIL.Image
from datetime import datetime

#Disable max pixels to avoid errors in RAW files and panoramas
PIL.Image.MAX_IMAGE_PIXELS = None

directory = pathlib.Path(input("Please write(or drag) the path to process: "))

def get_date(filename):
	'''
	The method returns a tuple (isEXIF, date) where the first value indicates 
	if the date is from EXIF or from the OS and the second contains the date itself
	'''
	#The EXIF index to the DateOriginal code is 36867 (0x9003)
	#The EXIF time is always in the following format: "YYYY:MM:DD HH:MM:SS" 
	try:
		with PIL.Image.open(filename) as image:
			image.verify()
			return (1, datetime.strptime(image._getexif()[36867], "%Y:%m:%d %H:%M:%S"))
	except:
		return (0, datetime.fromtimestamp(os.path.getmtime(filename)))

for File in directory.iterdir():
	if os.path.isfile(File):
		FileDate = None
		#Get all the files with the same stem
		SameStemFiles = File.parent.glob(str(File.stem) + ".*")
		for SameStemFile in SameStemFiles:
			if FileDate:
				SameStemFileDate = get_date(SameStemFile)
				#If both dates come from the same source (either OS or EXIF) and
				#If the new file has an older date than the one stored
				if FileDate[0] == SameStemFileDate[0] and FileDate[1] > SameStemFileDate[1]:
					FileDate = SameStemFileDate #Replace the stored date with the new one
				#If the date stored is from the OS and the new one is from EXIF
				elif FileDate[0] < SameStemFileDate[0]:
					FileDate = SameStemFileDate #Replace the stored date with the new one
			else:
				FileDate = get_date(SameStemFile)
		
		print(FileDate[1].strftime("%Y-%m-%d %H:%M:%S") + "\t" + str(File.name))
		NewFolder = str(File.parent) + os.path.sep + FileDate[1].strftime("%Y-%m-%d")
		if os.path.exists(NewFolder) == False:
			os.mkdir(NewFolder)
			shutil.move(File, NewFolder + os.path.sep + str(File.name))
			if os.path.exists(str(File.parent) + os.path.sep + str(File.stem) + ".xmp"):
				shutil.move(str(File.parent) + os.path.sep + str(File.stem) + ".xmp", NewFolder + os.path.sep + str(File.stem) + ".xmp")
		elif os.path.isfile(NewFolder) == True:
				print(FileDate[1].strftime("%Y-%m-%d") + " exists as a file. Not moving " + str(File.name))
		else:
			shutil.move(File, NewFolder + os.path.sep + str(File.name))
			if os.path.exists(str(File.parent) + os.path.sep + str(File.stem) + ".xmp"):
				shutil.move(str(File.parent) + os.path.sep + str(File.stem) + ".xmp", NewFolder + os.path.sep + str(File.stem) + ".xmp")
input("Press any key to exit")