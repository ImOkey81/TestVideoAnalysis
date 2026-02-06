
FROM python:3.9-slim


WORKDIR /app

COPY . .


RUN pip install --no-cache-dir -r requirements.txt


RUN apt-get update && apt-get install -y libsm6 libxext6 libxrender-dev
RUN apt-get install -y ffmpeg
RUN pip install opencv-python-headless


CMD ["python", "AnalisesAI.py"]
