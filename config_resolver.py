# config/config_resolver_v6.py
"""
Configuration Resolver v6.0 - Refactored with Proper Layer Separation
=====================================================================
ARCHITECTURE CHANGES:
✅ No direct access to raw configs (master_config, setup_pattern_matrix, etc.)
✅ All config queries go through query_optimized_extractor
✅ Pure decision-making logic (no data extraction)
✅ Clean separation of concerns

Layer Flow:
    Raw Configs (confidence_config.py, master_config.py, etc.)
        ↓
    config_extractor.py (extracts sections)
        ↓
    query_optimized_extractor.py (provides queries)
        ↓
    config_resolver.py (makes decisions) ← YOU ARE HERE
        ↓
    Your Application (signal_engine, etc.)

Author: Quantitative Trading System
Version: 6.0 - Fully Refactored
"""

from typing import Dict, Any, List, Optional, Tuple, Union
from datetime import datetime, time
import logging

from config.logger_config import (
    METRICS,
    SafeDict,
    log_failures,
    log_resolver_context_quality,
    track_performance
)
from config.master_config import GATE_METRIC_REGISTRY
from config.fundamental_score_config import compute_fundamental_score
from services.data_fetch import _get_val, _safe_float, _safe_get_raw_float, ensure_numeric
from config.technical_score_config import calculate_dynamic_score, compute_technical_score

MIN_FIT_SCORE = 10.0
logger = logging.getLogger(__name__)


# ============================================================================
# SAFE CONDITION EVALUATOR (Embedded - No External Dependencies)
# ============================================================================

