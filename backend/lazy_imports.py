"""Lazy-loading wrapper for analysis modules to reduce startup memory."""
import logging

logger = logging.getLogger(__name__)

# Lazy-loaded module cache
_MODULES = {}


def _lazy_import(module_path: str, class_name: str = None):
    """Lazily import a module or class only when needed."""
    cache_key = f"{module_path}:{class_name or 'module'}"
    
    if cache_key in _MODULES:
        return _MODULES[cache_key]
    
    logger.info(f"[LAZY-LOAD] Importing {cache_key}")
    
    try:
        if class_name:
            # Import specific class
            module = __import__(module_path, fromlist=[class_name])
            obj = getattr(module, class_name)
            _MODULES[cache_key] = obj
            return obj
        else:
            # Import whole module
            module = __import__(module_path, fromlist=[''])
            _MODULES[cache_key] = module
            return module
    except Exception as e:
        logger.error(f"[LAZY-LOAD] Failed to import {cache_key}: {e}")
        raise


# Lazy getters for each analysis component
def get_noise_analyzer():
    """Lazy import NoiseAnalyzer."""
    from analysis.noise_analyzer import NoiseAnalyzer
    return NoiseAnalyzer


def get_report_generator():
    """Lazy import ReportGenerator."""
    from analysis.report_generator import ReportGenerator
    return ReportGenerator


def get_report_generator_v2():
    """Lazy import ReportGeneratorV2."""
    from analysis.report_generator_v2 import ReportGeneratorV2
    return ReportGeneratorV2


def get_word_report_generator():
    """Lazy import WordReportGenerator."""
    from analysis.docx_generator import WordReportGenerator
    return WordReportGenerator


def get_standards_analyzer():
    """Lazy import StandardsAnalyzer."""
    from analysis.iso_epa_standards import StandardsAnalyzer
    return StandardsAnalyzer


def get_data_summarizer():
    """Lazy import DataSummarizer."""
    from analysis.data_summarizer import DataSummarizer
    return DataSummarizer


def get_chart_generator():
    """Lazy import AdvancedChartGenerator."""
    from analysis.chart_generator import AdvancedChartGenerator
    return AdvancedChartGenerator


def get_environmental_viz():
    """Lazy import EnvironmentalVisualizationEngine."""
    from analysis.environmental_viz import EnvironmentalVisualizationEngine
    return EnvironmentalVisualizationEngine


def get_environmental_metrics():
    """Lazy import EnvironmentalMetricsCalculator."""
    from analysis.environmental_metrics import EnvironmentalMetricsCalculator
    return EnvironmentalMetricsCalculator


def get_wlg_parser():
    """Lazy import WLGParser."""
    from analysis.wlg_parser import WLGParser
    return WLGParser


def get_wlg_parse_function():
    """Lazy import parse_wlg_file function."""
    from analysis.wlg_parser import parse_wlg_file
    return parse_wlg_file


def get_gap_detector():
    """Lazy import gap detection functions."""
    from analysis.gap_detector import detect_gaps, gap_report_to_dict, merge_dataframes
    return detect_gaps, gap_report_to_dict, merge_dataframes


def get_compliance_matrix():
    """Lazy import evaluate_compliance."""
    from analysis.compliance_matrix import evaluate_compliance
    return evaluate_compliance


def get_acoustics():
    """Lazy import acoustics functions."""
    from analysis.acoustics import energetic_mean_db, compute_ldn_lden
    return energetic_mean_db, compute_ldn_lden
