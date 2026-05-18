#!/bin/bash
# Performance test script for caching functionality
# Tests: upload → analyze (1st run) → analyze (2nd run) → report generation

BASE_URL="http://localhost:5001"
SAMPLE_CSV="sample_data.csv"

echo "=================================="
echo "Performance Test: Caching"
echo "=================================="
echo ""

# Check if server is reachable
echo "🔍 Checking server connectivity..."
if ! curl -s "$BASE_URL/api/analyze" > /dev/null 2>&1; then
    echo "❌ Cannot reach server at $BASE_URL"
    echo "Please start the backend server:"
    echo "  cd backend && python app.py"
    exit 1
fi
echo "✅ Server is running"
echo ""

# Test 1: Upload file
echo "📤 TEST 1: Uploading file..."
UPLOAD_START=$(date +%s.%N)
UPLOAD_RESPONSE=$(curl -s -X POST "$BASE_URL/api/upload" -F "file=@$SAMPLE_CSV")
UPLOAD_END=$(date +%s.%N)
UPLOAD_TIME=$(echo "$UPLOAD_END - $UPLOAD_START" | bc)

echo "Response: $UPLOAD_RESPONSE"
echo "⏱️  Time: ${UPLOAD_TIME}s"
echo ""

# Test 2: First analysis (should compute)
echo "📊 TEST 2: First analysis (fresh computation)..."
ANALYZE1_START=$(date +%s.%N)
ANALYZE1_RESPONSE=$(curl -s -X POST "$BASE_URL/api/analyze" \
  -H "Content-Type: application/json" \
  -d "{\"filepath\": \"$SAMPLE_CSV\"}")
ANALYZE1_END=$(date +%s.%N)
ANALYZE1_TIME=$(echo "$ANALYZE1_END - $ANALYZE1_START" | bc)

# Show just keys
echo "Response keys: $(echo $ANALYZE1_RESPONSE | python3 -c "import sys, json; d=json.load(sys.stdin); print(list(d.keys()) if isinstance(d, dict) else 'error')" 2>/dev/null || echo 'unknown')"
echo "⏱️  Time: ${ANALYZE1_TIME}s"
echo ""

# Test 3: Second analysis (should use cache)
echo "📊 TEST 3: Second analysis (cached)..."
ANALYZE2_START=$(date +%s.%N)
ANALYZE2_RESPONSE=$(curl -s -X POST "$BASE_URL/api/analyze" \
  -H "Content-Type: application/json" \
  -d "{\"filepath\": \"$SAMPLE_CSV\"}")
ANALYZE2_END=$(date +%s.%N)
ANALYZE2_TIME=$(echo "$ANALYZE2_END - $ANALYZE2_START" | bc)

echo "Response keys: $(echo $ANALYZE2_RESPONSE | python3 -c "import sys, json; d=json.load(sys.stdin); print(list(d.keys()) if isinstance(d, dict) else 'error')" 2>/dev/null || echo 'unknown')"
echo "⏱️  Time: ${ANALYZE2_TIME}s"
echo ""

# Test 4: Report generation (should use cached analysis)
echo "📄 TEST 4: Report generation..."
REPORT_START=$(date +%s.%N)
REPORT_RESPONSE=$(curl -s -X POST "$BASE_URL/api/generate-report" \
  -H "Content-Type: application/json" \
  -d "{\"filepath\": \"$SAMPLE_CSV\", \"report_type\": \"comprehensive\"}")
REPORT_END=$(date +%s.%N)
REPORT_TIME=$(echo "$REPORT_END - $REPORT_START" | bc)

echo "Response: $(echo $REPORT_RESPONSE | python3 -c "import sys, json; d=json.load(sys.stdin); print('report_path:' + d.get('report_path', 'N/A'))" 2>/dev/null || echo $REPORT_RESPONSE)"
echo "⏱️  Time: ${REPORT_TIME}s"
echo ""

# Calculate speedup
echo "=================================="
echo "PERFORMANCE SUMMARY"
echo "=================================="
SPEEDUP=$(echo "scale=2; $ANALYZE1_TIME / $ANALYZE2_TIME" | bc 2>/dev/null || echo "N/A")
TIME_SAVED=$(echo "scale=2; $ANALYZE1_TIME - $ANALYZE2_TIME" | bc 2>/dev/null || echo "N/A")

echo "First analysis (fresh):     ${ANALYZE1_TIME}s"
echo "Second analysis (cached):   ${ANALYZE2_TIME}s"
echo "Report generation (cached): ${REPORT_TIME}s"
echo ""
echo "🚀 Cache speedup: ${SPEEDUP}x faster"
echo "⏱️  Time saved: ${TIME_SAVED}s"
echo ""
echo "✅ If second analysis is significantly faster, caching is working!"
