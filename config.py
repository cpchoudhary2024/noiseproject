# Configuration file for Noise Analysis Platform

# Flask Configuration
FLASK_ENV = 'development'  # 'development' or 'production'
DEBUG = True  # Set to False in production
SECRET_KEY = 'your-secret-key-change-this-in-production'

# File Upload Configuration
MAX_FILE_SIZE_MB = 50  # Maximum file size in MB
ALLOWED_EXTENSIONS = ['csv', 'xlsx', 'xls']
UPLOAD_FOLDER = './uploads'

# Server Configuration
HOST = '0.0.0.0'  # Listen on all interfaces
PORT = 5000

# CORS Configuration
CORS_ORIGINS = '*'  # Restrict in production

# Analysis Configuration
MIN_DATA_POINTS = 10  # Minimum data points required for analysis
TREND_ANALYSIS_ENABLED = True
PEAK_DETECTION_ENABLED = True

# Standards Configuration
ISO_STANDARD = 'ISO 1996-1:2016'
EPA_STANDARD = 'EPA Noise Abatement Criteria'
OSHA_STANDARD = 'OSHA PEL'

# Report Configuration
REPORT_FORMAT = 'HTML'  # Can be extended to 'PDF', etc.
CHARTS_LIBRARY = 'plotly'

# Logging
LOG_LEVEL = 'INFO'  # 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
LOG_FILE = './logs/app.log'

# Performance
CACHE_ENABLED = True
CACHE_TIMEOUT = 3600  # 1 hour

# Database (for future use)
DATABASE_URL = 'sqlite:///./noise_analysis.db'

# Email Notifications (for future use)
ENABLE_EMAIL_NOTIFICATIONS = False
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587

# Advanced Features
BATCH_PROCESSING = False
REAL_TIME_MONITORING = False
FREQUENCY_ANALYSIS = False
