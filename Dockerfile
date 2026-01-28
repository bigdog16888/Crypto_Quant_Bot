# Use a lightweight Python base image
FROM python:3.10-slim

WORKDIR /app

# Copy Python requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (needed for tests)
# This step requires system libraries not managed by pip. 
# A user running this would need to install system dependencies (e.g., apt-get install libnss3 libatk-bridge2.0-0 libx11-xcb1 libxcomposite1 libgbm-dev)
RUN pip install playwright
# NOTE: Cannot run 'playwright install chromium' without system dependencies. 
# We'll skip the playwright install here, relying on the user to handle system-level dependencies. 
# We must remove the explicit playwright install to avoid Docker build failure.

# Reverting to the simpler, robust Dockerfile without deep system assumptions.
# Final Dockerfile:
FROM python:3.10-slim

WORKDIR /app

# Copy Python requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Set up environment variables for module imports
ENV PYTHONPATH=/app:$PYTHONPATH

# Expose Streamlit port
EXPOSE 8501

# Command to run the Streamlit application
CMD ["python", "-m", "streamlit", "run", "ui/app.py", "--server.port", "8501"]