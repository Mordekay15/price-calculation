FROM python:3.12-slim

# System libraries the STEP reader (cadquery-ocp) needs
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libx11-6 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x bin/sparrow bin/dwg2dxf

# Saved price lists go to /data, which is kept outside the container
RUN mkdir /data
WORKDIR /data
EXPOSE 8501
CMD ["streamlit", "run", "/app/app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false", "--server.maxUploadSize=500", "--client.toolbarMode=viewer"]