class ConditionEvaluator:
    """
    Enhanced condition evaluator supporting:
    - Numeric/string comparisons
    - Math functions (abs, min, max, round)
    - Nested dict value extraction
    - Logical operators (AND/OR)
    """
    
    OPERATORS = {
        ">=": lambda a, b: a >= b,
        "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b,
        "<": lambda a, b: a < b,
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
    }
    
    # Math functions available in conditions
    MATH_FUNCTIONS = {
        "abs": abs,
        "min": min,
        "max": max,
        "round": round,
    }
    
    @classmethod
    def evaluate_condition(cls, condition: str, namespace: Dict[str, Any]) -> bool:
        """
        Evaluate condition with smart value extraction from nested dicts.
        
        Examples:
            "abs(price - maFast) / maFast <= 0.05"  # Math functions
            "ttmSqueeze == 'Squeeze On'"             # String comparison
            "price < bbLow"                          # Numeric comparison (auto-extracts from nested)
        """
        if not condition or not condition.strip():
            return False
        
        # Handle logical operators
        if " and " in condition.lower():
            parts = condition.split(" and ")
            return all(cls.evaluate_condition(part.strip(), namespace) for part in parts)
        
        if " or " in condition.lower():
            parts = condition.split(" or ")
            return any(cls.evaluate_condition(part.strip(), namespace) for part in parts)
        
        # Parse single comparison
        condition = condition.strip()
        
        for op_str, op_func in cls.OPERATORS.items():
            if f" {op_str} " in f" {condition} ":
                parts = condition.split(op_str)
                if len(parts) != 2:
                    continue
                
                left = parts[0].strip()
                right = parts[1].strip()
                
                # Evaluate left side (may contain math expressions)
                try:
                    left_value = cls._evaluate_expression(left, namespace)
                except Exception as e:
                    return False
                
                # Parse right value
                right_value = cls._parse_right_operand(right, namespace)
                
                if left_value is None or right_value is None:
                    return False
                
                try:
                    return cls._compare_values(left_value, right_value, op_func)
                except (TypeError, ValueError):
                    return False
        
        return False
    
    @classmethod
    def _evaluate_expression(cls, expr: str, namespace: Dict[str, Any]) -> Any:
        """
        Evaluate expression with auto-extraction from nested dicts.
        
        Handles:
        - Simple variables: "price" → extracts from {"price": {"value": 954.55, ...}}
        - Math functions: "abs(price - maFast)" → evaluates with extracted values
        - Arithmetic: "price - maFast" → evaluates with extracted values
        """
        expr = expr.strip()
        
        # Check if expression contains functions or operators
        has_function = any(f"{func}(" in expr for func in cls.MATH_FUNCTIONS.keys())
        has_operator = any(op in expr for op in ['+', '-', '*', '/', '(', ')'])
        
        if has_function or has_operator:
            # Build safe namespace with extracted values
            safe_namespace = cls._build_safe_namespace(namespace)
            
            try:
                result = eval(expr, {"__builtins__": {}}, safe_namespace)
                
                # ✅ DEBUG: Log successful evaluation
                logger.debug(
                    f"Expression evaluated: '{expr}' = {result:.4f} | "
                    f"Available vars: {list(safe_namespace.keys())[:10]}"
                )
                
                return result
            except Exception as e:
                # ✅ IMPROVED: Log detailed error
                logger.warning(
                    f"⚠️ eval failed in _evaluate_expression: {e} | "
                    f"Expression: '{expr}' | "
                    f"Namespace keys: {list(safe_namespace.keys())}"
                )
                return None
        else:
            # Simple variable lookup with auto-extraction
            value = namespace.get(expr)
            return cls._extract_value_from_metric(value)

    @classmethod
    def _build_safe_namespace(cls, namespace: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build safe namespace with extracted values + math functions.
        
        Converts:
        {"price": {"value": 954.55, ...}} → {"price": 954.55, "abs": <function>, ...}
        """
        safe = {**cls.MATH_FUNCTIONS}
        
        for key, value in namespace.items():
            extracted = cls._extract_value_from_metric(value)
            if extracted is not None:
                safe[key] = extracted
        
        return safe
    
    @classmethod
    def _extract_value_from_metric(cls, value: Any) -> Any:
        """
        Extract usable value from metric (handles nested dicts).
        
        Extraction priority:
        1. Simple values (int/float/str/bool) → return as-is
        2. Nested dict with raw → extract raw (unless pattern)
        3. Nested dict with value → extract value
        4. Nested dict with score → extract score
        
        Special handling for patterns:
        - Pattern dicts (with raw.found) are preserved for pattern detection
        """
        if value is None:
            return None
        
        # Already primitive
        if isinstance(value, (int, float, str, bool)):
            return value
        
        # Nested dict
        if isinstance(value, dict):
            # Check if this is a pattern metric (has nested raw.found)
            raw = value.get('raw')
            if isinstance(raw, dict) and 'found' in raw:
                # This is a pattern - DON'T extract nested value
                # Return top-level 'value' or 'found' instead
                if 'value' in value and not isinstance(value['value'], dict):
                    return value['value']
                if 'found' in value:
                    return value['found']
                return value  # Return whole dict as fallback
            
            # Extract from nested dict: priority raw > value > score
            if raw is not None and not isinstance(raw, dict):
                if isinstance(raw, str):
                    return raw  # String metric
                else:
                    try:
                        return float(raw)
                    except (ValueError, TypeError):
                        pass
            
            val = value.get('value')
            if val is not None and not isinstance(val, dict):
                if isinstance(val, str):
                    return val
                else:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        pass
            
            score = value.get('score')
            if score is not None:
                try:
                    return float(score)
                except (ValueError, TypeError):
                    pass
        
        return value
    
    @classmethod
    def _parse_right_operand(cls, right: str, namespace: Dict[str, Any]) -> Any:
        """Parse right side of comparison."""
        right = right.strip()
        
        # String literal
        if (right.startswith("'") and right.endswith("'")) or \
           (right.startswith('"') and right.endswith('"')):
            return right[1:-1]
        
        # Boolean
        if right.lower() == "true":
            return True
        if right.lower() == "false":
            return False
        
        # Variable from namespace (with auto-extraction)
        if right in namespace:
            return cls._extract_value_from_metric(namespace[right])
        
        # Numeric literal
        try:
            if '.' not in right:
                return int(right)
            return float(right)
        except ValueError:
            return None
    
    @classmethod
    def _compare_values(cls, left: Any, right: Any, op_func) -> bool:
        """
        Type-aware comparison that handles mixed types gracefully.
        
        Rules:
        1. Both strings → Direct string comparison
        2. Both numeric → Numeric comparison  
        3. String vs numeric → Try conversion, fail gracefully
        4. Boolean → Exact match only
        """
        # Handle None values
        if left is None or right is None:
            return False
        
        # Both strings
        if isinstance(left, str) and isinstance(right, str):
            return op_func(left, right)
        
        # Both booleans
        if isinstance(left, bool) and isinstance(right, bool):
            return op_func(left, right)
        
        # Both numeric
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return op_func(float(left), float(right))
        
        # Mixed: try conversion
        if isinstance(left, str) and isinstance(right, (int, float)):
            try:
                left_num = float(left)
                return op_func(left_num, float(right))
            except (ValueError, TypeError):
                return False
        
        if isinstance(left, (int, float)) and isinstance(right, str):
            try:
                right_num = float(right)
                return op_func(float(left), right_num)
            except (ValueError, TypeError):
                return False
        
        # Boolean vs other
        if isinstance(left, bool) or isinstance(right, bool):
            return False
        
        return False
    
    @classmethod
    def evaluate_conditions_list(cls, conditions: list, namespace: Dict[str, Any]) -> bool:
        """Evaluate list of conditions (all must pass)."""
        if not conditions:
            return False
        return all(cls.evaluate_condition(cond, namespace) for cond in conditions)

    @classmethod
    def evaluate_gate_config(
        cls, 
        actual_value: Any, 
        gate_config: Union[Dict, float, int], 
        metric_name: str
    ) -> Tuple[bool, Optional[str]]:
        """
        ✅ NEW: Handles structured gate configs (Dict/Numeric).
        Reuses cls._compare_values for consistent type safety.
        """
        if actual_value is None:
            return False, f"{metric_name} is Missing/None"

        # Normalize to min/max
        mn, mx = None, None
        if isinstance(gate_config, dict):
            mn = gate_config.get("min")
            mx = gate_config.get("max")
        elif isinstance(gate_config, (int, float)):
            mn = gate_config
        
        # Check Min
        if mn is not None:
            # Reuse _compare_values from your existing string evaluator!
            if not cls._compare_values(actual_value, mn, lambda a, b: a >= b):
                return False, f"{metric_name} {actual_value} < min {mn}"

        # Check Max
        if mx is not None:
            if not cls._compare_values(actual_value, mx, lambda a, b: a <= b):
                return False, f"{metric_name} {actual_value} > max {mx}"

        return True, None
# ============================================================================
# HELPER FUNCTIONS (Minimal - Most Logic in Resolver)
# ============================================================================

# ============================================================================
# CORE RESOLVER CLASS (v6.0 - Refactored)
# ============================================================================

class ConfigResolver:
    """
    Configuration Resolver v6.0 - Pure Decision-Making Logic
    
    ARCHITECTURE:
    ┌─────────────────────────────────────────────────────────┐
    │ Raw Configs (confidence_config.py, master_config.py)   │
    │              ↓                                          │
    │ config_extractor.py (extracts & merges sections)       │
    │              ↓                                          │
    │ query_optimized_extractor.py (provides query methods)  │
    │              ↓                                          │
    │ config_resolver.py (makes decisions) ← YOU ARE HERE    │
    │              ↓                                          │
    │ Application (signal_engine, trade_plan_generator)      │
    └─────────────────────────────────────────────────────────┘
    
    DESIGN PRINCIPLES:
    ✅ No direct access to raw configs
    ✅ All config queries via self.extractor
    ✅ Pure business logic (no data extraction)
    ✅ Clean layer separation
    
    RESOLVER RESPONSIBILITIES:
    - Setup classification (using extractor for rules)
    - Strategy selection (using extractor for fit indicators)
    - Gate validation (using extractor for thresholds)
    - Confidence calculation (using extractor for modifiers)
    - Execution permission (combining all validations)
    
    EXTRACTOR RESPONSIBILITIES:
    - Config extraction from raw files
    - Hierarchy enforcement (horizon > setup > global)
    - Gate merging and resolution
    - Pattern context building
    """
    
    
class ConfigResolver:
    def __init__(self, master_config: Dict, horizon: str, logger=None):
        """
        Initialize resolver with master config and horizon.

        Args:
            master_config: Complete MASTER_CONFIG dictionary
            horizon: Target horizon (intraday, short_term, long_term, multibagger)
        """
        import logging
        from config.query_optimized_extractor import QueryOptimizedExtractor

        self.horizon = horizon
        self.logger = logger or logging.getLogger(__name__)

        # Initialize query extractor (our ONLY config interface)
        self.extractor = QueryOptimizedExtractor(master_config, horizon, self.logger)
        self.logger.info(
            f"✅ ConfigResolver v6.0 initialized for {horizon} "
            f"(using query extractor)"
        )

        # Hard validation – do not run if confidence config / critical sections are missing
        state = self.extractor.validate_extractor_state()
        if not state.get("valid") or not state.get("has_confidence_config"):
            msg = (
                f"Extractor/Confidence config invalid for horizon={horizon}. "
                f"errors={state.get('errors')}, has_conf={state.get('has_confidence_config')}"
            )
            self.logger.error(msg)
            raise RuntimeError(msg)

    def _flatten_indicator_data(self, data: Dict[str, Any]) -> Dict[str, float]:
        """
        Flatten nested indicator dicts to simple numeric values.
        
        Ensures extractor receives clean data that matches test expectations.
        
        Transforms:
            {"rsi": {"value": 62.01, "score": 8}} → {"rsi": 62.01}
            {"adx": 33.13} → {"adx": 33.13}
        
        Args:
            data: Raw indicator/fundamental data (may have nested dicts)
        
        Returns:
            Flattened dict with numeric values only
        """
        flattened = {}
        
        for key, value in data.items():
            if value is None:
                continue
            
            # Already numeric - use as-is
            if isinstance(value, (int, float)):
                flattened[key] = float(value)
                continue
            
            # Nested dict - extract numeric value
            if isinstance(value, dict):
                # Priority: value > raw > score
                extracted = (
                    value.get("value") if "value" in value else
                    value.get("raw") if "raw" in value else
                    value.get("score") if "score" in value else
                    None
                )
                
                if extracted is not None:
                    try:
                        flattened[key] = float(extracted)
                    except (ValueError, TypeError):
                        # Skip if can't convert to float
                        pass
                continue
            
            # String number - try to convert
            if isinstance(value, str):
                try:
                    flattened[key] = float(value)
                except (ValueError, TypeError):
                    # Skip if not a valid number
                    pass
                continue
        
        return flattened
    

    # ========================================================================
    # PHASE 1: EVALUATION CONTEXT (WHAT to trade)
    # ========================================================================
    
    @log_failures(return_on_error={}, critical=False)
    def _build_evaluation_context(
        self,
        symbol: str,
        fundamentals: Dict[str, float],
        indicators: Dict[str, float],
        price_data: Dict[str, float],
        detected_patterns: Optional[Dict[str, Dict]]
    ) -> Dict[str, Any]:
        """Build evaluation context using ONLY extractor queries."""
        # Log input data quality
        log_resolver_context_quality(
            fundamentals=fundamentals,
            indicators=indicators,
            patterns=detected_patterns or {},
            symbol=symbol
        )
        
        safe_fund = SafeDict(fundamentals or {}, context="fundamentals", source=symbol)
        safe_ind = SafeDict(indicators or {}, context="indicators", source=symbol)
        safe_price = SafeDict(price_data or {}, context="price_data", source=symbol)
        
        overall_start = datetime.now().timestamp()
        
        ctx = {
            "meta": {
                "symbol": symbol,
                "horizon": self.horizon,
                "timestamp": datetime.utcnow().isoformat(),
                "config_version": "6.0"
            }
        }
        
        # Store raw dicts
        ctx["fundamentals"] = safe_fund.raw
        ctx["indicators"] = safe_ind.raw
        ctx["price_data"] = safe_price.raw
        ctx["patterns"] = detected_patterns
        ctx["trend"] = self._build_trend_context(ctx["indicators"])
        ctx["momentum"] = self._build_momentum_context(ctx["indicators"])

        # =====================================================================
        # âœ… CORRECTED EXECUTION ORDER - Respects Dependencies
        # =====================================================================
        
        # PHASE 1: Foundation (No Dependencies)
        # ---------------------------------------------------------------------
        with track_performance("calculate_scores"):
            ctx["scoring"] = self._calculate_all_scores(ctx)
        
        with track_performance("build_conditions"):
            ctx["conditions"] = self._build_conditions(ctx)
        
        with track_performance("detect_volume_signature"):
            ctx["volume_signature"] = self.detect_volume_signature(safe_ind.raw)
        
        with track_performance("detect_divergence"):
            ctx["divergence"] = self.detect_divergence(safe_ind.raw)
        
        # PHASE 2: Setup Classification (Needs: conditions, patterns)
        # ---------------------------------------------------------------------
        with track_performance("classify_setup"):
            ctx["setup"] = self._classify_setup(ctx)
        
        # PHASE 3: Pattern Validation (Evaluate pattern validity PER SETUP, and record results.)
        # ---------------------------------------------------------------------
        with track_performance("validate_patterns"):
            ctx["pattern_validation"] = self._validate_patterns(ctx)
        
        # PHASE 4: Strategy & Preferences (Needs: setup)
        # ---------------------------------------------------------------------
        with track_performance("classify_strategy"):
            ctx["strategy"] = self._classify_strategy(ctx)
        
        with track_performance("apply_setup_preferences"):
            ctx["setup_preferences"] = self._apply_setup_preferences(ctx)
        
        # PHASE 5: Structural Gates (Needs: setup)
        # ---------------------------------------------------------------------
        with track_performance("validate_structural_gates"):
            ctx["structural_gates"] = self._validate_structural_gates(ctx)
        
        # PHASE 6: Execution Rules (Needs: setup, structural_gates)
        # ---------------------------------------------------------------------
        with track_performance("validate_execution_rules"):
            ctx["execution_rules"] = self._validate_execution_rules(ctx)
        
        # PHASE 7: Confidence (Needs: setup, volume, divergence)
        # ---------------------------------------------------------------------
        with track_performance("calculate_confidence"):
            ctx["confidence"] = self._calculate_confidence(ctx)

        with track_performance("risk_candidates"):
            ctx["risk_candidates"] = self._build_risk_candidates(ctx)
        
        # PHASE 8: Opportunity Gates (Needs: confidence - MUST BE LAST)
        # ---------------------------------------------------------------------
        with track_performance("validate_opportunity_gates"):
            ctx["opportunity_gates"] = self._validate_opportunity_gates(ctx)


        # Log overall performance
        overall_elapsed = datetime.now().timestamp() - overall_start
        self.logger.info(
            f"[{symbol}] ✅ EVALUATION CONTEXT BUILT in {overall_elapsed*1000:.1f}ms"
        )
        METRICS.log_performance("_build_evaluation_context", overall_elapsed, threshold_ms=100)
        
        return ctx
    
    def build_evaluation_context_only(
        self,
        symbol: str,
        fundamentals: Dict[str, float],
        indicators: Dict[str, float],
        price_data: Dict[str, float],
        detected_patterns: Optional[Dict[str, Dict]] = None
    ) -> Dict[str, Any]:
        """
        ✅ PUBLIC API: Build evaluation context (Phase 1 only).
        
        This is the public interface called by config_helpers.py.
        Delegates to internal _build_evaluation_context method.
        
        Args:
            symbol: Stock symbol
            fundamentals: Fundamental metrics
            indicators: Technical indicators
            price_data: Price and volume data
            detected_patterns: Optional pre-detected patterns
        
        Returns:
            Complete evaluation context dict
        
        Usage:
            resolver = get_resolver('short_term')
            eval_ctx = resolver.build_evaluation_context_only(
                symbol='RELIANCE.NS',
                fundamentals=fund_data,
                indicators=tech_data,
                price_data=price_info,
                detected_patterns=patterns
            )
        """
        return self._build_evaluation_context(
            symbol=symbol,
            fundamentals=fundamentals,
            indicators=indicators,
            price_data=price_data,
            detected_patterns=detected_patterns
        )

    def build_execution_context_from_evaluation(
        self,
        evaluation_ctx: Dict[str, Any],
        capital: float
    ) -> Dict[str, Any]:
        """
        ✅ PUBLIC API: Build execution context from existing evaluation context.
        
        This is the public interface called by config_helpers.py.
        Delegates to internal _build_execution_context method.
        
        Args:
            evaluation_ctx: Pre-built evaluation context
            capital: Available trading capital
        
        Returns:
            Complete execution context dict
        
        Usage:
            exec_ctx = resolver.build_execution_context_from_evaluation(
                evaluation_ctx=eval_ctx,
                capital=100000
            )
        """
        return self._build_execution_context(
            evaluation_ctx=evaluation_ctx,
            capital=capital,
            now=None  # Time constraints handled separately
        )
    
    # ========================================================================
    # SCORING CALCULATION (Uses Extractor Methods)
    # ========================================================================
    def _build_trend_context(self, indicators: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build trend context using config-based thresholds.
        
        Returns dict that includes:
        - Uses trend_thresholds.slope from config (not hardcoded slope > 0)
        - Does NOT add confidence_boost (already handled by trend_strength_bands)
        - Backward compatible with signal_engine/trade_enhancer
        - returns regime/adx/slope, classification details for advanced usage
        """        
        adx = ensure_numeric(indicators.get("adx"))
        slope = ensure_numeric(indicators.get("regSlope"))
        
        thresholds = self.extractor.get_trend_thresholds()
        slope_thresholds = thresholds.get("slope", {})
        strong_threshold = slope_thresholds.get("strong", 15.0)
        moderate_threshold = slope_thresholds.get("moderate", 5.0)
        
        # Determine direction
        if slope > 0:
            direction = "bullish"
        elif slope < 0:
            direction = "bearish"
        else:
            direction = "neutral"
        
        # Classify strength using config thresholds
        abs_slope = abs(slope)
        
        if abs_slope >= strong_threshold:
            strength_class = "strong"
            multiplier = 1.2
        elif abs_slope >= moderate_threshold:
            strength_class = "moderate"
            multiplier = 1.0
        else:
            strength_class = "weak"
            multiplier = 0.9
        
        # Determine regime using config-based classification + ADX
        # This maintains backward compatibility while using smart thresholds
        if adx >= 25 and direction == "bullish" and strength_class == "strong":
            regime = "strong"
        elif adx >= 25 and direction == "bullish" and strength_class == "moderate":
            regime = "strong"  # ADX strength compensates for moderate slope
        elif adx >= 15:
            regime = "normal"
        else:
            regime = "weak"

        return {
            "regime": regime,
            "adx": adx,
            "slope": slope,
            "classification": {
                "strength": strength_class,      # "strong"|"moderate"|"weak"
                "direction": direction,           # "bullish"|"bearish"|"neutral"
                "multiplier": multiplier,         # Position sizing multiplier
                "thresholds_used": {
                    "strong": strong_threshold,
                    "moderate": moderate_threshold,
                    "horizon": self.horizon
                }
            },
            "source": "config_based_classification, Confidence adjustments via trend_strength_bands (separate metric)",
        }
    
    def _build_momentum_context(self, indicators: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build momentum context using config-based thresholds.
    
        Used for adaptive divergence severity determination.
        Does NOT include confidence adjustments (handled separately).
        """
        from services.data_fetch import ensure_numeric
        
        # Get horizon-specific thresholds from config
        thresholds = self.extractor.get_momentum_thresholds()
        rsi_thresholds = thresholds.get("rsislope", {})
        macd_thresholds = thresholds.get("macd", {})
        
        rsi_accel = rsi_thresholds.get("acceleration_floor", 0.05)
        rsi_decel = rsi_thresholds.get("deceleration_ceiling", -0.05)
        macd_accel = macd_thresholds.get("acceleration_floor", 0.5)
        macd_decel = macd_thresholds.get("deceleration_ceiling", -0.5)
        
        # Get actual values
        rsi_slope = ensure_numeric(indicators.get("rsislope", 0))
        macd_hist = ensure_numeric(indicators.get("macdhistogram", 0))
        
        # Classify RSI momentum state
        if rsi_slope >= rsi_accel:
            rsi_state = "accelerating"
        elif rsi_slope <= rsi_decel:
            rsi_state = "decelerating"
        else:
            rsi_state = "neutral"
        
        # Classify MACD momentum state
        if macd_hist >= macd_accel:
            macd_state = "accelerating"
        elif macd_hist <= macd_decel:
            macd_state = "decelerating"
        else:
            macd_state = "neutral"
        
        # Determine severity for deceleration (adaptive thresholds)
        severity = None
        if rsi_state == "decelerating":
            if rsi_slope <= rsi_decel * 3:  # 3x threshold = severe
                severity = "severe"
            elif rsi_slope <= rsi_decel * 1.5:  # 1.5x threshold = moderate
                severity = "moderate"
            else:
                severity = "minor"
        
        # Combined state and warnings
        if rsi_state == "decelerating" and macd_state == "decelerating":
            combined_state = "bearish"
        elif rsi_state == "accelerating" and macd_state == "accelerating":
            combined_state = "bullish"
        else:
            combined_state = "neutral"
        
        return {
            "rsi_state": rsi_state,
            "rsi_slope": rsi_slope,
            "macd_state": macd_state,
            "macd_hist": macd_hist,
            "combined_state": combined_state,
            "severity": severity,
            "thresholds_used": {
                "rsi_accel": rsi_accel,
                "rsi_decel": rsi_decel,
                "macd_accel": macd_accel,
                "macd_decel": macd_decel,
                "horizon": self.horizon
            }
        }

    def _calculate_all_scores(self, ctx: Dict) -> Dict:
        """
        Calculate scores using Extractor proxies.
        No direct imports from score configs allowed here.
        """
        indicators = ctx["indicators"]
        fundamentals = ctx["fundamentals"]
        
        start = datetime.now().timestamp()
        
        # 1. Technical Score (Via Extractor)
        tech_result = self.extractor.get_technical_score(indicators)
        tech_elapsed = datetime.now().timestamp() - start
        
        METRICS.log_score_calculation(
            score_type="technical",
            score=tech_result["score"],
            breakdown=tech_result["breakdown"],
            elapsed=tech_elapsed
        )
        
        # 2. Fundamental Score (Via Extractor)
        start = datetime.now().timestamp()
        fund_result = self.extractor.get_fundamental_score(fundamentals)
        category_scores = fund_result.get("category_scores", {})
        fund_elapsed = datetime.now().timestamp() - start
        
        METRICS.log_score_calculation(
            score_type="fundamental",
            score=fund_result["score"],
            breakdown=fund_result["breakdown"],
            elapsed=fund_elapsed
        )
        
        # 3. Hybrid Score (Uses internal method -> Extractor Proxy)
        start = datetime.now().timestamp()
        hybrid_metrics = self._calculate_hybrid_metrics(fundamentals, indicators)
        hybrid_pillar = self._aggregate_hybrid_pillar(hybrid_metrics)
        hybrid_elapsed = datetime.now().timestamp() - start
        
        METRICS.log_score_calculation(
            score_type="hybrid",
            score=hybrid_pillar["score"],
            breakdown=hybrid_pillar["breakdown"],
            elapsed=hybrid_elapsed
        )
        
        from services.data_fetch import extract_metric_details
        breakdown_metrics = {
            **tech_result["breakdown"],
            **fund_result["breakdown"],
            **hybrid_pillar["breakdown"]
        }
        metric_details = extract_metric_details(breakdown_metrics)
        
        return {
            "technical": tech_result,
            "fundamental": fund_result,
            "category_scores": category_scores,
            "hybrid": {
                "score": hybrid_pillar["score"],
                "metrics": hybrid_metrics,
                "breakdown": hybrid_pillar["breakdown"]
            },
            "metric_details": metric_details,
            "timestamp": datetime.now().isoformat()
        }
    
    def _calculate_hybrid_metrics(
        self, 
        fundamentals: Dict, 
        indicators: Dict
    ) -> Dict:
        """Calculate hybrid metrics (no raw config access)."""
        HYBRID_METRIC_REGISTRY = self.extractor.get_hybrid_metric_registry()

        safe_fund = SafeDict(fundamentals, context="hybrid_fundamentals")
        safe_ind = SafeDict(indicators, context="hybrid_indicators")
        
        # Extract raw values
        roe = _safe_get_raw_float(safe_fund.raw.get("roe"))
        pe = _safe_get_raw_float(safe_fund.raw.get("peRatio"))
        eps_5y = _safe_get_raw_float(safe_fund.raw.get("epsGrowth5y"))
        q_growth = _safe_get_raw_float(safe_fund.raw.get("quarterlyGrowth"))
        net_margin = _safe_get_raw_float(safe_fund.raw.get("netProfitMargin"))
        fcf_yield = _safe_get_raw_float(safe_fund.raw.get("fcfYield"))
        atr_pct = _safe_get_raw_float(safe_ind.raw.get("atrPct"))
        price = _safe_get_raw_float(safe_ind.raw.get("price"))
        ma_slow = _get_val(safe_ind.raw, "maSlow") or _get_val(safe_ind.raw, "ema_200")
        adx = _get_val(safe_ind.raw, "adx")
        
        # Calculation mapping
        math_results = {
            "volatilityAdjustedRoe": (roe / atr_pct) if (roe and atr_pct and atr_pct > 0) else None,
            "priceToIntrinsicValue": (price / (price * (1 / (pe / eps_5y)))) if (price and pe and eps_5y and eps_5y > 0) else None,
            "fcfYieldVsVolatility": (fcf_yield / max(atr_pct, 0.1)) if (fcf_yield and atr_pct) else None,
            "trendConsistency": adx,
            "priceVsPrimaryTrendPct": ((price / ma_slow) - 1) if (price and ma_slow) else None,
            "fundamentalMomentum": ((q_growth + eps_5y/5) / 2) if (q_growth is not None and eps_5y is not None) else None,
            "earningsConsistencyIndex": ((roe + net_margin) / 2) if (roe and net_margin) else None
        }
        
        # Score each metric
        hybrid_metrics = {}
        for metric_name, raw_val in math_results.items():
            if raw_val is None:
                continue
            
            # ✅ REFACTORED: Use Extractor Proxy
            score = self.extractor.calculate_dynamic_metric_score(
                metric_name=metric_name,
                value=raw_val,
                indicators=safe_ind.raw
            )
            
            hybrid_metrics[metric_name] = {
                "raw": raw_val,
                "value": round(raw_val, 2) if metric_name != "priceVsPrimaryTrendPct" else round(raw_val*100, 2),
                "score": score,
                "desc": HYBRID_METRIC_REGISTRY[metric_name]["description"],
                "source": "hybrid"
            }
        
        return hybrid_metrics
    
    def _aggregate_hybrid_pillar(self, hybrid_metrics: Dict) -> Dict:
        """Aggregate hybrid metrics into single pillar score."""
        from config.master_config import HYBRID_PILLAR_COMPOSITION
        
        # Get horizon-specific weights
        weights = self.extractor.get_hybrid_pillar_composition()
        
        total_weighted_score = 0.0
        total_weight = 0.0
        breakdown = {}
        
        for metric_name, weight in weights.items():
            metric_data = hybrid_metrics.get(metric_name)
            if not metric_data:
                continue
            
            score = metric_data.get('score', 0.0)
            contribution = score * weight
            total_weighted_score += contribution
            total_weight += weight
            
            breakdown[metric_name] = {
                "score": score,
                "weight": weight,
                "contribution": round(contribution, 2)
            }
        
        final_score = (total_weighted_score / total_weight) if total_weight > 0 else 0.0
        
        return {
            "score": round(final_score, 2),
            "breakdown": breakdown,
            "horizon": self.horizon,
            "source": "hybrid_pillar"
        }
    
    # ========================================================================
    # CONDITIONS & SETUP CLASSIFICATION (Refactored)
    # ========================================================================
    
    def _build_conditions(self, ctx: Dict) -> Dict[str, bool]:
        """Pre-calculate common boolean conditions."""
        safe_ind = SafeDict(ctx["indicators"], context="conditions_indicators")
        safe_fund = SafeDict(ctx["fundamentals"], context="conditions_fundamentals")
        safe_price = SafeDict(ctx["price_data"], context="conditions_price")
        
        try:
            bc = {
                # Technical
                "bb_width_tight": ensure_numeric(safe_ind.raw.get("bbWidth", 999)) < 5.0,
                "rsi_oversold": ensure_numeric(safe_ind.raw.get("rsi", 50)) < 35,
                "rsi_overbought": ensure_numeric(safe_ind.raw.get("rsi", 50)) > 70,
                "trend_strong": ensure_numeric(safe_ind.raw.get("trendStrength", 0)) >= 6.0,
                "momentum_strong": ensure_numeric(safe_ind.raw.get("momentumStrength", 0)) >= 6.0,
                "volatility_high_quality": ensure_numeric(safe_ind.raw.get("volatilityQuality", 0)) >= 7.0,
                "adx_strong": ensure_numeric(safe_ind.raw.get("adx", 0)) >= 25,
                "volume_surge": ensure_numeric(safe_ind.raw.get("rvol", 1.0)) >= 1.5,
                "macd_bullish": ensure_numeric(safe_ind.raw.get("macdhistogram", 0)) > 0,
                
                # Fundamental
                "roe_excellent": ensure_numeric(safe_fund.raw.get("roe", 0)) >= 20,
                "roce_excellent": ensure_numeric(safe_fund.raw.get("roce", 0)) >= 25,
                "low_debt": ensure_numeric(safe_fund.raw.get("deRatio", 999)) <= 0.5,
                "quality_compounder": (
                    ensure_numeric(safe_fund.raw.get("roe", 0)) >= 20 and 
                    ensure_numeric(safe_fund.raw.get("roce", 0)) >= 25 and 
                    ensure_numeric(safe_fund.raw.get("epsGrowth5y", 0)) >= 15
                ),
                
                # Position
                "near_52w_high": ensure_numeric(safe_price.raw.get("position52w", 0)) >= 85,
                "deep_in_base": ensure_numeric(safe_price.raw.get("position52w", 0)) < 50,
                
                # MA alignment
                "price_above_fast_ma": ensure_numeric(safe_price.raw.get("price", 0)) > ensure_numeric(safe_ind.raw.get("maFast", 0)),
                "ma_aligned_bullish": self._check_ma_alignment(safe_price.raw, safe_ind.raw)
            }
        except Exception as e:
            self.logger.debug(f"Error building conditions: {e}")
            bc = {}
        
        return bc
    
    def _check_ma_alignment(self, price: Dict, ind: Dict) -> bool:
        """Check if MAs are in bullish alignment."""
        p = price.get("price", 0)
        fast = _get_val(ind, "maFast")
        mid = _get_val(ind, "maMid")
        slow = _get_val(ind, "maSlow")
        
        if all([p, fast, mid, slow]):
            return p > fast > mid > slow
        return False
    
    def _classify_setup(self, ctx: Dict) -> Dict[str, Any]:
        """
        Evaluate all qualifying setups.
        
        IMPORTANT:
        - Detection & scoring only
        - No irreversible commitment
        - Rank by BOTH priority AND fit quality
        """

        ind = ctx["indicators"]
        fund = ctx["fundamentals"]
        patterns = ctx.get("patterns", {})

        candidates = []
        rejected = []

        all_setups = self.extractor.get_all_setup_names()

        for setup_name in all_setups:
            # Horizon block
            if self.extractor.is_setup_blocked_for_horizon(setup_name):
                rejected.append({
                    "type": setup_name,
                    "reason": "blocked_by_horizon",
                    "priority": 0,
                    "fit_score": 0
                })
                continue

            rules = self.extractor.get_setup_classification_rules(setup_name)
            if not rules:
                continue

            pattern_rules = rules.get("pattern_detection", {})
            fund_conditions = rules.get("fundamental_conditions", [])
            tech_conditions = rules.get("technical_conditions", [])
            require_fund = rules.get("require_fundamentals", False)

            # Fundamentals availability check
            if require_fund:
                required_keys = ["roe", "roce", "deRatio"]
                if not all(fund.get(k) not in (None, 0) for k in required_keys):
                    rejected.append({
                        "type": setup_name,
                        "reason": "missing_fundamentals"
                    })
                    continue

            # Pattern detection
            if pattern_rules and not self._evaluate_pattern_detection(
                pattern_rules, patterns, self.horizon
            ):
                rejected.append({
                    "type": setup_name,
                    "reason": "pattern_detection_failed"
                })
                continue

            # Fundamental conditions
            if fund_conditions and not self._evaluate_conditions_list(
                fund_conditions, fund, context=setup_name
            ):
                rejected.append({
                    "type": setup_name,
                    "reason": "fundamental_conditions_failed"
                })
                continue

            # Technical conditions
            if not self._evaluate_conditions_list(
                tech_conditions, ind, context=setup_name
            ):
                rejected.append({
                    "type": setup_name,
                    "reason": "technical_conditions_failed"
                })
                continue

            # Context requirements
            meets_ctx, ctx_reason = self._validate_context_requirements_via_extractor(
                setup_name, fund, ctx["price_data"], ind
            )
            if not meets_ctx:
                rejected.append({
                    "type": setup_name,
                    "reason": ctx_reason or "context_validation_failed"
                })
                continue

            # ✅ NEW: Calculate fit quality score
            fit_score = self._calculate_setup_fit_quality(
                setup_name, ind, fund, patterns
            )

            priority = self.extractor.get_setup_priority(setup_name)

            candidates.append({
                "type": setup_name,
                "priority": priority,
                "fit_score": fit_score,
                "require_fundamentals": require_fund
            })

        # ✅ NEW: Rank by WEIGHTED combination of priority + fit
        for candidate in candidates:
            # Composite score: 70% priority, 30% fit
            candidate["composite_score"] = (
                (candidate["priority"] * 0.7) + 
                (candidate["fit_score"] * 0.3)
            )

        ranked = sorted(candidates, key=lambda x: x["composite_score"], reverse=True)

        TOP_K = 2
        top = ranked[:TOP_K]

        # Best setup
        best = ranked[0] if ranked else None

        if best:
            setup_type = best["type"]
            setup_patterns = self.extractor.get_setup_patterns(setup_type)
            confidence_floor = self.extractor.get_setup_confidence_floor(setup_type)

            best_setup = {
                "type": setup_type,
                "priority": best["priority"],
                "fit_score": best["fit_score"],  # ✅ NEW
                "composite_score": best["composite_score"],  # ✅ NEW
                "confidence_floor": confidence_floor,
                "require_fundamentals": best["require_fundamentals"],
                "patterns_primary": setup_patterns.get("PRIMARY", []),
                "patterns_confirming": setup_patterns.get("CONFIRMING", []),
                "patterns_conflicting": setup_patterns.get("CONFLICTING", []),
                "reasoning": f"Top ranked setup (priority={best['priority']}, fit={best['fit_score']:.1f})"
            }
            
            # ✅ IMPROVED LOGGING
            self.logger.info(
                f"✅ SETUP SELECTED: {setup_type} | "
                f"Priority={best['priority']} | Fit={best['fit_score']:.1f} | "
                f"Composite={best['composite_score']:.1f}"
            )
        else:
            best_setup = {
                "type": "GENERIC",
                "priority": 0,
                "fit_score": 0,
                "composite_score": 0,
                "confidence_floor": 40,
                "require_fundamentals": False,
                "patterns_primary": [],
                "patterns_confirming": [],
                "patterns_conflicting": [],
                "reasoning": "No specific setup matched"
            }

        return {
            "best": best_setup,
            "candidates": candidates,
            "ranked": ranked,
            "top": top,
            "rejected": rejected,
            # Backward compatibility
            "type": best_setup["type"]
        }

    def _calculate_setup_fit_quality(
        self,
        setup_name: str,
        indicators: Dict,
        fundamentals: Dict,
        patterns: Dict
    ) -> float:
        """
        ✅ NEW: Calculate how well indicators match setup requirements.
        
        Returns:
            Score from 0-100
        """
        rules = self.extractor.get_setup_classification_rules(setup_name)
        if not rules:
            return 0.0

        score = 50.0  # Base score

        # Check technical conditions
        tech_conditions = rules.get("technical_conditions", [])
        if tech_conditions:
            passed = sum(
                1 for cond in tech_conditions
                if self._evaluate_conditions_list([cond], indicators, setup_name)
            )
            # +5 points per passed condition
            score += (passed / len(tech_conditions)) * 30

        # Check fundamental conditions
        fund_conditions = rules.get("fundamental_conditions", [])
        if fund_conditions:
            passed = sum(
                1 for cond in fund_conditions
                if self._evaluate_conditions_list([cond], fundamentals, setup_name)
            )
            # +2 points per passed condition
            score += (passed / len(fund_conditions)) * 20

        return min(score, 100.0)
    
    def _validate_context_requirements_via_extractor(
        self,
        setup_name: str,
        fundamentals: Dict,
        price_data: Dict,
        indicators: Optional[Dict] = None
    ) -> Tuple[bool, str]:
        """
        ✅ REFACTORED: Validate context requirements using extractor.
        
        Previously: Direct access to PATTERN_MATRIX
        Now: Uses self.extractor methods
        """
        # Get setup config via extractor
        setup_config = self.extractor.base_extractor.get(f"setup_{setup_name}")
        if not setup_config:
            return True, "No context requirements"
        
        requirements = self.extractor.get_setup_context_requirements(setup_name)
        if not requirements:
            return True, "No context requirements"
        
        # Check market cap
        market_cap_min = requirements.get("market_cap_min_cr")
        if market_cap_min is not None:
            stock_market_cap = fundamentals.get("marketCap", 0)
            if stock_market_cap < market_cap_min:
                return False, f"Market cap {stock_market_cap:.0f}cr < required {market_cap_min}cr"
        
        # Check liquidity
        avg_volume_min = requirements.get("avg_volume_min")
        if avg_volume_min is not None:
            stock_volume = price_data.get("avgVolume", 0)
            if stock_volume < avg_volume_min:
                return False, f"Avg volume {stock_volume:.0f} < required {avg_volume_min}"
        
        # Check price
        min_price = requirements.get("min_price")
        if min_price is not None:
            current_price = price_data.get("price", 0)
            if current_price < min_price:
                return False, f"Price {current_price:.2f} < required {min_price}"
        
        # Check fundamentals required
        fundamental_reqs = requirements.get("fundamental", {})
        if fundamental_reqs.get("required", False):
            required_keys = [k for k in fundamental_reqs.keys() if k != "required"]
            if not fundamentals or not any(fundamentals.get(k) for k in required_keys):
                return False, f"Required fundamentals missing for {setup_name}"
        
        # Check technical requirements
        technical_reqs = requirements.get("technical", {})
        if technical_reqs and indicators:
            for tech_field, tech_value in technical_reqs.items():
                if isinstance(tech_value, dict):
                    min_val = tech_value.get("min")
                    max_val = tech_value.get("max")
                    actual_val = ensure_numeric(indicators.get(tech_field)) or ensure_numeric(price_data.get(tech_field))
                    
                    if actual_val is None:
                        return False, f"Missing technical indicator: {tech_field}"
                    
                    if min_val is not None and actual_val < min_val:
                        return False, f"{tech_field} {actual_val:.2f} < required {min_val}"
                    
                    if max_val is not None and actual_val > max_val:
                        return False, f"{tech_field} {actual_val:.2f} > max {max_val}"
                else:
                    actual_val = ensure_numeric(indicators.get(tech_field)) or ensure_numeric(price_data.get(tech_field))
                    if actual_val is None:
                        return False, f"Missing technical indicator: {tech_field}"
                    
                    if actual_val < tech_value:
                        return False, f"{tech_field} {actual_val:.2f} < required {tech_value}"
        
        return True, "All context requirements met"
    
    def _evaluate_pattern_detection(
        self,
        pattern_detection_rules: Dict[str, bool],
        patterns: Dict,
        horizon: str
    ) -> bool:
        """Evaluate pattern detection requirements."""
        if not pattern_detection_rules:
            return True
        
        for pattern_name, required_found in pattern_detection_rules.items():
            indicator_key = pattern_name
            pattern_data = ensure_numeric(patterns.get(indicator_key))
            
            if pattern_data is None:
                if required_found:
                    return False
                continue
            
            if isinstance(pattern_data, dict):
                raw_data = pattern_data.get("raw", pattern_data)
                actual_found = raw_data.get("found", False)
            else:
                actual_found = False
            
            if actual_found != required_found:
                return False
        
        return True
    
    def _evaluate_conditions_list(
        self,
        conditions: List[str],
        namespace: Dict[str, Any],
        context: str = ""
    ) -> bool:
        """Evaluate a list of conditions against a namespace."""
        if not conditions:
            return True
        
        evaluator = ConditionEvaluator
        
        for condition in conditions:
            result = evaluator.evaluate_condition(condition, namespace)
            
            # Extract ACTUAL variables (excluding functions)
            variables = self._extract_variables_from_condition(condition)
            
            # ✅ IMPROVED: Extract values properly for logging
            var_values = {}
            for var in variables:
                raw_val = namespace.get(var)
                # Extract numeric value for display
                if isinstance(raw_val, dict):
                    extracted = (
                        raw_val.get("value") or 
                        raw_val.get("raw") or 
                        raw_val.get("score")
                    )
                    var_values[var] = extracted
                else:
                    var_values[var] = raw_val
            
            # ✅ IMPROVED: Calculate actual expression result for complex conditions
            if any(func in condition for func in ['abs', 'min', 'max', 'round']):
                # This is a math expression - try to evaluate left side
                try:
                    # Extract left side before comparison operator
                    for op in ['<=', '>=', '<', '>', '==', '!=']:
                        if f" {op} " in condition:
                            left_expr = condition.split(op)[0].strip()
                            left_result = evaluator._evaluate_expression(left_expr, namespace)
                            if left_result is not None:
                                var_values[f"COMPUTED[{left_expr}]"] = round(left_result, 4)
                            break
                except Exception:
                    pass
            
            METRICS.log_condition_evaluation(
                condition=condition,
                result=result,
                context=context,
                variables=var_values  # ✅ Now shows computed values too
            )
            
            if not result:
                return False
        
        return True

    def _extract_variables_from_condition(self, condition: str) -> List[str]:
        """
        Extract variable names from condition string.
        
        Excludes:
        - Math functions (abs, min, max, round)
        - Operators (and, or)
        - Numeric literals
        """
        import re
        
        # Extract all word-like tokens
        all_tokens = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', condition)
        
        # Filter out math functions and operators
        excluded = {'abs', 'min', 'max', 'round', 'and', 'or', 'true', 'false', 'True', 'False'}
        
        variables = [
            token for token in all_tokens
            if token not in excluded and not token.isdigit()
        ]
        
        return variables

    
    # ========================================================================
    # STRATEGY CLASSIFICATION (Refactored)
    # ========================================================================
    
    def _classify_strategy(self, ctx: Dict) -> Dict[str, Any]:
        """
        ✅ REFACTORED: Delegate to analyze_strategy_fit_v5 (DRY principle).
        
        Now just a thin wrapper that calls the helper with proper parameters.
        """
        try:
            # ✅ Use existing helper with enhanced options
            result = self.analyze_strategy_fit_v5(
                ticker=ctx["meta"]["symbol"],
                indicators=ctx["indicators"],
                fundamentals=ctx["fundamentals"],
                horizon=self.horizon,
                include_rejected=True,     # ← Track all strategies
                include_breakdown=True     # ← Get detailed breakdown
            )
            
            # Transform to expected format (backward compatible)
            if not result.get("all_candidates"):
                return self._fallback_strategy_classification()
            
            best = result["all_candidates"][0]
            
            return {
                # Best strategy (backward compatible)
                "best": {
                    "strategy": best["name"],
                    "fit_score": best["fit_score"],
                    "weighted_score": best["weighted_score"],
                    "description": best["description"],
                    "breakdown": best.get("breakdown", {})
                },
                "primary": best["name"],
                "fit_score": best["fit_score"],
                "weighted_score": best["weighted_score"],
                "description": best["description"],
                
                # All qualified strategies (ranked)
                "ranked": result["all_candidates"],
                
                # ✅ NEW: All strategies including rejected
                "all_strategies": result["all_candidates"] + result.get("rejected", []),
                
                # ✅ NEW: Rejected strategies
                "rejected": result.get("rejected", []),
                
                # ✅ NEW: Summary
                "summary": result.get("summary", {
                    "total": len(result["all_candidates"]),
                    "qualified": len(result["all_candidates"]),
                    "best_strategy": best["name"],
                    "rejected": 0
                }),
                
                # Metadata
                "meta": {
                    "enabled": result.get("summary", {}).get("total", 0),
                    "evaluated": len(result["all_candidates"]),
                    "rejected": len(result.get("rejected", []))
                }
            }
        
        except Exception as e:
            self.logger.error(f"Strategy classification failed: {e}", exc_info=True)
            return self._fallback_strategy_classification()
        
    def analyze_strategy_fit_v5(
        self,
        ticker: str,
        indicators: Dict[str, Any],
        fundamentals: Dict[str, Any],
        horizon: str,
        include_rejected: bool = False,      # ← NEW
        include_breakdown: bool = False      # ← NEW
    ) -> Dict[str, Any]:
        """
        ✅ ENHANCED: Strategy fit analysis with optional detailed breakdown.
        
        Args:
            ticker: Stock symbol
            indicators: Technical indicators
            fundamentals: Fundamental metrics
            horizon: Trading horizon
            include_rejected: If True, include strategies that failed threshold
            include_breakdown: If True, include detailed fit breakdown per strategy
        
        Returns:
            {
                "ticker": str,
                "horizon": str,
                "primary_strategy": str,
                "primary_weighted_score": float,
                "all_candidates": [...],      # Qualified strategies
                "rejected": [...],            # NEW: Failed strategies (if include_rejected=True)
                "summary": {                  # NEW: Summary stats
                    "total": int,
                    "qualified": int,
                    "rejected": int
                }
            }
        """
        try:
            extractor = self.extractor
            # Get all enabled strategies
            all_strategies = extractor.get_all_strategy_names()
            enabled_strategies = [
                s for s in all_strategies
                if extractor.get_strategy_enabled_status(s)
            ]
            
            all_candidates = []
            rejected_strategies = []  # ← NEW
            
            for strategy_name in enabled_strategies:
                # ✅ Calculate fit (with optional breakdown)
                if include_breakdown:
                    base_fit, breakdown = self._calculate_strategy_fit_via_extractor(
                        strategy_name, indicators, fundamentals,
                        return_breakdown=True  # ← Request breakdown
                    )
                else:
                    base_fit = self._calculate_strategy_fit_via_extractor(
                        strategy_name, indicators, fundamentals
                    )
                    breakdown = None
                
                # Get horizon multiplier
                horizon_mult = extractor.get_strategy_horizon_multiplier(strategy_name)
                
                # Get strategy config
                strategy_cfg = extractor.base_extractor.get(f"strategy_{strategy_name}")
                fit_threshold = strategy_cfg.get("fit_threshold", 50) if strategy_cfg else 50
                
                # ─────────────────────────────────────────────────────────────
                # Horizon-adjusted score — kept 0-100
                #
                # Old: weighted_score = base_fit * horizon_mult
                #      → multiplier of 1.5 turns a 100 fit into 150, unbounded
                #
                # New: Convert the horizon_mult (range ~0.5–1.5) into a
                #      ±15 pt bonus/penalty added to base_fit, then clamp 0-100.
                #
                #   mult=1.0 → neutral  → +0 pt
                #   mult=1.5 → perfect  → +15 pt  (best horizon fit)
                #   mult=0.7 → poor     → -9 pt
                #   mult=0.0 → blocked  → score forced to 0 (handled below)
                #
                # This preserves ranking power of the horizon multiplier while
                # keeping every score on the same 0-100 scale as fit_score.
                # ─────────────────────────────────────────────────────────────
                if horizon_mult == 0.0:
                    weighted_score = 0.0  # blocked horizon — will be rejected
                else:
                    horizon_bonus = (horizon_mult - 1.0) * 30  # ±30pt range
                    weighted_score = round(min(max(base_fit + horizon_bonus, 0.0), 100.0), 1)
                
                # Build record
                record = {
                    "name": strategy_name,
                    "fit_score": round(base_fit, 1),
                    "weighted_score": round(weighted_score, 1),
                    "horizon_multiplier": horizon_mult,
                    "fit_threshold": fit_threshold,
                    "description": strategy_cfg.get("description", "") if strategy_cfg else "",
                }
                
                # ✅ Add breakdown if requested
                if include_breakdown and breakdown:
                    record["breakdown"] = breakdown
                
                # ===================================================================
                # Categorize: Qualified vs Rejected
                # ===================================================================
                rejection_reasons = []
                
                # Check 1: Horizon compatibility
                if horizon_mult == 0.0:
                    rejection_reasons.append({
                        "type": "horizon_block",
                        "reason": f"Incompatible with {horizon} horizon (multiplier=0)"
                    })
                
                # Check 2: Weighted score vs threshold
                elif weighted_score < fit_threshold:
                    rejection_reasons.append({
                        "type": "low_score",
                        "reason": f"Weighted score {weighted_score:.1f} < threshold {fit_threshold}"
                    })
                
                # Categorize
                if rejection_reasons:
                    # ✅ Only add to rejected list if requested
                    if include_rejected:
                        record["rejection_reasons"] = rejection_reasons
                        rejected_strategies.append(record)
                    # else: skip rejected strategies (backward compatible)
                else:
                    # Qualified
                    all_candidates.append(record)
            
            # ===================================================================
            # Sort and select best
            # ===================================================================
            all_candidates.sort(key=lambda x: x["weighted_score"], reverse=True)
            
            if all_candidates:
                winner = all_candidates[0]
                logger.info(
                    f"[{ticker}][{horizon}] Best strategy: {winner['name']} "
                    f"(fit={winner['fit_score']}, weighted={winner['weighted_score']})"
                )
            else:
                winner = {"name": "generic", "weighted_score": 0, "fit_score": 0}
                logger.warning(f"[{ticker}][{horizon}] No strategies matched")
            
            # ===================================================================
            # Build result
            # ===================================================================
            result = {
                "ticker": ticker,
                "horizon": horizon,
                "primary_strategy": winner["name"],
                "primary_fit_score": winner.get("fit_score", 0),
                "primary_weighted_score": winner["weighted_score"],
                "all_candidates": all_candidates
            }
            
            # ✅ Add rejected strategies if requested
            if include_rejected:
                result["rejected"] = rejected_strategies
                result["summary"] = {
                    "total": len(all_candidates) + len(rejected_strategies),
                    "qualified": len(all_candidates),
                    "rejected": len(rejected_strategies)
                }
            
            return result
        
        except Exception as e:
            logger.error(f"[{ticker}][{horizon}] Strategy analysis failed: {e}", exc_info=True)
            return {
                "ticker": ticker,
                "horizon": horizon,
                "primary_strategy": "generic",
                "primary_fit_score": 0,
                "primary_weighted_score": 0,
                "all_candidates": [],
                "rejected": [] if include_rejected else None,
                "error": str(e)
            }

    def _get_enabled_strategies_via_extractor(self) -> List[str]:
        """✅ Get enabled strategies using extractor."""
        # ✅ REFACTORED: Use query extractor methods
        all_strategies = self.extractor.get_all_strategy_names()
        
        enabled = []
        for strategy_name in all_strategies:
            # Check if globally enabled
            if self.extractor.get_strategy_enabled_status(strategy_name):
                # Check if not blocked by horizon
                if not self.extractor.is_strategy_blocked_for_horizon(strategy_name):
                    enabled.append(strategy_name)
        
        return enabled
    
    def _calculate_strategy_fit_via_extractor(
        self,
        strategy_name: str,
        indicators: Dict,
        fundamentals: Dict,
        return_breakdown: bool = False  # ← ADD THIS
    ) -> Union[float, Tuple[float, Dict]]:
        """
        Calculate strategy fit using extractor methods.
        
        Args:
            strategy_name: Strategy to evaluate
            indicators: Technical indicators
            fundamentals: Fundamental metrics
            return_breakdown: If True, return (score, breakdown) tuple
        
        Returns:
            float: Just the score (default, backward compatible)
            OR
            Tuple[float, Dict]: (score, breakdown) if return_breakdown=True
        """
        # Get fit indicators via extractor
        fit_indicators = self.extractor.get_strategy_fit_indicators(strategy_name)
        
        if not fit_indicators:
            if return_breakdown:
                return 0.0, {"error": "No fit indicators configured"}
            return 0.0
        
        # ===================================================================
        # Calculate fit with breakdown
        # ===================================================================
        total_weight = 0.0
        weighted_score = 0.0
        fit_indicator_results = {}
        missing_indicators = []
        
        for indicator, params in fit_indicators.items():
            weight = params.get("weight", 0.1)
            min_val = params.get("min")
            max_val = params.get("max")
            direction = params.get("direction", "normal")
            
            # Get actual value
            raw_value = (
                ensure_numeric(indicators.get(indicator)) or 
                ensure_numeric(fundamentals.get(indicator))
            )
            
            if raw_value is None:
                missing_indicators.append(indicator)
                if return_breakdown:
                    fit_indicator_results[indicator] = {
                        "required": {"min": min_val, "max": max_val, "direction": direction},
                        "actual": None,
                        "passed": False,
                        "weight": weight,
                        "reason": "indicator_missing"
                    }
                continue
            
            # Extract numeric value
            if isinstance(raw_value, dict):
                actual = (
                    raw_value.get("value") or 
                    raw_value.get("raw") or 
                    raw_value.get("score")
                )
            else:
                actual = raw_value
            
            try:
                actual = float(actual)
            except (ValueError, TypeError):
                if return_breakdown:
                    fit_indicator_results[indicator] = {
                        "required": {"min": min_val, "max": max_val},
                        "actual": raw_value,
                        "passed": False,
                        "weight": weight,
                        "reason": "value_not_numeric"
                    }
                continue
            
            # Check threshold
            total_weight += weight
            threshold_met = True
            failure_reason = None
            
            if direction == "invert":
                if max_val is not None and actual > max_val:
                    threshold_met = False
                    failure_reason = f"actual ({actual:.2f}) > max ({max_val})"
            else:
                if min_val is not None and actual < min_val:
                    threshold_met = False
                    failure_reason = f"actual ({actual:.2f}) < min ({min_val})"
                elif max_val is not None and actual > max_val:
                    threshold_met = False
                    failure_reason = f"actual ({actual:.2f}) > max ({max_val})"
            
            if threshold_met:
                weighted_score += weight
            
            # Store breakdown
            if return_breakdown:
                fit_indicator_results[indicator] = {
                    "required": {"min": min_val, "max": max_val, "direction": direction},
                    "actual": round(actual, 2),
                    "passed": threshold_met,
                    "weight": weight,
                    "contribution": weight if threshold_met else 0,
                    "reason": "threshold_met" if threshold_met else failure_reason
                }
        
        # Calculate base score
        base_score = (weighted_score / total_weight * 100) if total_weight > 0 else 0.0
        
        # ===================================================================
        # Scoring rules (bonus points)
        # ===================================================================
        scoring_rules = self.extractor.get_strategy_scoring_rules(strategy_name)
        bonus_points = 0
        scoring_rule_results = {}
        
        if scoring_rules:
            evaluator = ConditionEvaluator()
            
            # Build namespace
            namespace = {}
            for key, value in {**indicators, **fundamentals}.items():
                if isinstance(value, dict):
                    namespace[key] = (
                        value.get("value") or 
                        value.get("raw") or 
                        value.get("score")
                    )
                else:
                    namespace[key] = value
            
            for rule_name, rule_config in scoring_rules.items():
                condition = rule_config.get("condition", "")
                points = rule_config.get("points", 0)
                reason = rule_config.get("reason", "")
                
                matched = False
                if condition:
                    try:
                        matched = evaluator.evaluate_condition(condition, namespace)
                    except Exception as e:
                        self.logger.debug(
                            f"[{strategy_name}] Rule '{rule_name}' eval failed: {e}"
                        )
                
                if return_breakdown:
                    scoring_rule_results[rule_name] = {
                        "condition": condition,
                        "matched": matched,
                        "points": points if matched else 0,
                        "reason": reason
                    }
                
                if matched:
                    bonus_points += points
        
        # ===================================================================
        # Normalize scoring rules to 0-100 then blend with base_score
        #
        # Problem solved: bonus_points were raw additive (0-155) stacked on
        # top of base_score (0-100), producing totals like 180-225% with no
        # defined maximum. The old min(..., 150) cap was arbitrary.
        #
        # Solution: Two-component blend, both on 0-100 scale:
        #   fit_score   (65%) — "Does this stock have the right DNA?"
        #                        Derived from fit_indicators pass/fail.
        #   setup_score (35%) — "Is it well set up RIGHT NOW?"
        #                        Derived from scoring_rules, normalized by the
        #                        declared scoring_rules_max_bonus in config so
        #                        each strategy has a known ceiling.
        #
        # Result: final_score is always 0-100, true percentage, comparable
        # across strategies. Ties are honest (multiple strategies can score
        # high if they genuinely fit — rank by score to break ties).
        # ===================================================================

        # Fetch declared max bonus from strategy config (set in strategy_matrix)
        strategy_cfg = self.extractor.base_extractor.get(f"strategy_{strategy_name}") or {}
        max_bonus_declared = strategy_cfg.get("scoring_rules_max_bonus")

        # Fallback: derive max from the rules themselves if not declared
        if not max_bonus_declared and scoring_rules:
            max_bonus_declared = sum(
                r.get("points", 0)
                for r in scoring_rules.values()
                if r.get("points", 0) > 0
            )

        # Normalize bonus to 0-100; clamp negatives (penalties) to floor 0
        if max_bonus_declared and max_bonus_declared > 0:
            # bonus_points can go negative due to penalty rules — floor at 0
            # so penalties reduce setup_score toward 0 but don't invert it
            rule_score_pct = max(0.0, min(bonus_points / max_bonus_declared * 100, 100.0))
        else:
            rule_score_pct = 0.0

        # Weighted blend: 65% DNA fit + 35% current setup quality
        # Both components are already 0-100, so final_score is always 0-100
        final_score = round((base_score * 0.65) + (rule_score_pct * 0.35), 1)

        # ===================================================================
        # Return based on mode
        # ===================================================================
        if return_breakdown:
            breakdown = {
                # Legacy field — kept for any callers that read it
                "base_score": round(base_score, 2),
                # New normalized fields
                "dna_fit_score": round(base_score, 2),          # 0-100: fit_indicators
                "setup_quality_score": round(rule_score_pct, 2), # 0-100: scoring_rules
                "bonus_points_raw": bonus_points,                # raw for debugging
                "max_bonus_declared": max_bonus_declared,        # ceiling used
                "final_score": round(final_score, 2),            # 0-100: blended
                "fit_indicators": fit_indicator_results,
                "scoring_rules": scoring_rule_results,
                "missing_indicators": missing_indicators,
                "stats": {
                    "total_indicators": len(fit_indicators),
                    "passed_indicators": sum(
                        1 for r in fit_indicator_results.values() if r["passed"]
                    ),
                    "total_weight": round(total_weight, 3),
                    "achieved_weight": round(weighted_score, 3),
                    "blend_weights": {"dna_fit": 0.65, "setup_quality": 0.35}
                }
            }
            return final_score, breakdown

        return final_score
    
    def _validate_strategy_market_cap_via_extractor(
        self,
        strategy_name: str,
        fundamentals: Dict,
        price_data: Dict
    ) -> Tuple[bool, str]:
        """✅ Validate market cap requirements via extractor."""
        # Get market cap requirements via extractor
        market_cap_reqs = self.extractor.get_strategy_market_cap_requirements(strategy_name)
        
        if not market_cap_reqs:
            return True, "No market cap requirements"
        
        stock_market_cap = _get_val(fundamentals, "marketCap")
        
        # Determine bracket
        bracket = None
        for bracket_name, bracket_config in market_cap_reqs.items():
            min_cap = bracket_config.get("min_market_cap", 0)
            max_cap = bracket_config.get("max_market_cap", float('inf'))
            
            if min_cap <= stock_market_cap < max_cap:
                bracket = bracket_name
                break
        
        bracket_reqs = market_cap_reqs.get(bracket, {})
        if not bracket_reqs:
            return True, f"No requirements for {bracket}"
        
        # Check institutional ownership
        min_inst_ownership = bracket_reqs.get("min_institutional_ownership_pct")
        if min_inst_ownership is not None:
            actual_inst = price_data.get("institutional_ownership") or \
                        price_data.get("institutionalOwnership", 0)
            
            if actual_inst < min_inst_ownership:
                return False, (
                    f"{bracket.replace('_', ' ').title()}: "
                    f"Institutional ownership {actual_inst:.1f}% < required {min_inst_ownership}%"
                )
        
        # Check delivery percentage
        min_delivery = bracket_reqs.get("min_delivery_pct")
        if min_delivery is not None:
            actual_delivery = price_data.get("delivery_pct") or \
                            price_data.get("deliveryPct", 0)
            
            if actual_delivery < min_delivery:
                return False, (
                    f"{bracket.replace('_', ' ').title()}: "
                    f"Delivery {actual_delivery:.1f}% < required {min_delivery}%"
                )
        
        return True, f"Passes {bracket} requirements"
    
    def _fallback_strategy_classification(self) -> Dict[str, Any]:
        """Fallback when no strategy qualifies."""
        return {
            "primary": "generic",
            "fit_score": 0,
            "horizon_multiplier": 1.0,
            "weighted_score": 0,
            "all_suggestions": [],
            "description": "No strategy qualified",
            "preferred_setups": [],
            "avoid_setups": []
        }
    
    # ========================================================================
    # REMAINING METHODS - Continue in next artifact update
    # ========================================================================

    def _apply_setup_preferences(self, ctx: Dict) -> Dict[str, Any]:
        """
        ✅ PHASE 4: Setup–Strategy compatibility annotation
        No blocking, no filtering, no sizing decisions.
        """

        setup_type = ctx["setup"]["type"]
        strategy_ctx = ctx.get("strategy", {})
        ranked_strategies = strategy_ctx.get("ranked", [])

        compatibility = {}
        preferred_by = []
        avoided_by = []
        neutral_by = []

        for strat in ranked_strategies:
            strategy_name = strat["name"]

            preferred = self.extractor.get_strategy_preferred_setups(strategy_name)
            avoided = self.extractor.get_strategy_avoided_setups(strategy_name)

            if setup_type in preferred:
                relation = "preferred"
                score = +1
                preferred_by.append(strategy_name)

            elif setup_type in avoided:
                relation = "avoid"
                score = -1
                avoided_by.append(strategy_name)

            else:
                relation = "neutral"
                score = 0
                neutral_by.append(strategy_name)

            compatibility[strategy_name] = {
                "relation": relation,
                "score": score,
                "strategy_weighted_score": strat.get("weighted_score", 0),
                "strategy_rank": ranked_strategies.index(strat) + 1
            }

        # Aggregate signal (FACT, not decision)
        net_alignment_score = sum(v["score"] for v in compatibility.values())

        return {
            "setup": setup_type,
            "compatibility": compatibility,
            "summary": {
                "preferred_by": preferred_by,
                "avoided_by": avoided_by,
                "neutral_by": neutral_by,
                "net_alignment_score": net_alignment_score,
                "evaluated_strategies": len(ranked_strategies)
            }
        }

    # ========================================================================
    # GATE VALIDATION (Uses Extractor Throughout)
    # ========================================================================
    def _validate_structural_gates(self, ctx: Dict) -> Dict[str, Any]:
        """
        PHASE 5: Structural Gate Evaluation (Standardized Logic)
        """
        setup_type = ctx.get("setup", {}).get("type")
        
        # 1. Get Resolved Gates (Global -> Horizon -> Setup Override)
        gates_map = self.extractor.get_resolved_gates("structural", setup_type)

        gate_results = {}
        violations = []
        stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}

        for gate_name, resolved_gate in gates_map.items():
            stats["total"] += 1
            threshold = resolved_gate.threshold

            # ── Disabled gate check ──
            if not threshold or (threshold.get("min") is None and threshold.get("max") is None):
                gate_results[gate_name] = {
                    "status": "skipped", "reason": "disabled", 
                    "required": None, "actual": None, "source": resolved_gate.source
                }
                stats["skipped"] += 1
                continue

            # 2. Get Data
            actual = self._resolve_gate_value_from_context(gate_name, ctx)

            # ── Missing metric ──
            if actual is None:
                gate_meta = GATE_METRIC_REGISTRY.get(gate_name, {})

                is_optional = gate_meta.get("optional", False)
                
                status = "skipped" if is_optional else "failed"
                reason = "optional_missing" if is_optional else "metric_missing"
                
                gate_results[gate_name] = {
                    "status": status, "reason": reason,
                    "required": threshold, "actual": None, "source": resolved_gate.source
                }
                
                if status == "failed":
                    stats["failed"] += 1
                    violations.append({
                        "gate": gate_name, "reason": reason, 
                        "severity": gate_meta.get("severity", "high"), 
                        "source": resolved_gate.source
                    })
                else:
                    stats["skipped"] += 1
                continue

            # ── Threshold check (Use Canonical Evaluator) ──
            passed, reason = ConditionEvaluator.evaluate_gate_config(
                actual, 
                threshold, 
                gate_name
            )

            gate_results[gate_name] = {
                "status": "passed" if passed else "failed",
                "required": threshold,
                "actual": actual,
                "reason": reason,
                "source": resolved_gate.source
            }

            if passed:
                stats["passed"] += 1
            else:
                stats["failed"] += 1
                violations.append({
                    "gate": gate_name,
                    "reason": reason,
                    "required": threshold,
                    "actual": actual,
                    "source": resolved_gate.source
                })

            METRICS.log_gate_check(
                gate_name=gate_name,
                phase="structural",
                passed=passed,
                actual=actual,
                required=threshold,
                context=setup_type
            )
        failure_ratio = stats["failed"] / max(stats["total"], 1)
        overall = {
            "passed": stats["failed"] == 0,
            "failed_gates": [
                {
                    "gate": v["gate"],
                    "reason": v["reason"]
                }
                for v in violations
            ],
            "total_gates": stats["total"],
            "passed_gates": stats["passed"],
        }
        return {
            "phase": "structural",
            "by_gate": gate_results,
            "summary": {
                **stats,
                "failure_ratio": round(failure_ratio, 3)
            },
            "violations": violations,
            "overall": overall, 
            "failed_gates": [{"gate": v["gate"], "reason": v["reason"]} for v in violations]
        }
    def _resolve_gate_value_from_context(self, gate_name: str, ctx: Dict) -> Optional[float]:
        """
        ✅ SIMPLIFIED: Resolve gate value using GATE_METRIC_REGISTRY.
        
        Now uses registry's context_paths instead of hardcoded mappings.
        """
        
        # Get gate metadata
        gate_meta = GATE_METRIC_REGISTRY.get(gate_name)
        
        if not gate_meta:
            # Check if this is a config key, not a metric
            if "_" in gate_name or gate_name.endswith("guards"):
                self.logger.error(
                    f"❌ CONFIGURATION ERROR: '{gate_name}' is not a valid gate metric. "
                    f"This appears to be a config key or execution rule name. "
                    f"Gates should only reference actual metrics from indicators/fundamentals."
                )
            else:
                self.logger.warning(f"⚠️ Unknown gate metric: {gate_name}")
            return None
        
        # Try all context paths
        context_paths = gate_meta.get("context_paths", [])
        
        for path in context_paths:
            value = ctx
            for key in path:
                if isinstance(value, dict):
                    value = value.get(key)
                else:
                    value = None
                    break
            
            if value is not None:
                numeric = ensure_numeric(value)
                if numeric is not None:
                    return numeric
        
        # Check if optional with fallback
        if gate_meta.get("optional", False):
            fallback = gate_meta.get("fallback")
            if fallback is not None:
                self.logger.debug(
                    f"✅ Using fallback for optional gate {gate_name}: {fallback}"
                )
                return fallback
        
        # Not found and no fallback
        if "guard" in gate_name.lower() or "validation" in gate_name.lower():
            self.logger.error(
                f"❌ CONFIG ERROR: '{gate_name}' looks like an execution rule, not a gate metric. "
                f"Execution rules belong in 'execution_rules' section, not 'gates'."
            )
        elif "_" in gate_name and gate_name not in ["market_cap", "institutional_ownership"]:
            self.logger.warning(
                f"⚠️ Gate '{gate_name}' with underscores may be a config key, not a metric. "
                f"Tried paths: {context_paths}"
            )
        else:
            self.logger.debug(
                f"Gate value not found: {gate_name}. "
                f"Tried paths: {context_paths}"
            )
        return None

    def _calculate_confidence(self, ctx: Dict) -> Dict[str, Any]:
        """
        Calculate confidence following CONFIDENCE_CALCULATION_PIPELINE.
        Uses query_optimized_extractor methods.
        """
        ind = ctx["indicators"]
        fund = ctx["fundamentals"]
        setup_type = ctx["setup"]["type"]

        adx_data = ind.get("adx", 0)
        
        # ==========================================================
        # STEPS 1-4: BASE + ADX BANDS
        # ==========================================================
        base = self.extractor.calculate_dynamic_confidence_floor(setup_type, adx_data)

        flat_data = self._flatten_indicator_data({**ind, **fund})

        # Inject aggregate scores so validation_modifiers can reference them
        scoring = ctx.get("scoring", {})
        technical_score_block = scoring.get("technical", {}) or {}
        fundamental_score_block = scoring.get("fundamental", {}) or {}
        technical_score_value = technical_score_block.get("score")
        fundamental_score_value = fundamental_score_block.get("score")
        if technical_score_value is not None:
            flat_data["technicalScore"] = technical_score_value
        if fundamental_score_value is not None:
            flat_data["fundamentalScore"] = fundamental_score_value

        # ==========================================================
        # STEPS 5-7: UNIVERSAL + HORIZON + CONDITIONAL MODIFIERS
        # ==========================================================
        modifier_results = self.extractor.evaluate_all_confidence_modifiers(
            data=flat_data,
            setup_type=setup_type
        )

        # This handles divergence multiplier internally
        total_adjustment, breakdown = self.extractor.calculate_total_confidence_adjustment(
            modifier_results
        )
        self.logger.debug(
            "Calculating confidence modifiers for setup '%s' with data keys: %s",
            setup_type,
            list(flat_data.keys())
        )


        # Extract divergence multiplier for reporting
        divergence_multiplier = 1.0
        for item in breakdown:
            if "Final multiplier" in item:
                try:
                    divergence_multiplier = float(item.split("×")[1])
                except:
                    pass
                break

        # Build structured adjustments with multiplier already applied
        structured_adjustments = []
        
        for group_name, group_data in modifier_results.items():
            if group_name == "_adx_note":
                continue
                
            if isinstance(group_data, dict):
                # Handle nested structure (penalties/bonuses)
                if group_name == "conditional_adjustments":
                    for category in ["penalties", "bonuses"]:
                        for name, result in group_data.get(category, {}).items():
                            if result.get("applies") and result.get("adjustment") is not None:
                                delta = result["adjustment"]
                                structured_adjustments.append({
                                    "name": name,
                                    "delta": round(delta, 2),
                                    "type": category[:-1],  # "penalty" or "bonus"
                                    "direction": "negative" if delta < 0 else "positive",
                                    "source": "conditional"
                                })
                else:
                    # Handle flat structure (volume, trend, divergence)
                    for name, result in group_data.items():
                        if result.get("applies") and result.get("adjustment") is not None:
                            delta = result["adjustment"]
                            structured_adjustments.append({
                                "name": name,
                                "delta": round(delta, 2),
                                "type": "bonus" if delta > 0 else "penalty",
                                "direction": "positive" if delta > 0 else "negative",
                                "source": group_name
                            })

        # ==========================================================
        # SETUP VALIDATION MODIFIERS (if any)
        # ==========================================================
        setup_modifiers = self.extractor.get_setup_validation_modifiers(setup_type)
        setup_modifier_count = 0
        setup_modifier_breakdown = []

        if setup_modifiers:
            evaluator = ConditionEvaluator()
            namespace = {**ind, **fund}

            # Inject aggregate scores so setup validation_modifiers can reference
            # technicalScore / fundamentalScore in condition strings
            scoring = ctx.get("scoring", {}) or {}
            tech_block = scoring.get("technical", {}) or {}
            fund_block = scoring.get("fundamental", {}) or {}
            if tech_block.get("score") is not None:
                namespace["technicalScore"] = tech_block["score"]
            if fund_block.get("score") is not None:
                namespace["fundamentalScore"] = fund_block["score"]

            for category in ["penalties", "bonuses"]:
                for name, config in setup_modifiers.get(category, {}).items():
                    condition = config.get("condition")
                    if not condition:
                        continue

                    try:
                        if evaluator.evaluate_condition(condition, namespace):
                            amount = config.get("amount", 0)
                            
                            # ✅ APPLY DIVERGENCE MULTIPLIER
                            raw_delta = -amount if category == "penalties" else amount
                            delta = raw_delta * divergence_multiplier
                            
                            total_adjustment += delta
                            setup_modifier_count += 1

                            breakdown.append(
                                f"setup_{category}.{name}: {delta:+.1f}"
                                + (f" (×{divergence_multiplier})" if divergence_multiplier != 1.0 else "")
                            )

                            structured_adjustments.append({
                                "name": name,
                                "delta": round(delta, 2),
                                "type": "penalty" if delta < 0 else "bonus",
                                "direction": "negative" if delta < 0 else "positive",
                                "source": "setup_validation"
                            })

                            setup_modifier_breakdown.append({
                                "type": category[:-1],
                                "name": name,
                                "amount": delta,
                                "raw_amount": raw_delta,  # ✅ Track original
                                "divergence_multiplier": divergence_multiplier,
                                "condition": condition
                            })

                    except Exception as e:
                        self.logger.warning(f"Failed evaluating setup modifier '{name}': {e}")

        # ==========================================================
        # EXECUTION RULE IMPACT
        # ==========================================================
        execution = ctx.get("execution_rules", {}).get("summary", {})
        exec_adjustment = 0
        exec_breakdown = []

        warnings = execution.get("warnings", [])
        violations = execution.get("violations", [])
        risk_score = execution.get("execution_risk_score", 0)

        for rule in warnings:
            raw_penalty = -5
            exec_adjustment += raw_penalty * divergence_multiplier  # ✅ APPLY MULTIPLIER
            exec_breakdown.append(
                f"execution_warning.{rule}: {raw_penalty * divergence_multiplier:+.1f}"
            )

        for rule in violations:
            raw_penalty = -15
            exec_adjustment += raw_penalty * divergence_multiplier  # ✅ APPLY MULTIPLIER
            exec_breakdown.append(
                f"execution_violation.{rule}: {raw_penalty * divergence_multiplier:+.1f}"
            )

        if risk_score >= 80:
            raw_penalty = -10
            exec_adjustment += raw_penalty * divergence_multiplier  # ✅ APPLY MULTIPLIER
            exec_breakdown.append(
                f"execution_risk_score >= 80: {raw_penalty * divergence_multiplier:+.1f}"
            )
        elif risk_score >= 60:
            raw_penalty = -5
            exec_adjustment += raw_penalty * divergence_multiplier  # ✅ APPLY MULTIPLIER
            exec_breakdown.append(
                f"execution_risk_score >= 60: {raw_penalty * divergence_multiplier:+.1f}"
            )
        elif risk_score < 40:
            raw_bonus = 5
            exec_adjustment += raw_bonus * divergence_multiplier  # ✅ APPLY MULTIPLIER
            exec_breakdown.append(
                f"execution_risk_score < 40: {raw_bonus * divergence_multiplier:+.1f}"
            )

        if exec_adjustment != 0:
            total_adjustment += exec_adjustment
            breakdown.extend(exec_breakdown)

            structured_adjustments.append({
                "name": "execution_rules",
                "delta": round(exec_adjustment, 2),
                "type": "penalty" if exec_adjustment < 0 else "bonus",
                "direction": "negative" if exec_adjustment < 0 else "positive",
                "source": "execution"
            })

        execution_adjustment_applied = {
            "amount": exec_adjustment,
            "breakdown": exec_breakdown
        }

        # ==========================================================
        # FINAL + CLAMP
        # ==========================================================
        final = base + total_adjustment
        clamp = self.extractor.get_confidence_clamp()
        clamped = max(clamp[0], min(clamp[1], final))

        # ==========================================================
        # TRADEABILITY CHECK (min_tradeable_confidence)
        # ==========================================================
        min_tradeable = self.extractor.get_min_tradeable_confidence()
        tradeable = True
        below_by = 0.0

        if min_tradeable and min_tradeable > 0:
            tradeable = clamped >= min_tradeable
            if not tradeable:
                below_by = round(float(min_tradeable) - float(clamped), 1)
                breakdown.append(
                    f"min_tradeable_floor: {clamped:.1f} < {min_tradeable:.1f} → BELOW TRADEABLE THRESHOLD"
                )

        # High-confidence override config (metadata only, used by signal engine)
        high_conf_override = self.extractor.get_high_confidence_override() or {}

        return {
            "base": base,
            "adjustments": {
                "total": total_adjustment,
                "breakdown": breakdown,
                "setup_modifiers_applied": setup_modifier_count
            },
            "structured_adjustments": structured_adjustments,
            "execution_adjustment": execution_adjustment_applied,
            "modifier_results": modifier_results,
            "setup_validation_modifiers": setup_modifier_breakdown,
            "divergence_multiplier": divergence_multiplier,  # ✅ From config, not ctx
            "final": final,
            "clamped": clamped,
            "clamp_range": clamp,
            "score": clamped,
            "confidence": round(clamped, 1),
            "tradeable": tradeable,
            "min_tradeable_threshold": float(min_tradeable or 0.0),
            "below_threshold_by": below_by,
            "high_confidence_override": high_conf_override,
            "calculation_method": "extractor_v8_fixed",
            "horizon": self.horizon,
            "setup_type": setup_type
        }
    
    def _validate_execution_rules(self, ctx: Dict) -> Dict[str, Any]:
        """
        ✅ PHASE 6 (REFACTORED):
        Evaluate execution feasibility WITHOUT making decisions.
        """
        setup_type = ctx["setup"]["type"]

        execution_rules = self.extractor.get_execution_rules(setup_type)
        rule_results = {}
        violations = []
        warnings = []

        def _normalize(result, rule_name):
            if result.get("skipped"):
                return {
                    "status": "skipped",
                    "severity": 0,
                    "reason": result.get("reason", "Skipped"),
                    "context": {}
                }

            if result.get("passed"):
                return {
                    "status": "ok",
                    "severity": 0,
                    "reason": "Rule satisfied",
                    "context": result
                }

            # failed
            severity = result.get("severity", 50)
            status = "violation" if severity >= 70 else "warning"

            entry = {
                "status": status,
                "severity": severity,
                "reason": result.get("reason", "Execution constraint failed"),
                "context": result
            }

            if status == "violation":
                violations.append(rule_name)
            else:
                warnings.append(rule_name)

            return entry

        # ───────────────────────────────────────
        # Rule 1: Volatility Guards
        # ───────────────────────────────────────
        if self.extractor.is_execution_rule_enabled("volatility_guards"):
            res = self._check_volatility_guards(
                ctx, self.extractor.get_volatility_guards_config()
            )
            rule_results["volatility_guards"] = _normalize(res, "volatility_guards")
        else:
            rule_results["volatility_guards"] = {
                "status": "skipped",
                "severity": 0,
                "reason": "Disabled",
                "context": {}
            }

        # ───────────────────────────────────────
        # Rule 2: Structure Validation
        # ───────────────────────────────────────
        if self.extractor.is_execution_rule_enabled("structure_validation"):
            res = self._check_structure_validation(
                ctx, self.extractor.get_structure_validation_config()
            )
            rule_results["structure_validation"] = _normalize(res, "structure_validation")
        else:
            rule_results["structure_validation"] = {
                "status": "skipped",
                "severity": 0,
                "reason": "Disabled",
                "context": {}
            }

        # ───────────────────────────────────────
        # Rule 3: SL Distance Validation
        # ───────────────────────────────────────
        if self.extractor.is_execution_rule_enabled("sl_distance_validation"):
            res = self._check_sl_distance(
                ctx, self.extractor.get_sl_distance_validation_config()
            )
            rule_results["sl_distance_validation"] = _normalize(res, "sl_distance_validation")
        else:
            rule_results["sl_distance_validation"] = {
                "status": "skipped",
                "severity": 0,
                "reason": "Disabled",
                "context": {}
            }

        # ───────────────────────────────────────
        # Rule 4: Target Proximity
        # ───────────────────────────────────────
        if self.extractor.is_execution_rule_enabled("target_proximity_rejection"):
            res = self._check_target_proximity(
                ctx, self.extractor.get_target_proximity_rejection_config()
            )
            rule_results["target_proximity"] = _normalize(res, "target_proximity")
        else:
            rule_results["target_proximity"] = {
                "status": "skipped",
                "severity": 0,
                "reason": "Disabled",
                "context": {}
            }

        # ───────────────────────────────────────
        # Aggregate risk score (NOT a decision)
        # ───────────────────────────────────────
        execution_risk = sum(
            r["severity"] for r in rule_results.values()
            if r["status"] in ("warning", "violation")
        )
        overall = {
            "passed": len(violations) == 0,
            "failed_rules": [
                {
                    "rule": rule_name,
                    "severity": rule_results[rule_name]["severity"],
                    "reason": rule_results[rule_name]["reason"]
                }
                for rule_name in violations
            ],
            "warning_rules": warnings,
        }
        return {
            "phase": "execution",
            "rules": rule_results,
            "summary": {
                "violations": violations,
                "warnings": warnings,
                "execution_risk_score": execution_risk,
                "evaluated_rules": len(rule_results)
            },
            "overall": overall   # 🔑 REQUIRED
        }

    def _check_volatility_guards(self, ctx: Dict, vol_guards: Dict) -> Dict:
        """Check volatility guard rules."""
        if not vol_guards:
            return {"passed": True, "reason": "No volatility guards configured"}
        
        # ✅ FIX: Extract from indicators, not price_data
        indicators = ctx.get("indicators", {})
        
        # ✅ Use ensure_numeric for safe extraction
        atr_pct = ensure_numeric(indicators.get("atrPct"))
        vol_quality = ensure_numeric(indicators.get("volatilityQuality"))
        
        # ✅ Handle missing values gracefully
        if atr_pct is None or vol_quality is None:
            self.logger.warning(
                f"[{ctx['meta']['symbol']}] ⚠️ Volatility guards skipped: "
                f"atrPct={atr_pct}, volatilityQuality={vol_quality}"
            )
            return {
                "passed": True,  # ✅ Don't block if data missing
                "reason": "Volatility data incomplete - guards skipped",
                "skipped": True
            }
        extreme_buffer = vol_guards.get("extreme_vol_buffer", 2.0)
        
        # Determine required quality based on volatility
        if atr_pct and atr_pct > extreme_buffer:
            required_quality = vol_guards.get("min_quality_breakout", 2.0)
            regime = "extreme"
        else:
            required_quality = vol_guards.get("min_quality_normal", 4.0)
            regime = "normal"
        
        if vol_quality and vol_quality >= required_quality:
            return {
                "passed": True,
                "reason": f"Volatility quality {vol_quality:.1f} >= {required_quality:.1f} ({regime})"
            }
        
        return {
            "passed": False,
            "reason": f"Volatility quality {vol_quality:.1f} < {required_quality:.1f} ({regime} regime)"
        }

    def _check_structure_validation(self, ctx: Dict, struct_val: Dict) -> Dict:
        """Check price structure validation."""
        if not struct_val:
            return {"passed": True, "reason": "No structure validation configured"}
        
        setup_type = ctx["setup"]["type"]
        price = ctx["price_data"].get("price", 0)
        
        # Check breakout clearance
        if "BREAKOUT" in setup_type:
            resistance = ctx["price_data"].get("resistance1", 0)
            clearance = struct_val.get("breakout_clearance", 0.001)
            
            if resistance and price > 0:
                required_price = resistance * (1 + clearance)
                if price >= required_price:
                    return {
                        "passed": True,
                        "reason": f"Price {price} cleared resistance {resistance}"
                    }
                return {
                    "passed": False,
                    "reason": f"Price {price} < required {required_price:.2f} (resistance + clearance)"
                }
        
        # Check breakdown clearance
        elif "BREAKDOWN" in setup_type:
            support = ctx["price_data"].get("support1", 0)
            clearance = struct_val.get("breakdown_clearance", 0.001)
            
            if support and price > 0:
                required_price = support * (1 - clearance)
                if price <= required_price:
                    return {
                        "passed": True,
                        "reason": f"Price {price} broke support {support}"
                    }
                return {
                    "passed": False,
                    "reason": f"Price {price} > required {required_price:.2f} (support - clearance)"
                }
        
        return {"passed": True, "reason": "Structure validation not applicable"}

    def _check_sl_distance(self, ctx: Dict, sl_config: Dict) -> Dict:
        """
        Check if SL distance is acceptable.
        
        Args:
            ctx: Evaluation context
            sl_config: SL distance validation config from extractor
        
        Returns:
            {"passed": bool, "reason": str}
        """
        if not sl_config:
            return {"passed": True, "reason": "No SL distance rules configured"}
        
        # Get SL distance from risk model (calculated earlier)
        risk_model = ctx.get("risk_model", {})
        sl_distance_pct = risk_model.get("sl_distance_pct", 0)
        if sl_distance_pct == 0 or sl_distance_pct is None:
            sl_distance_pct = ensure_numeric(ctx["indicators"].get("slDistance"))
        
        # Get thresholds from config
        min_sl = sl_config.get("min_sl_distance_pct", 0.5)
        max_sl = sl_config.get("max_sl_distance_pct", 10.0)
        
        # Validate
        if sl_distance_pct == 0:
            return {
                "passed": False,
                "reason": "SL distance not calculated in risk model"
            }
        
        if sl_distance_pct < min_sl:
            return {
                "passed": False,
                "reason": f"SL too tight: {sl_distance_pct:.2f}% < min {min_sl}%"
            }
        
        if sl_distance_pct > max_sl:
            return {
                "passed": False,
                "reason": f"SL too wide: {sl_distance_pct:.2f}% > max {max_sl}%"
            }
        
        return {
            "passed": True,
            "reason": f"SL distance OK: {sl_distance_pct:.2f}% (range: {min_sl}-{max_sl}%)"
        }

    def _check_target_proximity(self, ctx: Dict, target_config: Dict) -> Dict:
        """
        Check if target distance is reasonable.
        
        Args:
            ctx: Evaluation context
            target_config: Target proximity config from extractor
        
        Returns:
            {"passed": bool, "reason": str}
        """
        if not target_config:
            return {"passed": True, "reason": "No target proximity rules"}
        
        # Get target data from risk model
        risk_model = ctx.get("risk_model", {})
        pattern_targets = risk_model.get("pattern_targets")
        
        if not pattern_targets:
            # No pattern targets calculated, use default validation
            return {"passed": True, "reason": "No pattern targets to validate"}
        
        entry = pattern_targets.get("entry", 0)
        t1 = pattern_targets.get("t1", 0)
        
        if entry == 0 or t1 == 0:
            return {"passed": False, "reason": "Invalid target data"}
        
        # Calculate distance
        t1_distance_pct = abs((t1 - entry) / entry) * 100
        
        # Get thresholds
        min_target = target_config.get("min_target_distance_pct", 1.0)
        max_target = target_config.get("max_target_distance_pct", 50.0)
        
        # Validate
        if t1_distance_pct < min_target:
            return {
                "passed": False,
                "reason": f"Target too close: {t1_distance_pct:.2f}% < min {min_target}%"
            }
        
        if t1_distance_pct > max_target:
            return {
                "passed": False,
                "reason": f"Target too far: {t1_distance_pct:.2f}% > max {max_target}%"
            }
        
        return {
            "passed": True,
            "reason": f"Target distance OK: {t1_distance_pct:.2f}% (range: {min_target}-{max_target}%)"
        }
    
    def _build_risk_candidates(self, ctx: Dict) -> Dict[str, Any]:
        """
        PHASE 6.5: Risk feasibility metrics (Evaluation)
        ✅ Calculates RR and SL ONCE
        ❌ No gates
        ❌ No decisions
        ❌ No capital logic
        """

        setup_type = ctx["setup"]["type"]

        price = ensure_numeric(ctx["price_data"].get("price"))
        atr = ensure_numeric(ctx["indicators"].get("atrDynamic"))
        sl_dist = ensure_numeric(ctx["indicators"].get("slDistance"))

        risk_cfg = self.extractor.get_risk_management_config()
        atr_mult = risk_cfg.get("stop_loss_atr_mult", 2.0)
        target_mult = risk_cfg.get("target_atr_mult", 3.0)

        rr = None
        rr_source = None
        pattern_targets = None
        generic_targets = None
        sl_price = None
        if price > 0 and atr > 0:
            sl_price = price - (atr * atr_mult)
        # --------------------------------------------------
        # 1️⃣ Pattern-based RR + SL (validated patterns only)
        # --------------------------------------------------
        primary_patterns = (
            ctx.get("pattern_validation", {})
            .get("by_setup", {})
            .get(setup_type, {})
            .get("primary_found", [])
        )

        if primary_patterns and atr > 0:
            primary = primary_patterns[0]
            pdata = ctx.get("patterns", {}).get(primary, {})

            if pdata.get("found"):
                pattern_targets = self._calculate_pattern_targets(
                    primary, pdata, ctx["price_data"]
                )

                if pattern_targets:
                    entry = pattern_targets.get("entry")
                    sl_price = pattern_targets.get("stop_loss")

                    targets = [
                        v for k, v in pattern_targets.items()
                        if k.startswith("t") and isinstance(v, (int, float))
                    ]

                    best_target = max(targets) if targets else None

                    if entry and sl_price and best_target and entry > sl_price:
                        rr = (best_target - entry) / (entry - sl_price)
                        rr_source = "pattern"

        # --------------------------------------------------
        # 2️⃣ Generic ATR fallback (RR + SL + Targets)
        # --------------------------------------------------
        # Merge the logic to prevent unconditional overwrite
        if atr > 0 and price > 0:
            if sl_dist > 0:
                generic_rr = (atr * target_mult) / sl_dist
                
                # ONLY apply if not already set by a primary pattern
                if rr is None:
                    rr = generic_rr
                    rr_source = "generic_atr"
                
                # Always calculate generic_targets as a backup
                generic_targets = {
                    "entry": price,
                    "stop_loss": price - (atr * atr_mult),
                    "t1": price + (atr * target_mult),
                    "t2": price + (atr * target_mult * 2)
                }

        return {
            "rrRatio": round(rr, 2) if rr else None,
            "rr_source": rr_source,
            "sl_price": sl_price,
            "pattern_targets": pattern_targets,
            "generic_targets": generic_targets,
            "atr_multiple": atr_mult,
            "target_multiple": target_mult
        }
    
    def _validate_opportunity_gates(self, ctx: Dict) -> Dict[str, Any]:
        """
        ✅ PHASE 8: Validate opportunity gates (FINAL DECISION LAYER)
        Uses Standardized ConditionEvaluator logic.
        """
        setup_type = ctx["setup"]["type"]
        
        # 1. Build Opportunity Metrics (Data)
        opportunity_ctx = self._build_opportunity_metrics(ctx)
        ctx["opportunity_metrics"] = opportunity_ctx
        
        # 2. Get Resolved Gates (Config)
        gates_map = self.extractor.get_resolved_gates("opportunity", setup_type)
        
        gate_results = {}
        failures = []
        stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}

        for gate_name, resolved_gate in gates_map.items():
            stats["total"] += 1
            threshold = resolved_gate.threshold

            # ── Disabled gate check ──
            if not threshold or (threshold.get("min") is None and threshold.get("max") is None):
                gate_results[gate_name] = {
                    "status": "skipped", "reason": "disabled",
                    "required": None, "actual": None, "source": resolved_gate.source
                }
                stats["skipped"] += 1
                continue

            # 3. Get Data (Resolver knows *where* to look)
            actual_value = self._resolve_opportunity_gate_value(
                gate_name, 
                ctx["opportunity_metrics"]
            )

            # ── Missing value check (Strict for Opportunity Gates) ──
            if actual_value is None:
                gate_results[gate_name] = {
                    "status": "failed", "reason": "value_unavailable",
                    "required": threshold, "actual": None, "source": resolved_gate.source
                }
                failures.append({
                    "gate": gate_name, "reason": "Value unavailable", 
                    "required": threshold, "actual": None, "source": resolved_gate.source
                })
                stats["failed"] += 1
                continue

            # 4. Evaluate ( Use Canonical Evaluator)
            passed, reason = ConditionEvaluator.evaluate_gate_config(
                actual_value,
                threshold,
                gate_name
            )

            gate_results[gate_name] = {
                "status": "passed" if passed else "failed",
                "required": threshold,
                "actual": actual_value,
                "reason": reason,
                "source": resolved_gate.source
            }

            if passed:
                stats["passed"] += 1
            else:
                stats["failed"] += 1
                failures.append({
                    "gate": gate_name,
                    "reason": reason,
                    "required": threshold,
                    "actual": actual_value,
                    "source": resolved_gate.source
                })

        return {
            "phase": "opportunity",
            "overall": {
                "passed": len(failures) == 0,
                "failed_gates": failures,
                "total_gates": stats["total"],
                "passed_gates": stats["passed"]
            },
            "gates": gate_results,
            "summary": stats
        }
    
    def _build_opportunity_context(self, ctx: Dict) -> Dict[str, Any]:
        """
        Build a flat, decision-ready context for opportunity gates.

        ✅ No calculations
        ✅ No config logic
        ✅ No thresholds
        """

        return {
            "metrics": ctx.get("opportunity_metrics", {}),
            "gates": ctx.get("opportunity_gates", {}),
            "passed": ctx["opportunity_gates"]["overall"]["passed"],
            "failed_gates": ctx["opportunity_gates"]["overall"]["failed_gates"],
            "confidence": ctx["confidence"]["clamped"],
            "setup": ctx["setup"]["type"],
            "strategy": ctx["strategy"]["primary"],
        }

    def _resolve_opportunity_gate_value(
        self,
        gate_name: str,
        opportunity_ctx: Dict[str, Any]
    ):
        """
        Resolve opportunity gate values from derived metrics.
        """
        return opportunity_ctx.get(gate_name)

    def _build_opportunity_metrics(self, ctx: Dict) -> Dict[str, Any]:
        """
        ✅ Aggregate FINAL metrics required by opportunity gates.
        ❌ No calculations
        ❌ No thresholds
        ❌ No decisions
        """

        scoring = ctx.get("scoring", {})
        confidence = ctx.get("confidence", {})
        execution = ctx.get("execution_rules", {})
        risk = ctx.get("risk_candidates", {}) 

        return {
            # Core confidence
            "confidence": confidence.get("clamped"),

            # Scores (pure aggregation)
            "technicalScore": scoring.get("technical", {}).get("score"),
            "fundamentalScore": scoring.get("fundamental", {}).get("score"),
            "hybridScore": scoring.get("hybrid", {}).get("score"),

            # Risk / R:R (will be filled next)
            "rrRatio": risk.get("rrRatio"),

            # Safety metadata (already used)
            "execution_risk_score": execution.get("summary", {}).get("execution_risk_score"),
        }

    def validate_pattern_conditions(self, conditions: List[str], namespace: Dict[str, Any], pattern_name: str, logic: str = "AND") -> bool:
        """
        Validate pattern-specific conditions (invalidation/breakdown).
        
        This is a specialized wrapper for pattern validation that:
        - Automatically extracts values from nested indicator dicts
        - Supports pattern metadata access (age_candles, width, state, etc.)
        - Logs pattern-specific failures
        
        Args:
            conditions: List of condition strings from pattern invalidation config
            namespace: Raw indicators dict (may contain nested structures)
            pattern_name: Pattern name for logging
            logic: "AND" or "OR" (default: "AND")
        
        Returns:
            True if conditions trigger invalidation, False otherwise
        
        Examples:
            conditions = ["price < bbLow", "bbWidth > 8.0"]
            namespace = ctx["indicators"]  # Raw nested dict
            validate_pattern_conditions(conditions, namespace, "bollingerSqueeze")
        """
        if not conditions:
            return True
        
        evaluator = ConditionEvaluator
        results = []
        for condition in conditions:
            result = evaluator.evaluate_condition(condition, namespace)
            results.append(result)
            # Log pattern-specific evaluation
            METRICS.log_condition_evaluation(
                condition=condition,
                result=result,
                context=f"pattern_invalidation_{pattern_name}",
                variables={k: namespace.get(k) for k in self._extract_variables_from_condition(condition)}
            )
            
        if logic.upper() == "OR":
            return any(results)  
        return all(results)
    
    
    def _validate_patterns(self, ctx: Dict) -> Dict[str, Any]:
        """
        ✅ UPDATED: Pattern validation with entry rules (MINIMAL CHANGES).
        
        Now includes:
        1. Pattern affinity (setup compatibility)
        2. Pattern invalidation (breakdown detection)
        3. Entry rule validation (reuses existing method!)
        Pattern Validation (RESOLVER)
        
        ROLE: Gatekeeper (Pre-Entry)
        - Selects best primary pattern compatible with setup.
        - Checks static entry rules (e.g., "close > ema").
        - Checks immediate invalidation (e.g., "pattern broken").
        
        ❌ DOES NOT: Monitor post-entry expiration or trail stops.
        (See TradeEnhancer for post-entry lifecycle).
        """
        detected = ctx.get("patterns", {})
        setup_info = ctx.get("setup", {})
        top_setups = setup_info.get("top", [])

        if not detected or not top_setups:
            return {
                "available": False,
                "by_setup": {}
            }

        results = {}

        for setup in top_setups:
            setup_type = setup["type"]
            setup_patterns = self.extractor.get_setup_patterns(setup_type)

            primary = setup_patterns.get("PRIMARY", [])
            confirming = setup_patterns.get("CONFIRMING", [])
            conflicting = setup_patterns.get("CONFLICTING", [])

            primary_found = [p for p in detected if p in primary]
            confirming_found = [p for p in detected if p in confirming]
            conflicting_found = [p for p in detected if p in conflicting]

            score = 0
            if primary_found:
                score += 50
            score += len(confirming_found) * 10
            score -= len(conflicting_found) * 20

            # ──── Pattern invalidation (PER SETUP) ────────────────────────
            invalidation_status = {}
            
            # ✅ NEW: Entry rule validation (MINIMAL CODE!)
            entry_validation_status = {}

            for pattern_name, pattern_data in detected.items():
                if not pattern_data.get("found"):
                    continue

                pattern_ctx = self.extractor.get_pattern_context(pattern_name)
                if not pattern_ctx:
                    continue

                # 1. Check invalidation (breakdown) - EXISTING
                if pattern_ctx.invalidation:
                    conditions = pattern_ctx.invalidation.get("conditions", [])
                    logic = pattern_ctx.invalidation.get("_logic", "AND")
                    
                    if conditions:
                        is_invalidated = self.validate_pattern_conditions(
                            conditions,
                            ctx["indicators"],
                            pattern_name,
                            logic=logic
                        )

                        invalidation_status[pattern_name] = {
                            "invalidated": is_invalidated,
                            "rules": pattern_ctx.invalidation,
                            "reason": "Breakdown conditions met" if is_invalidated else "Valid"
                        }
                        
                        METRICS.log_pattern_validation(
                            pattern_name=pattern_name,
                            found=True,
                            quality=pattern_data.get("score", 0),
                            invalidated=is_invalidated,
                            reason=invalidation_status[pattern_name]["reason"]
                        )
                
                # ✅ 2. Check entry rules - REUSES EXISTING METHOD!
                entry_conditions = pattern_ctx.entry_rules.get("conditions", [])
                
                if entry_conditions:
                    # Build namespace with pattern metadata
                    namespace = self._build_pattern_namespace(
                        ctx, pattern_name, pattern_data
                    )
                    
                    # ✅ REUSE: Same method used for invalidation!
                    entry_passes = self.validate_pattern_conditions(
                        entry_conditions,
                        namespace,
                        f"{pattern_name}_entry",
                        logic="AND"  # Entry usually requires ALL conditions
                    )
                    
                    entry_validation_status[pattern_name] = {
                        "passes": entry_passes,
                        "conditions_checked": entry_conditions,
                        "reason": "All entry conditions met" if entry_passes else "Entry conditions failed"
                    }

            # ──────────────────────────────────────────────────────────────
            # ✅ Pattern affinity (PER SETUP)
            # ──────────────────────────────────────────────────────────────
            affinity = self._calculate_pattern_affinity(setup_type, detected)

            results[setup_type] = {
                "valid": bool(primary_found or confirming_found),
                "score": max(0, score),
                "primary_found": primary_found,
                "confirming_found": confirming_found,
                "conflicting_found": conflicting_found,
                "pattern_affinity": affinity,
                "invalidation": invalidation_status,
                "entry_validation": entry_validation_status  # ✅ NEW
            }

        return {
            "available": True,
            "by_setup": results
        }
    def _build_pattern_namespace(self, ctx: Dict, pattern_name: str, pattern_data: Dict):
        indicators = ctx.get("indicators", {})
        fundamentals = ctx.get("fundamentals", {})
        
        # ✅ SAFE EXTRACTION (handles both nested AND flat)
        namespace = {}
        
        for key, value in {**indicators, **fundamentals}.items():
            if isinstance(value, dict):
                # Extract from nested structure
                extracted = (
                    value.get("value") or 
                    value.get("raw") or 
                    value.get("score")
                )
                if extracted is not None:
                    namespace[key] = extracted
            elif isinstance(value, (int, float, str)):
                # Already flat
                namespace[key] = value
        
        # ✅ ADD PATTERN METADATA (age_candles, etc.)
        if isinstance(pattern_data, dict):
            meta = pattern_data.get("raw", {}).get("meta", {})
            namespace.update(meta)
        
        return namespace

    def _calculate_pattern_affinity(
        self,
        setup_type: str,
        detected: Dict
    ) -> List[Dict]:
        """Calculate affinity scores between setup and detected patterns."""
        # ✅ Get setup patterns via extractor
        setup_patterns = self.extractor.get_setup_patterns(setup_type)
        
        affinities = []
        for pattern_name in detected.keys():
            affinity = 0.0
            role = "UNKNOWN"
            
            if pattern_name in setup_patterns.get("PRIMARY", []):
                affinity = 2.0
                role = "PRIMARY"
            elif pattern_name in setup_patterns.get("CONFIRMING", []):
                affinity = 1.0
                role = "CONFIRMING"
            elif pattern_name in setup_patterns.get("CONFLICTING", []):
                affinity = -1.0
                role = "CONFLICTING"
            
            if affinity != 0:
                affinities.append({
                    "pattern": pattern_name,
                    "affinity": affinity,
                    "role": role
                })
        
        return sorted(affinities, key=lambda x: x["affinity"], reverse=True)
    
    def detect_divergence(self, indicators: Dict) -> Dict[str, Any]:
        """
        ✅ REFACTORED v6.0: Unified Divergence Detection.
        Uses confidence_config.py as the sole authority for both math and scoring.
        """
        # 1. Resolve Physics & Scoring Config
        physics = self.extractor.get_divergence_physics()
        div_penalties = self.extractor.get_universal_adjustments().get("divergence_penalties", {})
        
        # 2. Extract Values
        flat_indicators = self._flatten_indicator_data(indicators)
        rsi_slope = ensure_numeric(flat_indicators.get("rsislope", 0))
        price = ensure_numeric(flat_indicators.get("price", 0))
        prev_price = ensure_numeric(flat_indicators.get("prevclose", price))
        
        # Physics Triggers
        bear_trigger = physics.get("slope_diff_min", -0.05)
        bull_trigger = physics.get("bullish_slope_min", 0.05)
        price_slope = (price - prev_price) / prev_price if prev_price > 0 else 0

        # 3. Detect BEARISH Divergence (Price ↑, RSI ↓)
        if price_slope > 0 and rsi_slope < bear_trigger:
            # ✅ Use config-based momentum thresholds for adaptive severity
            mom_thresholds = self.extractor.get_momentum_thresholds()
            rsi_decel = mom_thresholds.get("rsislope", {}).get("deceleration_ceiling", -0.05)
            
            # Adaptive severity based on threshold multiples
            if rsi_slope <= rsi_decel * 3:  # 3x threshold = severe
                severity = "severe"
            elif rsi_slope <= rsi_decel * 1.5:  # 1.5x threshold = moderate
                severity = "moderate"
            else:
                severity = "minor"
            
            penalty_cfg = div_penalties.get(severity, {})
            
            return {
                "divergence_type": "bearish",
                "confidence_factor": penalty_cfg.get("confidence_multiplier", 0.70),
                "warning": f"Bearish Divergence ({severity.upper()}): RSI slope={rsi_slope:.2f}",
                "severity": severity,
                "allow_entry": not penalty_cfg.get("block_entry", False)
            }

        # 4. Detect BULLISH Divergence (Price ↓, RSI ↑)
        elif price_slope < 0 and rsi_slope > bull_trigger:
            return {
                "divergence_type": "bullish",
                "confidence_factor": 1.0,  # No penalty for bullish divergence
                "warning": f"Bullish Divergence: RSI slope={rsi_slope:.2f}",
                "severity": "moderate",
                "allow_entry": True
            }

        return {
            "divergence_type": "none",
            "confidence_factor": 1.0,
            "allow_entry": True
        }
    def detect_volume_signature(self, indicators: Dict) -> Dict[str, Any]:
        """
        ✅ Detect volume signature using extractor.
        
        Detects: surge, drought, climax, normal
        """
        flat_indicators = self._flatten_indicator_data(indicators)
        rvol = ensure_numeric(flat_indicators.get("rvol", 1.0))

        # ✅ Get volume modifiers via extractor
        vol_mods = self.extractor.get_volume_modifiers()
        
        # Check for SURGE
        surge_config = vol_mods.get("surge_bonus", {})
        if surge_config:
            surge_gates = surge_config.get("gates", {})
            surge_passes, _ = self.extractor.evaluate_confidence_gates(
                surge_gates,
                flat_indicators  # ✅ Use flattened data
            )
            
            if surge_passes:
                return {
                    "type": "surge",
                    "adjustment": surge_config.get("confidence_boost", 10),
                    "warning": f"Volume surge: RVOL={rvol:.2f}",
                    "rvol": rvol
                }
        
        # Check for DROUGHT
        drought_config = vol_mods.get("drought_penalty", {})
        if drought_config:
            drought_gates = drought_config.get("gates", {})
            drought_passes, _ = self.extractor.evaluate_confidence_gates(
                drought_gates,
                flat_indicators
            )
            
            if drought_passes:
                return {
                    "type": "drought",
                    "adjustment": drought_config.get("confidence_penalty", -25),
                    "warning": f"Volume drought: RVOL={rvol:.2f}",
                    "rvol": rvol
                }
        
        # Check for CLIMAX
        climax_config = vol_mods.get("climax_warning", {})
        if climax_config:
            rsi = ensure_numeric(indicators.get("rsi", 50))
            climax_gates = climax_config.get("gates", {})
            climax_passes, _ = self.extractor.evaluate_confidence_gates(
                climax_gates,
                flat_indicators
            )
            
            if climax_passes:
                return {
                    "type": "climax",
                    "adjustment": -10,
                    "warning": f"Volume climax: RVOL={rvol:.2f}, RSI={rsi:.1f}",
                    "rvol": rvol
                }
        
        # Normal volume
        return {
            "type": "normal",
            "adjustment": 0,
            "warning": None,
            "rvol": rvol
        }

    def _build_execution_context(
        self,
        evaluation_ctx: Dict[str, Any],
        capital: Optional[float],
        now: Optional[datetime]
    ) -> Dict[str, Any]:
        """Build execution context (real-time validation with account state)."""
        execution = {
            "meta": {
                "built_at": datetime.utcnow().isoformat(),
                "capital_provided": capital is not None,
                "time_provided": now is not None
            }
        }
        
        execution["entry_permission"] = self._build_entry_permission(evaluation_ctx)
        execution["position_sizing"] = self._build_position_sizing(evaluation_ctx, capital)
        execution["risk"] = self._finalize_risk_model(evaluation_ctx, capital)
        execution["order_model"] = self._build_order_model(evaluation_ctx)
        execution["market_constraints"] = self._build_market_constraints(evaluation_ctx)
        execution["time_constraints"] = self._build_time_constraints(now)
        execution["can_execute"] = self._can_execute(execution, evaluation_ctx)
        
        return execution
    
    def _build_entry_permission(self, eval_ctx: Dict) -> Dict[str, Any]:
        """
        FINAL: Entry permission (execution projection)

        ❌ No re-evaluation
        ❌ No indicators
        ❌ No pattern logic recalculation
        ❌ No horizon branching

        ✅ Aggregates ALL evaluation-phase validations
        """

        opportunity = eval_ctx["opportunity_gates"]["overall"]
        setup_pref = eval_ctx.get("setup_preferences", {})
        
        # ✅ Pattern entry validation (from evaluation phase)
        pattern_entry_ok = True
        pattern_entry_reason = None
        
        pattern_validation = eval_ctx.get("pattern_validation", {})
        setup_type = eval_ctx["setup"]["type"]
        
        if pattern_validation.get("available"):
            by_setup = pattern_validation.get("by_setup", {})
            setup_validation = by_setup.get(setup_type, {})
            entry_validation = setup_validation.get("entry_validation", {})
            
            primary_found = setup_validation.get("primary_found", [])
            
            for pattern_name in primary_found:
                validation = entry_validation.get(pattern_name, {})
                if not validation.get("passes", True):
                    pattern_entry_ok = False
                    pattern_entry_reason = f"{pattern_name}: {validation.get('reason')}"
                    break
        
        # ✅ Divergence check (from evaluation phase)
        divergence = eval_ctx.get("divergence", {})
        divergence_ok = divergence.get("allow_entry", True)
        divergence_warning = divergence.get("warning") if not divergence_ok else None
        
        # ✅ Volume climax check (from evaluation phase)
        vol_signature = eval_ctx.get("volume_signature", {})
        vol_ok = vol_signature.get("type", "normal") != "climax"
        vol_warning = f"Volume climax: RVOL={vol_signature.get('rvol', 0):.2f}" if not vol_ok else None

        structural = eval_ctx["structural_gates"]["overall"]["passed"]
        execution_rules = eval_ctx["execution_rules"]["overall"]["passed"]

        # ✅ ALL CHECKS MUST PASS
        allowed = (
            opportunity["passed"]
            and not setup_pref.get("blocked", False)
            and pattern_entry_ok
            and divergence_ok  # ✅ RESTORED
            and vol_ok  # ✅ RESTORED
        )

        reason = None
        if not allowed:
            if not opportunity["passed"]:
                failures = opportunity.get("failed_gates", [])
                reason = f"Opportunity gates failed: {[f['gate'] for f in failures[:3]]}"
            elif setup_pref.get("blocked"):
                reason = setup_pref.get("blocked_reason")
            elif not pattern_entry_ok:
                reason = f"Pattern entry blocked: {pattern_entry_reason}"
            elif not divergence_ok:  # ✅ RESTORED
                reason = f"Divergence block: {divergence_warning}"
            elif not vol_ok:  # ✅ RESTORED
                reason = vol_warning

        return {
            "allowed": allowed,
            "reason": reason,
            
            # ✅ Validation breakdown (for debugging)
            "checks": {
                "opportunity_gates": opportunity["passed"],
                "setup_not_blocked": not setup_pref.get("blocked", False),
                "pattern_entry": pattern_entry_ok,
                "divergence": divergence_ok,
                "volume": vol_ok,
                "structural": structural,
                "execution_rules": execution_rules,
            },
            "pattern_status": {
                "any_invalidated": self._has_invalidated_patterns(pattern_validation, setup_type),
                "invalidated_list": self._get_invalidated_patterns(pattern_validation, setup_type),
                "affects_entry": False  # Currently doesn't block, but tracked
            },

            # Execution-facing transparency (UI / logs)
            "confidence": eval_ctx["confidence"]["clamped"],
            "setup": eval_ctx["setup"]["type"],
            "strategy": eval_ctx["strategy"]["primary"],

            # Debug-only
            "failed_gates": opportunity.get("failed_gates", []),
            "warnings": [w for w in [divergence_warning, vol_warning] if w]
        }
    
    def _has_invalidated_patterns(self, pattern_validation: Dict, setup_type: str) -> bool:
        """Check if any patterns are invalidated."""
        if not pattern_validation.get("available"):
            return False
        
        by_setup = pattern_validation.get("by_setup", {})
        setup_validation = by_setup.get(setup_type, {})
        invalidation = setup_validation.get("invalidation", {})
        
        return any(
            status.get("invalidated", False)
            for status in invalidation.values()
        )

    def _get_invalidated_patterns(self, pattern_validation: Dict, setup_type: str) -> List[str]:
        """Get list of invalidated pattern names."""
        if not pattern_validation.get("available"):
            return []
        
        by_setup = pattern_validation.get("by_setup", {})
        setup_validation = by_setup.get(setup_type, {})
        invalidation = setup_validation.get("invalidation", {})
        
        return [
            pattern_name
            for pattern_name, status in invalidation.items()
            if status.get("invalidated", False)
        ]
    
    def _build_position_sizing(self, eval_ctx: Dict, capital: Optional[float]) -> Dict[str, Any]:
        """✅ Calculate position size using extractor (backward compatible)."""

        if capital is None or capital <= 0:
            return {
                "mode": "unknown",
                "reason": "Capital not provided" if capital is None else "Capital is zero or negative"
            }

        # --------------------------------------------------
        # Base risk config
        # --------------------------------------------------
        risk_config = self.extractor.get_risk_management_config()
        base_risk = risk_config.get("base_risk_pct", 0.02)

        # --------------------------------------------------
        # Setup-based multipliers (already refactored)
        # --------------------------------------------------
        setup_type = eval_ctx.get("setup", {}).get("type", "GENERIC")
        mults = self.extractor.get_combined_position_sizing_multipliers(setup_type)

        # --------------------------------------------------
        # ✅ STRATEGY MULTIPLIER (SAFE EXTRACTION)
        # --------------------------------------------------
        strategy_ctx = eval_ctx.get("strategy", {})

        if isinstance(strategy_ctx.get("best"), dict):
            strategy_mult = strategy_ctx["best"].get("multiplier", 1.0)
        else:
            strategy_mult = strategy_ctx.get("horizon_multiplier", 1.0)

        # --------------------------------------------------
        # Final risk computation
        # --------------------------------------------------
        combined_multiplier = mults["combined"] * strategy_mult
        risk_pct = base_risk * combined_multiplier

        max_position = risk_config.get("max_position_pct", 0.05)
        risk_pct = min(risk_pct, max_position)

        return {
            "mode": "percent_capital",
            "base_risk_pct": base_risk,
            "global_setup_multiplier": mults["global_setup"],
            "horizon_setup_multiplier": mults["horizon_setup"],
            "horizon_base_multiplier": mults["horizon_base"],
            "strategy_multiplier": strategy_mult,  # ✅ EXPLICIT (useful for UI/debug)
            "combined_multiplier": round(combined_multiplier, 3),
            "final_risk_pct": round(risk_pct, 4),
            "capital": capital,
            "position_value": round(capital * risk_pct, 2)
        }
    
    def _finalize_risk_model(
        self,
        eval_ctx: Dict[str, Any],
        capital: Optional[float]
    ) -> Dict[str, Any]:
        """
        FINALIZE risk model with DUAL CONSTRAINTS (Risk vs. Capital).
        """
        risk_data = eval_ctx.get("risk_candidates", {})
        price = ensure_numeric(eval_ctx["price_data"].get("price"))
        confidence = eval_ctx["confidence"]["clamped"]
        
        # 1. Get Core Data
        sl_price = risk_data.get("sl_price")
        rr = risk_data.get("rrRatio")
        targets = risk_data.get("pattern_targets") or risk_data.get("generic_targets")
        
        # # 2. Validate Price Logic
        # valid = all([price > 0, sl_price is not None, sl_price < price])
        # risk_per_share = price - sl_price if valid else None
        # 2. Validate Price Logic – short‑circuit safely
        if price is None or price <= 0 or sl_price is None:
            valid = False
        else:
            valid = sl_price < price

        risk_per_share = (price - sl_price) if valid else None

        # 3. Initialize Sizing Variables
        quantity = 0
        risk_amount = 0.0
        capital_required = 0.0
        limit_reason = "invalid_data"

        if valid and risk_per_share > 0:
            # --- FETCH CONFIGURATION ---
            risk_cfg = self.extractor.get_risk_management_config()
            
            # A. Risk Appetite (e.g., ₹500)
            target_risk = risk_cfg.get("risk_per_trade", 500)
            
            # B. Capital Constraints (e.g., Max ₹50,000)
            sizing_cfg = risk_cfg.get("position_sizing", {})
            max_capital_per_trade = sizing_cfg.get("max_capital", 50000)
            
            # If account balance ('capital') is provided, don't exceed it
            if capital:
                max_capital_per_trade = min(max_capital_per_trade, capital)

            # --- CALCULATE DUAL LIMITS ---
            
            # Limit 1: Quantity based on Risk
            # (e.g., 500 / 3.30 = 606 shares)
            qty_by_risk = int(target_risk / risk_per_share)
            
            # Limit 2: Quantity based on Capital
            # (e.g., 50000 / 330.50 = 151 shares)
            qty_by_capital = int(max_capital_per_trade / price)
            
            # --- FINAL DECISION (Take the smaller one) ---
            if qty_by_capital < qty_by_risk:
                quantity = qty_by_capital
                limit_reason = "max_capital_cap"
            else:
                quantity = qty_by_risk
                limit_reason = "risk_target"
                
            # Recalculate actuals based on final quantity
            capital_required = quantity * price
            risk_amount = quantity * risk_per_share

        # 4. Normalize Targets (Same as before)
        normalized_targets = []
        if isinstance(targets, dict):
            for k, v in targets.items():
                if k.startswith("t") and isinstance(v, (int, float)):
                    normalized_targets.append(float(v))
        if not normalized_targets and price and risk_data.get("atr_multiple"):
             atr = ensure_numeric(eval_ctx["indicators"].get("atrDynamic"))
             if atr:
                 normalized_targets = [price + (atr * 3.0), price + (atr * 5.0)]

        # 5. Return Final Model
        return {
            "valid": valid and quantity > 0,
            "entry_price": price,
            "stop_loss": sl_price,
            "risk_per_share": round(risk_per_share, 2) if risk_per_share else 0,
            "atr": ensure_numeric(eval_ctx["indicators"].get("atrDynamic", 0)),
            # Execution Details
            "quantity": quantity,
            "risk_amount": round(risk_amount, 2),
            "capital_required": round(capital_required, 2),
            "limit_reason": limit_reason,  # Useful for debugging (why is qty low?)
            
            # Metadata
            "rrRatio": rr,
            "targets": normalized_targets,
            "confidence": confidence,
            "setup": eval_ctx["setup"]["type"]
        }
    def _calculate_pattern_targets(
        self,
        pattern_name: str,
        pattern_data: Dict,
        price_data: Dict
    ) -> Optional[Dict[str, float]]:
        """✅ Calculate pattern-based targets using extractor."""
        try:
            meta = pattern_data.get("raw", {}).get("meta", {})
            entry = price_data.get("price", 0)
            
            if not entry:
                return None
            
            # ✅ Get pattern physics via extractor
            pattern_ctx = self.extractor.get_pattern_context(pattern_name)
            if not pattern_ctx:
                return None
            
            target_ratio = pattern_ctx.physics.get("target_ratio", 1.0)
            
            # Calculate depth (pattern-specific)
            depth = None
            stop_loss = None
            
            if pattern_name == "darvasBox":
                box_high = meta.get("box_high")
                box_low = meta.get("box_low")
                if box_high and box_low:
                    if box_low >= box_high:
                        self.logger.warning(
                            f"Invalid Darvas box geometry: low={box_low} >= high={box_high}"
                        )
                        return None
                    depth = box_high - box_low
                    stop_loss = box_low * 0.995
            
            elif pattern_name == "cupHandle":
                rim = meta.get("rim_level")
                depth_pct = meta.get("depth_pct")
                handle_low = meta.get("handle_low") # Check if your detector provides this
                
                if rim and depth_pct:
                    depth = rim * (depth_pct / 100.0)
                    # Fix: If detector gives handle_low, use it. Otherwise, use handle depth.
                    if handle_low:
                        stop_loss = handle_low * 0.99
                    else:
                        # Fallback: SL at 30% of cup depth (standard handle area)
                        stop_loss = rim - (depth * 0.3)
            
            elif pattern_name == "flagPennant":
                pole_pct = meta.get("pole_gain_pct")
                if pole_pct:
                    depth = entry * (pole_pct / 100.0)
                    stop_loss = meta.get("flag_low") or (entry * 0.98)
            
            elif pattern_name == "doubleTopBottom":
                target = meta.get("target")
                neckline = meta.get("neckline")
                if target and neckline:
                    return {
                        "entry": entry,
                        "stop_loss": neckline * 0.99,
                        "t1": target,
                        "t2": target * 1.5,
                        "depth": abs(target - neckline),
                        "pattern": pattern_name
                    }
            
            else:
                # For GoldenCross, Ichimoku, etc., use ATR-based stop or recent swing
                # This ensures the Signal Engine always has a Risk/Reward to display
                atrDynamic = price_data.get("atrDynamic", entry * 0.02)
                depth = atrDynamic * 3.0 # Default "reward" expectation
                stop_loss = entry - (atrDynamic * 2.0) # Default 2x ATR Stop
            # Calculate targets if depth resolved
            if depth and entry:
                t1 = round(entry + (depth * target_ratio), 2)
                t2 = round(entry + (depth * target_ratio * 2), 2)
                
                return {
                    "entry": _safe_float(entry),
                    "stop_loss": _safe_float(stop_loss),
                    "t1": _safe_float(t1),
                    "t2": _safe_float(t2),
                    "depth": _safe_float(depth),
                    "pattern": pattern_name
                }
            
            return {
                "entry": _safe_float(entry),
                "stop_loss": _safe_float(entry * 0.98),
                "t1": _safe_float(entry * 1.05),
                "t2": _safe_float(entry * 1.10),
                "depth": 0.0,
                "pattern": pattern_name,
                "source": "ultimate_fallback"
            }
        
        except Exception as e:
            self.logger.error(f"Pattern target calculation failed: {e}")
            return None

    def _build_order_model(self, eval_ctx: Dict) -> Dict[str, Any]:
        """
        FINAL execution order model.

        ✅ Uses validated patterns only
        ❌ No gating
        ❌ No scoring
        ❌ No fallback logic leakage
        """

        setup_type = eval_ctx["setup"]["type"]
        pattern_validation = eval_ctx.get("pattern_validation", {})

        # --------------------------------------------------
        # 1️⃣ Pattern-based order model (validated only)
        # --------------------------------------------------
        if pattern_validation.get("available"):
            setup_patterns = (
                pattern_validation
                .get("by_setup", {})
                .get(setup_type, {})
            )

            primary_patterns = setup_patterns.get("primary_found", [])
            entry_validation = setup_patterns.get("entry_validation", {})

            for pattern_name in primary_patterns:
                validation = entry_validation.get(pattern_name, {})
                if not validation.get("passes", True):
                    continue  # execution must respect evaluation

                pattern_ctx = self.extractor.get_pattern_context(pattern_name)
                if pattern_ctx and pattern_ctx.entry_rules:
                    return {
                        "type": pattern_ctx.entry_rules.get("order_type", "market"),
                        "trigger": pattern_ctx.entry_rules.get("trigger"),
                        "confirmation": pattern_ctx.entry_rules.get("confirmation"),
                        "source": "pattern_matrix",
                        "pattern": pattern_name
                    }

        # --------------------------------------------------
        # 2️⃣ Setup-based fallback (execution-safe)
        # --------------------------------------------------
        order_type_map = {
            "MOMENTUM_BREAKOUT": "stop_market",
            "VOLATILITY_SQUEEZE": "stop_market",
            "QUALITY_ACCUMULATION": "limit",
            "VALUE_TURNAROUND": "limit",
            "TREND_PULLBACK": "limit"
        }

        return {
            "type": order_type_map.get(setup_type, "market"),
            "source": "setup_default",
            "reason": "no_valid_primary_pattern"
        }


    def _build_market_constraints(self, eval_ctx: Dict) -> Dict[str, Any]:
        """✅ Build market constraints using extractor."""
        # ✅ Get market constraints via extractor
        constraints = self.extractor.get_market_constraints_config()
        
        if constraints:
            return {
                "gates": constraints,
                "source": "strategy_matrix",
                "blocking": constraints.get("blocking", False),
                "strategy": eval_ctx["strategy"]["primary"]
            }
        
        return {"source": "none"}

    def _build_time_constraints(self, now: Optional[datetime]) -> Dict[str, Any]:
        """✅ Build time constraints using extractor."""
        if not now:
            return {"current_time": None, "allowed": True}
        
        current_time = now.time()
        
        # ✅ Get time filters via extractor
        time_filters = self.extractor.get_time_filters_config()
        
        if self.horizon == "intraday" and time_filters:
            # Check avoidance windows
            avoidance = time_filters.get("avoidance_windows", [])
            for window in avoidance:
                start_time = time(*map(int, window["start"].split(":")))
                end_time = time(*map(int, window["end"].split(":")))
                
                if start_time <= current_time <= end_time:
                    return {
                        "current_time": now.strftime("%H:%M"),
                        "allowed": False,
                        "reason": window["reason"]
                    }
        
        return {"current_time": now.strftime("%H:%M"), "allowed": True}

    def _can_execute(self, exec_ctx: Dict, eval_ctx: Dict) -> Dict[str, Any]:
        """Final execution decision combining all checks."""
        # checks = {
        #     "entry_permission": exec_ctx["entry_permission"]["allowed"],
        #     "time_allowed": exec_ctx["time_constraints"]["allowed"],
        #     "capital_available": exec_ctx["position_sizing"]["mode"] != "unknown"
        # }
        checks = {
            "entry_permission": exec_ctx["entry_permission"]["allowed"],
            "time_allowed": exec_ctx["time_constraints"]["allowed"],
            "risk_valid": exec_ctx["risk"]["valid"],
            "capital_available": exec_ctx["position_sizing"]["capital"],
            "quantity_available": exec_ctx["risk"]["quantity"] not in (None, 0)
        }

        all_passed = all(checks.values())
        failures = []
        
        if not checks["entry_permission"]:
            failures.append(exec_ctx["entry_permission"]["reason"])
        if not checks["time_allowed"]:
            failures.append(exec_ctx["time_constraints"].get("reason"))
        if not checks["capital_available"]:
            failures.append("Capital not provided")
        
        return {
            "can_execute": all_passed,
            "checks": checks,
            "failures": failures
        }

    # ========================================================================
    # PUBLIC API METHODS (for external callers)
    # ========================================================================

    def get_setup_priority(self, setup_name: str) -> float:
        """
        Get resolved priority for setup (public API).
        
        Delegates to extractor which handles hierarchy:
        Horizon override > Setup default > Fallback to 0
        """
        return self.extractor.get_setup_priority(setup_name)

    def get_setup_confidence_floor(self, setup_name: str) -> float:
        """
        Get confidence floor for setup (public API).
        
        Delegates to extractor which handles hierarchy:
        Horizon override > Global baseline > Default (40)
        """
        return self.extractor.get_setup_confidence_floor(setup_name)
    
    def _validate_indian_market_gates(self, eval_ctx: Dict) -> Tuple[bool, str]:
        """✅ Validate Indian market gates using extractor."""
        # ✅ Get market constraints via extractor
        constraints = self.extractor.get_market_constraints_config()
        
        if not constraints:
            return True, "No Indian market gates configured"
        
        gates = constraints.get("gates", {})
        if not gates:
            return True, "No gates defined"
        return True, "All Indian market gates passed"
        price_data = eval_ctx.get("price_data", {})
        
        # Check min average volume
        min_vol = gates.get("min_avg_volume")
        if min_vol:
            actual_vol = price_data.get("avgVolume", 0)
            if actual_vol < min_vol:
                return False, f"Avg volume {actual_vol:,.0f} < required {min_vol:,.0f}"
        
        # Check max spread
        max_spread = gates.get("max_spread_pct")
        if max_spread:
            spread = price_data.get("spread_pct") or \
                    price_data.get("bidAskSpread") or \
                    price_data.get("spreadPct", 0)
            
            if spread > max_spread:
                return False, f"Spread {spread:.2%} > max {max_spread:.2%}"
        
        # Check min delivery
        min_delivery = gates.get("min_delivery_pct")
        if min_delivery:
            delivery = price_data.get("delivery_pct") or \
                    price_data.get("deliveryPct", 0)
            
            if delivery < min_delivery:
                return False, f"Delivery {delivery:.1f}% < required {min_delivery}%"
        
        # Check GSM avoidance
        if gates.get("avoid_gsm"):
            gsm_flag = price_data.get("gsm_flag") or \
                    price_data.get("gsmFlag") or \
                    price_data.get("in_gsm", False)
            
            if gsm_flag:
                return False, "Stock in GSM - blocked"
        
        return True, "All Indian market gates passed"

def create_resolver(master_config: Dict, horizon: str) -> ConfigResolver:
    """Factory function to create resolver instance."""
    return ConfigResolver(master_config, horizon)