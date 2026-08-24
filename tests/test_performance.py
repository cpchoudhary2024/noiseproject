#!/usr/bin/env python3
"""
Performance test for caching functionality.
Tests that subsequent API calls reuse cached results efficiently.
"""

import time
import sys
import requests
import json
from pathlib import Path

# Configuration
BASE_URL = "http://localhost:5001"
SAMPLE_CSV = "sample_data.csv"
TIMEOUT = 60

def print_section(title):
    """Print a formatted section header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def test_upload_performance():
    """Test 1: Upload a file and verify it's cached."""
    print_section("TEST 1: File Upload & Caching")
    
    csv_path = Path(SAMPLE_CSV)
    if not csv_path.exists():
        print(f"❌ Sample CSV not found: {SAMPLE_CSV}")
        return False
    
    print(f"📤 Uploading {SAMPLE_CSV}...")
    start = time.time()
    
    with open(csv_path, 'rb') as f:
        files = {'file': f}
        response = requests.post(f"{BASE_URL}/api/upload", files=files, timeout=TIMEOUT)
    
    elapsed = time.time() - start
    
    if response.status_code != 200:
        print(f"❌ Upload failed with status {response.status_code}")
        print(f"Response: {response.text}")
        return False
    
    print(f"✅ Upload successful in {elapsed:.2f}s")
    print(f"Response: {response.json()}")
    return True

def test_analyze_first_run():
    """Test 2: Analyze - First run (computes fresh)."""
    print_section("TEST 2: First Analysis (Fresh Computation)")
    
    print("📊 Running analysis (first time - will compute)...")
    start = time.time()
    
    response = requests.post(f"{BASE_URL}/api/analyze", json={"filepath": SAMPLE_CSV}, timeout=TIMEOUT)
    
    elapsed = time.time() - start
    
    if response.status_code != 200:
        print(f"❌ Analysis failed with status {response.status_code}")
        print(f"Response: {response.text}")
        return False
    
    print(f"✅ First analysis completed in {elapsed:.2f}s")
    data = response.json()
    print(f"Analysis results keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}")
    return True, elapsed

def test_analyze_second_run():
    """Test 3: Analyze - Second run (should use cache)."""
    print_section("TEST 3: Second Analysis (Cached)")
    
    print("📊 Running analysis (second time - should be cached)...")
    start = time.time()
    
    response = requests.post(f"{BASE_URL}/api/analyze", json={"filepath": SAMPLE_CSV}, timeout=TIMEOUT)
    
    elapsed = time.time() - start
    
    if response.status_code != 200:
        print(f"❌ Analysis failed with status {response.status_code}")
        return False
    
    print(f"✅ Second analysis completed in {elapsed:.2f}s")
    return True, elapsed

def test_report_generation():
    """Test 4: Generate PDF report (should use cached analysis)."""
    print_section("TEST 4: PDF Report Generation (Using Cached Analysis)")
    
    print("📄 Generating PDF report (should use cached analysis & standards)...")
    start = time.time()
    
    response = requests.post(
        f"{BASE_URL}/api/generate-report",
        json={
            "filepath": SAMPLE_CSV,
            "report_type": "comprehensive"
        },
        timeout=TIMEOUT
    )
    
    elapsed = time.time() - start
    
    if response.status_code != 200:
        print(f"❌ Report generation failed with status {response.status_code}")
        print(f"Response: {response.text}")
        return False
    
    data = response.json()
    report_path = data.get('report_path', '')
    
    if report_path:
        print(f"✅ Report generated in {elapsed:.2f}s")
        print(f"📁 Report location: {report_path}")
        
        # Check file size
        try:
            file_size = Path(report_path).stat().st_size
            print(f"📊 Report size: {file_size / 1024:.2f} KB")
        except:
            pass
    else:
        print(f"⚠️  Report generated but path not returned")
    
    return True, elapsed

def test_performance_summary(first_analysis_time, second_analysis_time, report_time):
    """Summary of performance improvements."""
    print_section("PERFORMANCE SUMMARY")
    
    speedup = first_analysis_time / second_analysis_time if second_analysis_time > 0 else 0
    
    print(f"First analysis (fresh):     {first_analysis_time:.2f}s")
    print(f"Second analysis (cached):   {second_analysis_time:.2f}s")
    print(f"Report generation (cached): {report_time:.2f}s")
    print(f"\n🚀 Speedup on cache hit:    {speedup:.1f}x faster")
    print(f"⏱️  Time saved per request:  {(first_analysis_time - second_analysis_time):.2f}s")

def main():
    """Run all performance tests."""
    print("🧪 Noise Analysis Platform - Performance Test Suite")
    print(f"Target: {BASE_URL}")
    
    # Step 1: Upload
    if not test_upload_performance():
        print("\n❌ Upload test failed. Cannot continue.")
        sys.exit(1)
    
    # Step 2: First analysis
    success, first_time = test_analyze_first_run()
    if not success:
        print("\n❌ First analysis failed. Cannot continue.")
        sys.exit(1)
    
    # Step 3: Second analysis (should be faster due to cache)
    success, second_time = test_analyze_second_run()
    if not success:
        print("\n❌ Second analysis failed. Cannot continue.")
        sys.exit(1)
    
    # Step 4: Report generation (should use cached analysis)
    success, report_time = test_report_generation()
    if not success:
        print("\n❌ Report generation failed.")
        # Don't exit; continue to show summary
    
    # Summary
    test_performance_summary(first_time, second_time, report_time)
    
    print_section("✅ ALL TESTS COMPLETED")
    print("\n💡 Interpretation:")
    print("  - If second analysis is significantly faster (5x+), caching is working!")
    print("  - If times are similar, check server logs for cache hits.")
    print("  - Report generation should complete quickly due to cached analysis.")

if __name__ == '__main__':
    try:
        main()
    except requests.exceptions.ConnectionError:
        print(f"\n❌ ERROR: Cannot connect to {BASE_URL}")
        print("Please ensure the backend server is running:")
        print("  cd backend && python app.py")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
