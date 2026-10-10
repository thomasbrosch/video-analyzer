FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY video_analyzer ./video_analyzer
RUN pip install --no-cache-dir ".[web]"
ENV PORT=8080
# One process, many threads: jobs live in memory. --timeout 0 because analyses can run for minutes.
CMD exec gunicorn -k gthread --threads 8 -w 1 --timeout 0 -b 0.0.0.0:$PORT video_analyzer.webapp:app
