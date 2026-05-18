#!/usr/bin/env bash

# Installation and Troubleshooting Helper
# This script helps with common setup and troubleshooting issues

set -e

echo "🔧 Noise Analysis Platform - Troubleshooting Helper"
echo "==================================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
check=$(echo -e "${GREEN}✓${NC}")
error=$(echo -e "${RED}✗${NC}")
warning=$(echo -e "${YELLOW}!${NC}")

show_menu() {
    echo "${BLUE}Choose an option:${NC}"
    echo "1. Fresh Installation"
    echo "2. Check Dependencies"
    echo "3. Verify Python Setup"
    echo "4. Clear Cache and Reinstall"
    echo "5. Test API Connection"
    echo "6. View Common Issues"
    echo "7. Generate Sample Data"
    echo "8. Exit"
    echo ""
    read -p "Enter choice (1-8): " choice
}

fresh_install() {
    echo ""
    echo "${BLUE}Starting Fresh Installation...${NC}"
    
    cd backend
    
    # Check Python
    if ! command -v python3 &> /dev/null; then
        echo "${error} Python 3 not found"
        exit 1
    fi
    
    echo "${check} Python 3 found"
    
    # Remove old venv
    if [ -d "venv" ]; then
        echo "Removing old virtual environment..."
        rm -rf venv
    fi
    
    # Create new venv
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "${check} Virtual environment created"
    
    # Activate venv
    source venv/bin/activate
    echo "${check} Virtual environment activated"
    
    # Install dependencies
    echo "Installing dependencies..."
    pip install --upgrade pip
    pip install -r requirements.txt
    echo "${check} Dependencies installed"
    
    # Create directories
    echo "Creating project directories..."
    mkdir -p ../uploads ../logs
    echo "${check} Directories created"
    
    echo ""
    echo "${GREEN}✓ Fresh installation complete!${NC}"
    echo "Run 'python app.py' to start the server"
}

check_dependencies() {
    echo ""
    echo "${BLUE}Checking Dependencies...${NC}"
    
    cd backend
    source venv/bin/activate 2>/dev/null || true
    
    packages=("Flask" "pandas" "numpy" "plotly" "openpyxl")
    
    for package in "${packages[@]}"; do
        if python3 -c "import ${package,,}" 2>/dev/null; then
            echo "${check} $package installed"
        else
            echo "${error} $package NOT installed"
        fi
    done
}

verify_python() {
    echo ""
    echo "${BLUE}Verifying Python Setup...${NC}"
    
    echo "Python version:"
    python3 --version
    
    echo ""
    echo "Python location:"
    which python3
    
    echo ""
    echo "Python executable:"
    python3 -c "import sys; print(sys.executable)"
}

clear_and_reinstall() {
    echo ""
    echo "${YELLOW}This will remove venv and reinstall everything${NC}"
    read -p "Continue? (y/n) " -n 1 -r
    echo ""
    
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        cd backend
        rm -rf venv
        python3 -m venv venv
        source venv/bin/activate
        pip install --upgrade pip
        pip install -r requirements.txt
        pip install -r requirements-dev.txt
        echo "${check} Complete reinstall finished"
    fi
}

test_api() {
    echo ""
    echo "${BLUE}Testing API Connection...${NC}"
    
    if command -v curl &> /dev/null; then
        echo "Testing health endpoint..."
        response=$(curl -s http://localhost:5001/health)
        
        if echo "$response" | grep -q "ok"; then
            echo "${check} API is responding"
            echo "Response: $response"
        else
            echo "${error} API is not responding"
            echo "Make sure Flask server is running: python app.py"
        fi
    else
        echo "${warning} curl not found. Please manually test at http://localhost:5001/health"
    fi
}

show_common_issues() {
    echo ""
    echo "${BLUE}Common Issues and Solutions${NC}"
    echo ""
    
    echo "1. Port 5001 already in use"
    echo "   Solution: Kill existing process or use different port"
    echo "   Code: kill -9 \$(lsof -t -i:5001)"
    echo ""
    
    echo "2. ModuleNotFoundError: No module named 'flask'"
    echo "   Solution: Activate virtual environment"
    echo "   Code: source venv/bin/activate"
    echo ""
    
    echo "3. File upload fails"
    echo "   Solution: Check if uploads directory exists"
    echo "   Code: mkdir -p ../uploads"
    echo ""
    
    echo "4. Analysis takes too long"
    echo "   Solution: Large files take longer. Check file size"
    echo "   Code: wc -l your_file.csv"
    echo ""
    
    echo "5. Charts not displaying"
    echo "   Solution: Enable JavaScript or try different browser"
    echo "   Code: Check browser console for errors"
    echo ""
    
    echo "6. Can't find Python"
    echo "   Solution: Install Python 3.8 or later"
    echo "   Code: brew install python3  (macOS)"
    echo "         apt install python3  (Linux)"
    echo "         python-3.x-x86_64.exe  (Windows)"
}

generate_sample_data() {
    echo ""
    echo "${BLUE}Generating Enhanced Sample Data...${NC}"
    
    python3 << 'EOF'
import csv
from datetime import datetime, timedelta
import random
import os

# Create sample data
output_file = '../sample_noise_data_extended.csv'

with open(output_file, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['Timestamp', 'Location', 'Noise_Level_dB', 'Frequency_Hz', 'Duration_s'])
    
    start_time = datetime(2024, 1, 15, 6, 0, 0)
    
    for i in range(144):  # 24 hours, 10-minute intervals
        timestamp = start_time + timedelta(minutes=i*10)
        
        # Simulate hourly variation
        hour = timestamp.hour
        if 6 <= hour < 9:
            base_level = 70  # Morning traffic
        elif 9 <= hour < 12:
            base_level = 72  # Peak morning
        elif 12 <= hour < 17:
            base_level = 68  # Afternoon
        elif 17 <= hour < 20:
            base_level = 75  # Evening traffic
        else:
            base_level = 55  # Night time
        
        # Add variation
        noise_level = base_level + random.gauss(0, 2)
        noise_level = max(40, min(90, noise_level))  # Clamp between 40-90
        
        location = 'Residential Area'
        frequency = 1000
        duration = 600
        
        writer.writerow([
            timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            location,
            round(noise_level, 1),
            frequency,
            duration
        ])

print(f"✓ Sample data generated: {output_file}")
print(f"  - 144 data points (24 hours)")
print(f"  - Realistic hourly variations")
print(f"  - Ready for analysis")
EOF
}

# Main loop
while true; do
    show_menu
    
    case $choice in
        1) fresh_install ;;
        2) check_dependencies ;;
        3) verify_python ;;
        4) clear_and_reinstall ;;
        5) test_api ;;
        6) show_common_issues ;;
        7) generate_sample_data ;;
        8) echo "Goodbye!"; exit 0 ;;
        *) echo "Invalid option" ;;
    esac
    
    echo ""
    read -p "Press Enter to continue..."
done
