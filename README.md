---
title: Noise Analysis Platform
emoji: 📊
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Noise Data Analysis Platform

A comprehensive web-based platform for analyzing noise data with ISO 1996 and EPA standards compliance checking, advanced statistics, and detailed reporting capabilities.

## Features

### 📊 Core Features
- **File Upload**: Support for CSV and Excel files (.xlsx, .xls)
- **Data Preview**: Quick preview of uploaded data before analysis
- **Comprehensive Analysis**: Advanced statistical analysis including:
  - Mean, median, standard deviation
  - Percentile analysis (L5, L10, L50, L90, L95)
  - Trend analysis with linear regression
  - Peak detection and analysis
  - Distribution analysis
  - Data quality assessment

### 📈 Visualizations
- Noise level distribution charts
- Percentile level graphs
- Compliance comparison charts
- Interactive Plotly charts

### ✅ Standards Compliance
- **ISO 1996-1:2016**: Environmental Noise Assessment
  - Sensitive areas (hospitals, schools): 50 dB
  - Residential areas: 55 dB
  - Mixed residential/commercial: 60 dB
  - Commercial areas: 65 dB
  - Industrial areas: 75 dB

- **EPA Noise Abatement Criteria (NAC)**
  - Residential: 55 dB
  - Commercial: 65 dB
  - Industrial: 75 dB
  - Federal lands: 60 dB

- **OSHA Permissible Exposure Limits (PEL)**
  - 8-hour TWA: 90 dB
  - Action Level: 85 dB

### 📄 Reporting
- Comprehensive reports (HTML)
- ISO compliance reports
- EPA compliance reports
- Summary reports
- Data export (Excel with multiple sheets)

### 👥 User-Friendly Interface
- Intuitive 6-step workflow
- Interactive tabs for different analysis views
- Real-time status indicators
- Mobile-responsive design
- Easy-to-understand interpretations for non-technical users

## Installation

### Prerequisites
- Python 3.8+
- pip (Python package manager)

### Setup Instructions

1. **Clone or download the project**
```bash
cd noise-analysis-platform/backend
```

2. **Create a virtual environment** (optional but recommended)
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Run the Flask application**
```bash
python app.py
```

5. **Open in browser**
```
http://localhost:5001
```

## Usage

### Step 1: Upload Data
- Click the upload area or drag and drop your CSV/Excel file
- Supported file formats:
  - `.csv` - Comma-separated values
  - `.xlsx` - Excel 2007+
  - `.xls` - Excel 97-2003

### Step 2: Preview Data
- Review the data preview to ensure correct file format
- Check the number of records and columns
- Proceed if data looks correct

### Step 3: Analysis
- Automatic comprehensive analysis runs
- Analyzes noise levels, trends, and compliance

### Step 4: View Results
- **Executive Summary**: Key statistics and interpretations
- **Statistics**: Detailed statistical measures
- **Compliance**: Compliance against various standards
- **Charts**: Interactive visualizations
- **ISO/EPA Standards**: Detailed standards compliance

### Step 5: Generate Reports
- Generate comprehensive HTML reports
- Create ISO-specific reports
- Create EPA-specific reports
- Export data to Excel with analysis

## Data Format

### Expected CSV Format
```csv
Timestamp,Location,Noise_Level_dB,Frequency_Hz
2024-01-15 08:00:00,Site A,65.2,1000
2024-01-15 08:15:00,Site A,64.8,1000
2024-01-15 08:30:00,Site A,66.1,1000
```

### Column Requirements
- **At least one column** containing noise measurements in dB
- Column names should ideally contain: `db`, `decibel`, `level`, `sound`, `noise`, `spl`, `leq`, etc.
- Optional: Time/date column for temporal analysis
- Optional: Location or metadata columns

### Example with Multiple Measurements
```csv
DateTime,Area,Noise_Database,Noise_Level_Avg,Noise_Level_Max
2024-01-15 08:00:00,Residential,65.2,66.1
2024-01-15 08:30:00,Residential,64.8,65.9
2024-01-15 09:00:00,Residential,66.1,67.3
```

## Key Metrics Explained

### Leq (Equivalent Level)
The average noise level over the measurement period. Most commonly used metric for environmental noise.

### Percentile Levels
- **L5**: Noise level exceeded 5% of the time (higher values)
- **L10**: Noise level exceeded 10% of the time
- **L50**: Median noise level (50th percentile)
- **L90**: Noise level exceeded 90% of the time (lower quiet periods)
- **L95**: Noise level exceeded 95% of the time (quietest periods)

### Coefficient of Variation (CV)
Standardized measure of variability. Higher CV indicates more variable noise levels.

### Trend Analysis
Shows if noise levels are increasing, decreasing, or stable over time using linear regression.

## ISO 1996 Standards

### Daytime vs. Nighttime
- **Daytime**: 06:00 - 22:00
- **Nighttime**: 22:00 - 06:00

### Health Implications
- **< 40 dB**: No significant health effects
- **40-50 dB**: Minor annoyance, possible sleep disturbance
- **50-60 dB**: Moderate annoyance
- **60-70 dB**: High annoyance, speech interference
- **70-80 dB**: Severe annoyance, hearing damage risk
- **> 80 dB**: DANGEROUS - Risk of hearing damage

## Troubleshooting

### File Upload Issues
- Ensure file is CSV or Excel format
- Check that file size is less than 50 MB
- Verify the file is not corrupted

### No Noise Columns Detected
- Verify column name contains keywords: `db`, `level`, `noise`, `sound`, `spl`, `leq`
- Alternatively, ensure numeric columns exist (excluding time/date/ID columns)

### Analysis Errors
- Ensure data contains numeric values in noise columns
- Check for proper date/time formatting if using time-series analysis
- Verify no special characters in column names

## Project Structure
```
noise-analysis-platform/
├── backend/
│   ├── app.py                          # Main Flask application
│   ├── requirements.txt                # Python dependencies
│   └── analysis/
│       ├── noise_analyzer.py           # Core analysis engine
│       ├── iso_epa_standards.py        # Standards compliance checking
│       └── report_generator.py         # Report generation
├── frontend/
│   ├── templates/
│   │   └── index.html                  # Main HTML template
│   └── static/
│       ├── css/
│       │   └── style.css               # Styling
│       └── js/
│           └── app.js                  # Frontend logic
└── uploads/                            # Uploaded files storage
```

## Performance Notes

- Typical analysis time: < 5 seconds for 1000 records
- Supports files up to 50 MB
- All processing happens server-side for accuracy

## Future Enhancements

- Real-time noise monitoring
- Database storage for historical analysis
- Advanced frequency analysis (octave bands)
- Machine learning for predictive analysis
- Integration with external noise databases
- Multiple file batch processing
- Custom threshold configuration

## Compliance & Disclaimer

This platform provides analysis based on:
- ISO 1996-1:2016 Environmental Noise
- EPA Noise Abatement Criteria
- OSHA Occupational Noise Exposure Standards

**Disclaimer**: This analysis is for informational purposes. For official compliance determinations and professional acoustic assessments, consult a certified acoustic engineer or environmental specialist.

## Support

For issues or questions:
1. Check the troubleshooting section
2. Verify your data format
3. Review sample CSV files

## License

[Your License Here]

## Version

Version 1.0.0
Last Updated: January 2024
