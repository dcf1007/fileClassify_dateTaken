import glob
import os
import shutil
import pathlib
import PIL.Image
from datetime import datetime

#Disable max pixels to avoid errors in RAW files and panoramas
PIL.Image.MAX_IMAGE_PIXELS = None

DATETYPE = {0:"OS_DATE", 1:"EXIF"}

directory = pathlib.Path(input("Please write(or drag) the path to process: ").strip("\""))

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
			try:
				return (1, datetime.strptime(image._getexif()[36867], "%Y:%m:%d %H:%M:%S"))
			except:
				return (1, datetime.strptime(image.tag[36867][0], "%Y:%m:%d %H:%M:%S"))
	except:
		return (0, datetime.fromtimestamp(os.path.getmtime(filename)))

for File in directory.iterdir():
	if os.path.exists(File):
		if os.path.isfile(File):
			print(str(File.name) + "\t--->\t", end="")
			
			FileDate = None
			#Get all the files with the same stem
			SameStemFiles = list(File.parent.glob(str(File.stem) + ".*"))
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
			
			#Copy all the files sharing the stem.
			NewFolder = str(File.parent) + os.path.sep + FileDate[1].strftime("%Y-%m-%d")

			for SameStemFile in SameStemFiles:
				print(FileDate[1].strftime("%Y-%m-%d %H:%M:%S") + "\t" + DATETYPE[FileDate[0]] , end="")
				if os.path.exists(NewFolder) == False:
					os.mkdir(NewFolder)
					shutil.move(SameStemFile, NewFolder + os.path.sep + str(SameStemFile.name))
					print("(OK)")
				elif os.path.isfile(NewFolder) == True:
					print(FileDate[1].strftime("%Y-%m-%d") + " exists as a file. Not moving " + str(SameStemFile.name))
				else:
					shutil.move(SameStemFile, NewFolder + os.path.sep + str(SameStemFile.name))
					print("(OK)")
input("Press any key to exit")