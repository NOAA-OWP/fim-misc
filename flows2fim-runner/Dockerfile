# Use the GDAL Ubuntu image directly
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.10.2

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
# Try uncommenting below if running out of memory during raster processing
# ENV GDAL_CACHEMAX=1024

# Create and set working directory
WORKDIR /app

# Install Python pip and additional dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3-pip \
    wget \
    curl \
    unzip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    rm /usr/lib/python3.*/EXTERNALLY-MANAGED

# Copy requirements file first (for better caching)
COPY requirements.txt /app/

# Install Python dependencies from requirements.txt
RUN pip3 install -r requirements.txt

# Copy application files into the image
COPY src/ /app/src/
COPY config/ /app/config/
COPY flows2fim_0_3_0 /usr/local/bin/flows2fim

# Make flows2fim executable
RUN chmod +x /usr/local/bin/flows2fim

# Set the default entrypoint
ENTRYPOINT ["python3", "/app/src/extent_worker.py"]
