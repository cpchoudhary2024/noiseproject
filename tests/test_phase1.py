#!/usr/bin/env python3
"""
Integration test for Phase 1 Environmental Visualizations
Tests all new modules and visualizations with sample data
"""

import pandas as pd
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend'))

from analysis.environmental_viz import EnvironmentalVisualizationEngine
from analysis.environmental_metrics import EnvironmentalMetricsCalculator
from analysis.noise_analyzer import NoiseAnalyzer

def check_environmental_metrics():
    """Test EnvironmentalMetricsCalculator"""
    print("\n" + "="*70)
    print("TEST 1: Environmental Metrics Calculator")
    print("="*70)
    
    # Load sample data
    sample_file = 'sample_data.csv'
    if not os.path.exists(sample_file):
        print(f"❌ Sample file {sample_file} not found")
        return False
    
    df = pd.read_csv(sample_file)
    print(f"✓ Loaded {len(df)} records from {sample_file}")
    
    # Initialize calculator
    calc = EnvironmentalMetricsCalculator(df, ['Noise_Level_dB'], 'Timestamp')
    print("✓ EnvironmentalMetricsCalculator initialized")
    
    # Calculate metrics
    try:
        metrics = calc.calculate_all_metrics('Noise_Level_dB')
        
        # Verify structure
        assert 'exceedance' in metrics, "Missing exceedance metrics"
        assert 'percentiles' in metrics, "Missing percentile metrics"
        assert 'temporal' in metrics, "Missing temporal metrics"
        assert 'variability' in metrics, "Missing variability metrics"
        assert 'trend' in metrics, "Missing trend metrics"
        assert 'anomalies' in metrics, "Missing anomaly metrics"
        
        print("✓ All metric categories calculated")
        
        # Print sample results
        exc = metrics['exceedance']
        print(f"\n  Exceedance Analysis:")
        for std_name, data in list(exc.items())[:2]:
            pct = data.get('exceedance_frequency_pct', 0)
            print(f"    {std_name}: {pct:.1f}% exceeding limit")
        
        perc = metrics['percentiles']
        print(f"\n  Percentile Analysis:")
        print(f"    L50 (median): {perc.get('L50', 'N/A'):.1f} dB")
        print(f"    L95: {perc.get('L95', 'N/A'):.1f} dB")
        
        print(f"\n  Variability:")
        print(f"    Std Dev: {metrics['variability'].get('std_dev', 'N/A'):.2f} dB")
        print(f"    Skewness: {metrics['variability'].get('skewness', 'N/A'):.2f}")
        
        anom = metrics['anomalies']
        print(f"\n  Anomalies: {len(anom)} detected")
        if anom:
            print(f"    Top anomaly: {anom[0].get('severity', 0):.2f} dB severity")
        
        print("\n✅ PASS: Environmental Metrics Calculator")
        return True
        
    except Exception as e:
        print(f"\n❌ FAIL: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def check_environmental_visualizations():
    """Test EnvironmentalVisualizationEngine"""
    print("\n" + "="*70)
    print("TEST 2: Environmental Visualization Engine")
    print("="*70)
    
    # Load sample data
    sample_file = 'sample_data.csv'
    if not os.path.exists(sample_file):
        print(f"❌ Sample file {sample_file} not found")
        return False
    
    df = pd.read_csv(sample_file)
    print(f"✓ Loaded {len(df)} records from {sample_file}")
    
    # Initialize visualization engine
    viz = EnvironmentalVisualizationEngine(df, 'Noise_Level_dB')
    print("✓ EnvironmentalVisualizationEngine initialized")
    
    visualizations = [
        ('Exceedance Analysis', 'generate_exceedance_analysis', {'noise_col': 'Noise_Level_dB'}),
        ('Temporal Heatmap', 'generate_enhanced_temporal_heatmap', {'noise_col': 'Noise_Level_dB', 'resolution': 'hourly'}),
        ('Violin Distribution', 'generate_violin_distribution', {'noise_col': 'Noise_Level_dB'}),
        ('Cumulative Distribution', 'generate_cumulative_distribution', {'noise_col': 'Noise_Level_dB'}),
        ('Compliance Dashboard', 'generate_compliance_dashboard', {'noise_col': 'Noise_Level_dB', 'analysis': {'compliance': {'Noise_Level_dB': {'Residential_Day': {'status': 'NON_COMPLIANT'}}}}}),
        ('Anomaly Detection', 'generate_anomaly_detection', {'noise_col': 'Noise_Level_dB', 'threshold_std': 2.5}),
    ]
    
    passed = 0
    for viz_name, method_name, kwargs in visualizations:
        try:
            method = getattr(viz, method_name)
            fig = method(**kwargs)
            
            # Verify Plotly figure
            assert hasattr(fig, 'to_json'), f"{viz_name} didn't return Plotly figure"
            assert hasattr(fig, 'data'), f"{viz_name} figure missing data"
            assert hasattr(fig, 'layout'), f"{viz_name} figure missing layout"
            
            json_str = fig.to_json()
            assert len(json_str) > 100, f"{viz_name} JSON too short"
            
            print(f"✓ {viz_name}: Generated {len(json_str)} char JSON")
            passed += 1
            
        except Exception as e:
            print(f"❌ {viz_name}: {str(e)}")
            import traceback
            traceback.print_exc()
    
    if passed == len(visualizations):
        print(f"\n✅ PASS: All 6 visualizations generated successfully")
        return True
    else:
        print(f"\n❌ FAIL: {len(visualizations) - passed} visualizations failed")
        return False

def check_module_imports():
    """Test that all modules import correctly"""
    print("\n" + "="*70)
    print("TEST 0: Module Imports")
    print("="*70)
    
    modules = [
        ('analysis.environmental_viz', 'EnvironmentalVisualizationEngine'),
        ('analysis.environmental_metrics', 'EnvironmentalMetricsCalculator'),
        ('analysis.noise_analyzer', 'NoiseAnalyzer'),
    ]
    
    for module_name, class_name in modules:
        try:
            module = __import__(module_name, fromlist=[class_name])
            cls = getattr(module, class_name)
            print(f"✓ {module_name}.{class_name}")
        except ImportError as e:
            print(f"❌ Failed to import {module_name}: {str(e)}")
            return False
    
    print("✅ PASS: All modules import successfully")
    return True

def main():
    """Run all tests"""
    print("\n" + "█"*70)
    print("  PHASE 1 INTEGRATION TEST SUITE")
    print("  Environmental Visualizations & Metrics")
    print("█"*70)
    
    results = []
    
    # Test module imports
    results.append(("Module Imports", check_module_imports()))
    
    # Test metrics calculator
    results.append(("Environmental Metrics", check_environmental_metrics()))
    
    # Test visualizations
    results.append(("Environmental Visualizations", check_environmental_visualizations()))
    
    # Summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {test_name}")
    
    print(f"\nTotal: {passed}/{total} passed")
    
    if passed == total:
        print("\n🎉 ALL TESTS PASSED - Integration complete!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed - Review errors above")
        return 1

if __name__ == '__main__':
    sys.exit(main())


def test_environmental_metrics():
    assert check_environmental_metrics(), "environmental_metrics check failed"

def test_environmental_visualizations():
    assert check_environmental_visualizations(), "environmental_visualizations check failed"

def test_module_imports():
    assert check_module_imports(), "module_imports check failed"
