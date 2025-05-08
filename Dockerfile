FROM mcr.microsoft.com/playwright/python:v1.51.0-noble 

# --- convert all Ubuntu mirrors from http:// to https:// ----------------
RUN sed -Ei 's|http://([a-z0-9.-]*ubuntu\.com)|https://\1|g' /etc/apt/sources.list

# optional: if there are *.list files in sources.list.d/
RUN find /etc/apt/sources.list.d -type f -print0 | xargs -0 \
    sed -Ei 's|http://([a-z0-9.-]*ubuntu\.com)|https://\1|g'

# install what you need
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl build-essential libpoppler-cpp-dev poppler-utils \
    && rm -rf /var/lib/apt/lists/*
    
WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps    
 
RUN python -m pip install --upgrade pip

ENV PORT=8007

EXPOSE 8007

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8007"]