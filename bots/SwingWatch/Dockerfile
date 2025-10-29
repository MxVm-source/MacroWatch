FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN apt-get update && apt-get install -y --no-install-recommends     build-essential gcc g++ libfreetype6-dev libpng-dev pkg-config &&     rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "-m", "bot.main"]
