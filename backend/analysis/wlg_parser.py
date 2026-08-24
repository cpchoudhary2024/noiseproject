# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""
WLG File Parser for Larson Davis Sound Level Meter Data

Parses .wlg binary files from Larson Davis sound level meters and converts
them to a pandas DataFrame for noise analysis.

WLG Format:
- Proprietary binary format from Larson Davis
- Contains header metadata and time-stamped sound level measurements
- Typically stores LAeq, LAmax, LAmin, etc.
"""

import struct
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import os
import re

logger = logging.getLogger(__name__)


class WLGParser:
    """Parser for Larson Davis .wlg binary sound meter data files."""
    
    # WLG file structure constants
    HEADER_SIZE = 512  # Typical WLG header size
    
    # Expected magic bytes at start of WLG file
    WLG_MAGIC = b'\x00\x00'  # Common WLG file signature
    
    def __init__(self, filepath):
        """Initialize parser with file path."""
        self.filepath = filepath
        self.data = []
        self.metadata = {}
    
    def parse(self):
        """Parse WLG file and return pandas DataFrame."""
        try:
            with open(self.filepath, 'rb') as f:
                file_data = f.read()
            
            # Try to parse as WLG format
            df = self._parse_wlg_data(file_data)
            
            if df is None or df.empty:
                raise ValueError("Could not parse WLG file - empty result")
            
            logger.info(f"Successfully parsed WLG file: {len(df)} measurements")
            return df
            
        except Exception as e:
            logger.error(f"Error parsing WLG file: {str(e)}")
            raise ValueError(f"Failed to parse WLG file: {str(e)}")
    
    def _parse_wlg_data(self, file_data):
        """
        Parse binary WLG data.
        
        WLG files have varying formats depending on device model and firmware.
        This parser attempts to extract measurement data from the binary structure.
        """
        if len(file_data) < 100:
            raise ValueError("File too small to be valid WLG")
        
        try:
            # Try to extract measurements from binary data
            measurements = []
            
            # Try byte-by-byte parsing FIRST (most effective for this format)
            raw_measurements = self._extract_measurements_byte_by_byte(file_data)
            measurements = self._decode_db_values(raw_measurements)
            
            if measurements and len(measurements) >= 100:  # Need significant data
                logger.info(f"Byte-by-byte strategy extracted {len(measurements)} measurements")
                if measurements:
                    start_dt = self._infer_start_datetime() or datetime.now()
                    df = pd.DataFrame({
                        'LAeq': measurements,
                        'Timestamp': pd.date_range(
                            start=start_dt,
                            periods=len(measurements),
                            freq='1s'
                        )
                    })
                    return df
            
            # Fallback: Try 16-bit values if byte strategy didn't work well
            raw_measurements = self._extract_measurements(file_data)
            measurements = self._decode_db_values(raw_measurements)
            
            if measurements:
                logger.info(f"16-bit strategy extracted {len(measurements)} measurements")
                start_dt = self._infer_start_datetime() or datetime.now()
                df = pd.DataFrame({
                    'LAeq': measurements,
                    'Timestamp': pd.date_range(
                        start=start_dt,
                        periods=len(measurements),
                        freq='1s'
                    )
                })
                return df
            
            return None
            
        except Exception as e:
            logger.error(f"Error in WLG data parsing: {str(e)}")
            return None

    def _infer_start_datetime(self):
        """Best-effort: infer start datetime from filename.

        Many WLG exports are named like: *_YYYY_MM_DD__HHhMMmSSs.wlg
        Example: CID_2501_2026_03_12__14h30m36s.wlg
        """
        try:
            name = os.path.basename(self.filepath)
            m = re.search(
                r"(\d{4})_(\d{2})_(\d{2})__([0-2]\d)h([0-5]\d)m([0-5]\d)s",
                name,
            )
            if not m:
                return None
            year, month, day, hour, minute, second = map(int, m.groups())
            return datetime(year, month, day, hour, minute, second)
        except Exception:
            return None

    @staticmethod
    def _energetic_mean_db(values: np.ndarray) -> float:
        v = np.asarray(values, dtype=np.float64)
        v = v[np.isfinite(v)]
        if v.size == 0:
            return float('nan')
        mean_energy = np.mean(np.power(10.0, v / 10.0))
        if mean_energy <= 0 or not np.isfinite(mean_energy):
            return float('nan')
        return float(10.0 * np.log10(mean_energy))

    def _decode_db_values(self, raw_values):
        """Decode raw extracted values into plausible dB.

        Some WLG variants store LAeq as an encoded byte rather than a physical dB.
        If we treat those bytes as dB directly, a small fraction of high codes can
        dominate energetic averages and produce impossible LAeq/Lden values.

        This uses a lightweight heuristic to choose a transform that yields
        physically plausible levels.
        """
        if not raw_values:
            return []

        raw = np.asarray(raw_values, dtype=np.float64)
        raw = raw[np.isfinite(raw)]
        if raw.size == 0:
            return []

        # Candidate transforms (a * raw + b)
        candidates = [
            ("identity", 1.0, 0.0),
            ("half", 0.5, 0.0),
            ("half_plus20", 0.5, 20.0),
            ("half_plus30", 0.5, 30.0),
        ]

        def score(transformed: np.ndarray) -> float:
            t = transformed[np.isfinite(transformed)]
            if t.size == 0:
                return float("inf")
            emin = self._energetic_mean_db(t)
            tmin = float(np.min(t))
            tmax = float(np.max(t))

            if not np.isfinite(emin):
                return float("inf")

            # Prefer plausible ranges for environmental/occupational noise.
            # Keep this wide to avoid overfitting.
            penalty = 0.0
            if emin < 30.0:
                penalty += (30.0 - emin) * 4.0
            if emin > 105.0:
                penalty += (emin - 105.0) * 25.0

            # Prefer measurements that don't dip below ~20 dB.
            if tmin < 20.0:
                penalty += (20.0 - tmin) * 2.0

            # Prefer not exceeding ~130 dB.
            if tmax > 130.0:
                penalty += (tmax - 130.0) * 3.0

            return penalty

        best_name, best_a, best_b = candidates[0]
        best_vals = best_a * raw + best_b
        best_score = score(best_vals)

        for name, a, b in candidates[1:]:
            vals = a * raw + b
            s = score(vals)
            if s < best_score:
                best_name, best_a, best_b = name, a, b
                best_vals = vals
                best_score = s

        # Only accept non-identity transforms when identity looks implausible.
        identity_vals = raw
        identity_em = self._energetic_mean_db(identity_vals)
        if np.isfinite(identity_em) and identity_em <= 105.0:
            # Identity already yields plausible energetic averages; keep it.
            best_name, best_a, best_b = "identity", 1.0, 0.0
            best_vals = identity_vals
        else:
            logger.info(
                "Applied WLG dB decode transform=%s (a=%s, b=%s), energetic_mean=%.2f dB",
                best_name,
                best_a,
                best_b,
                self._energetic_mean_db(best_vals),
            )

        # Return as Python floats for downstream JSON serialization safety
        return [float(v) for v in best_vals.tolist()]
    
    def _extract_measurements_byte_by_byte(self, data):
        """
        Extract dB measurements byte-by-byte from WLG data.
        
        This is the PRIMARY extraction method - WLG files store individual bytes
        representing sound levels in the 20-150 dB range.
        """
        measurements = []
        
        try:
            # Skip potential header (first 20-50 bytes)
            start_offset = 20
            
            # Extract every byte as a potential measurement
            for i in range(start_offset, len(data)):
                byte_val = data[i]
                
                # Valid dB range: 20-150 (typical sound levels)
                if 20 <= byte_val <= 150:
                    measurements.append(float(byte_val))
            
            # Validate we have a reasonable number of measurements
            if len(measurements) >= 100:
                logger.info(f"Byte-by-byte extraction found {len(measurements)} valid measurements")
                return measurements
            else:
                logger.debug(f"Byte-by-byte extraction only found {len(measurements)} measurements (below threshold)")
                return []
                
        except Exception as e:
            logger.error(f"Byte-by-byte extraction failed: {str(e)}")
            return []
    
    def _extract_measurements(self, data):
        """
        Extract dB measurements from WLG binary data using 16-bit value interpretation.
        
        Fallback method when byte-by-byte doesn't yield enough data.
        """
        measurements = []
        
        # Strategy 1: Look for 16-bit signed values that represent dB measurements
        try:
            offset = 0
            while offset < len(data) - 1:
                # Try to read as little-endian signed 16-bit integer
                try:
                    value_signed = struct.unpack_from('<h', data, offset)[0]
                    value_unsigned = struct.unpack_from('<H', data, offset)[0]
                    
                    db_value = None
                    
                    # Try different interpretations
                    
                    # Format 1: Unsigned value represents dB*10 (30-1300 range = 3.0-130.0 dB)
                    if 300 <= value_unsigned <= 1300:
                        db_value = value_unsigned / 10.0
                    
                    # Format 2: Signed byte pair interpretation
                    # Some devices pack dB as: (dB + 100) * 100 then split into bytes
                    elif -32000 <= value_signed <= 32000:
                        # Try interpreting as dB+100 scaled
                        potential_db = (value_signed / 100.0) - 100
                        if 20 <= potential_db <= 150:
                            db_value = potential_db
                    
                    # Format 3: Raw signed value in dB range
                    if db_value is None:
                        if 30 <= value_signed <= 130:
                            db_value = float(value_signed)
                        elif 30 <= value_unsigned <= 130:
                            db_value = float(value_unsigned)
                    
                    if db_value is not None and 20 <= db_value <= 150:
                        measurements.append(db_value)
                    
                except:
                    pass
                
                offset += 2
            
            # If we found measurements, validate we have a reasonable amount
            if len(measurements) >= 10:
                logger.info(f"16-bit strategy extracted {len(measurements)} measurements")
                return measurements
                
        except Exception as e:
            logger.debug(f"16-bit strategy failed: {str(e)}")
        
        return []
    
    @staticmethod
    def is_wlg_file(filepath):
        """Check if file appears to be a WLG file."""
        try:
            with open(filepath, 'rb') as f:
                header = f.read(512)
            
            # Check for common WLG signatures or patterns
            # WLG files often start with specific byte patterns
            if len(header) >= 2:
                # Check various signatures that indicate WLG format
                signatures = [
                    b'\x00\x00',  # Common start
                    b'WLG',       # Text signature (rare)
                ]
                
                for sig in signatures:
                    if header.startswith(sig):
                        return True
                
                # Also check file size and structure patterns
                # WLG files are typically binary with repetitive patterns
                if len(header) >= 100:
                    # Look for patterns of valid dB values in the file
                    valid_bytes = sum(1 for b in header if 20 <= b <= 150)
                    if valid_bytes > 20:  # At least 20% should be in dB range
                        return True
            
            return False
            
        except Exception:
            return False


def parse_wlg_file(filepath):
    """
    Convenience function to parse a WLG file.
    
    Args:
        filepath: Path to .wlg file
        
    Returns:
        pandas.DataFrame with noise measurements
    """
    parser = WLGParser(filepath)
    return parser.parse()
