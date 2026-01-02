# Use lightweight python
FROM python:3.10

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 9001

# Run server
CMD ["uvicorn", "biometric:app", "--host", "0.0.0.0", "--port", "9001"]



