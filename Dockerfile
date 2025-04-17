FROM python:3.13-alpine

# Expose the application port
EXPOSE 7777/tcp

# Ensures Python output is flushed immediately (no buffering)
ENV PYTHONUNBUFFERED=1

# Set working directory inside the container
WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check --no-compile -r requirements.txt

# Copy source files
COPY pywsgi.py ./
COPY plex.py ./
COPY plex_tmsid.csv ./

# Run the server application
CMD ["python3", "pywsgi.py"]