FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

# new.py is the entry point; it imports app.py (core backend) from the same folder
CMD ["gunicorn", "--chdir", "dashboard", "new:app", "--workers", "1", "--threads", "4", "--timeout", "180", "--bind", "0.0.0.0:10000"]
