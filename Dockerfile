FROM python:3.11-slim-bullseye

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# Layer 1: Basic tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Layer 2: Add Chrome apt repo
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
        http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list

# Layer 3: Install Chrome (apt retries 3x; this layer is now CACHED from prior build)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        -o Acquire::Retries=3 \
        google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer 4: Core packages — small, install fast (cached once successful)
RUN pip install --no-cache-dir --retries 5 --timeout 120 \
    feedparser \
    pandas \
    python-dateutil \
    supabase \
    requests \
    urllib3 \
    beautifulsoup4 \
    lxml \
    selenium \
    webdriver-manager \
    nest_asyncio

# Layer 5: Playwright (separate layer so Layer 4 stays cached if this retries)
RUN pip install --no-cache-dir --retries 5 --timeout 300 playwright \
    && playwright install --with-deps chromium

# Layer 6: Heavy optional packages (batch_extract.py etc. — not in main pipeline)
RUN pip install --no-cache-dir --retries 5 --timeout 300 \
    trafilatura \
    newspaper3k \
    extruct

# Layer 7: Copy project files
COPY . .

CMD ["/bin/bash"]
