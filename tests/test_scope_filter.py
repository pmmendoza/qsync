"""
Tests for scope_filter module.
"""

from pathlib import Path

import pytest

from qsync.scope_filter import AndExpr, Criterion, OrExpr, ScopeFilter


class TestCriterion:
    """Test Criterion matching."""
    
    def test_qid_criterion_matches(self):
        """QID criterion matches exact QID."""
        criterion = Criterion(type="qid", value="QID123")
        
        assert criterion.matches(qid="QID123") is True
        assert criterion.matches(qid="QID456") is False
        assert criterion.matches(qid=None) is False
    
    def test_tag_criterion_matches(self):
        """Tag criterion matches when tag in list."""
        criterion = Criterion(type="tag", value="InPre")
        
        assert criterion.matches(tags=["InPre", "foo"]) is True
        assert criterion.matches(tags=["foo", "bar"]) is False
        assert criterion.matches(tags=[]) is False
        assert criterion.matches(tags=None) is False
    
    def test_js_criterion_matches_stem(self):
        """JS criterion matches file stem."""
        criterion = Criterion(type="js", value="my_script")
        
        assert criterion.matches(js_file=Path("my_script.js")) is True
        assert criterion.matches(js_file=Path("other_script.js")) is False
        assert criterion.matches(js_file=None) is False
    
    def test_js_criterion_matches_full_path(self):
        """JS criterion can match full path string."""
        criterion = Criterion(type="js", value="survey_js/core/my_script.js")
        
        assert criterion.matches(js_file=Path("survey_js/core/my_script.js")) is True
        assert criterion.matches(js_file=Path("my_script.js")) is False


class TestOrExpr:
    """Test OR expression evaluation."""
    
    def test_or_expr_any_matches(self):
        """OR expression returns True if any term matches."""
        expr = OrExpr(terms=[
            Criterion(type="qid", value="QID123"),
            Criterion(type="qid", value="QID456"),
        ])
        
        assert expr.matches(qid="QID123") is True
        assert expr.matches(qid="QID456") is True
        assert expr.matches(qid="QID789") is False
    
    def test_or_expr_empty_false(self):
        """OR expression with no terms returns False."""
        expr = OrExpr(terms=[])
        
        assert expr.matches(qid="QID123") is False


class TestAndExpr:
    """Test AND expression evaluation."""
    
    def test_and_expr_all_must_match(self):
        """AND expression returns True only if all factors match."""
        expr = AndExpr(factors=[
            Criterion(type="qid", value="QID123"),
            Criterion(type="tag", value="InPre"),
        ])
        
        assert expr.matches(qid="QID123", tags=["InPre"]) is True
        assert expr.matches(qid="QID123", tags=["OutPre"]) is False
        assert expr.matches(qid="QID456", tags=["InPre"]) is False
    
    def test_and_expr_empty_true(self):
        """AND expression with no factors returns True."""
        expr = AndExpr(factors=[])
        
        assert expr.matches(qid="QID123") is True


class TestScopeFilter:
    """Test ScopeFilter parsing and evaluation."""
    
    def test_none_expression_matches_all(self):
        """None/empty expression matches everything."""
        filter1 = ScopeFilter(None)
        filter2 = ScopeFilter("")
        
        assert filter1.matches(qid="QID123") is True
        assert filter2.matches(qid="QID123") is True
    
    def test_simple_qid(self):
        """Parse simple QID criterion."""
        filter = ScopeFilter("qid:QID123")
        
        assert filter.matches(qid="QID123") is True
        assert filter.matches(qid="QID456") is False
    
    def test_simple_tag(self):
        """Parse simple tag criterion."""
        filter = ScopeFilter("tag:InPre")
        
        assert filter.matches(tags=["InPre"]) is True
        assert filter.matches(tags=["OutPre"]) is False
    
    def test_simple_js(self):
        """Parse simple JS criterion."""
        filter = ScopeFilter("js:my_script")
        
        assert filter.matches(js_file=Path("my_script.js")) is True
        assert filter.matches(js_file=Path("other.js")) is False
    
    def test_or_expression(self):
        """Parse OR expression."""
        filter = ScopeFilter("qid:QID123 OR qid:QID456")
        
        assert filter.matches(qid="QID123") is True
        assert filter.matches(qid="QID456") is True
        assert filter.matches(qid="QID789") is False
    
    def test_and_expression(self):
        """Parse AND expression."""
        filter = ScopeFilter("qid:QID123 AND tag:InPre")
        
        assert filter.matches(qid="QID123", tags=["InPre"]) is True
        assert filter.matches(qid="QID123", tags=["OutPre"]) is False
        assert filter.matches(qid="QID456", tags=["InPre"]) is False
    
    def test_parentheses(self):
        """Parse parenthesized expression."""
        filter = ScopeFilter("(qid:QID123 OR qid:QID456) AND tag:InPre")
        
        assert filter.matches(qid="QID123", tags=["InPre"]) is True
        assert filter.matches(qid="QID456", tags=["InPre"]) is True
        assert filter.matches(qid="QID123", tags=["OutPre"]) is False
        assert filter.matches(qid="QID789", tags=["InPre"]) is False
    
    def test_quoted_value_with_spaces(self):
        """Parse quoted values containing spaces."""
        filter = ScopeFilter('tag:"my tag"')
        
        assert filter.matches(tags=["my tag"]) is True
        assert filter.matches(tags=["my", "tag"]) is False
    
    def test_case_insensitive_operators(self):
        """Operators are case-insensitive."""
        filter = ScopeFilter("qid:QID123 or qid:QID456 and tag:foo")
        
        # Precedence: AND binds tighter, so this is: qid:QID123 OR (qid:QID456 AND tag:foo)
        assert filter.matches(qid="QID123") is True
        assert filter.matches(qid="QID456", tags=["foo"]) is True
        assert filter.matches(qid="QID456", tags=["bar"]) is False
    
    def test_invalid_syntax_missing_colon(self):
        """Invalid criterion without colon raises error with improved message."""
        with pytest.raises(ValueError, match="Invalid scope syntax.*expected 'type:value'"):
            ScopeFilter("qid123")
    
    def test_invalid_syntax_unknown_type(self):
        """Invalid criterion type raises error with improved message."""
        with pytest.raises(ValueError, match="Unknown scope type.*Valid: qid, tag, js"):
            ScopeFilter("foo:bar")
    
    def test_invalid_syntax_unmatched_paren(self):
        """Unmatched parenthesis raises error."""
        with pytest.raises(ValueError, match="Missing closing parenthesis"):
            ScopeFilter("(qid:QID123")
    
    def test_invalid_syntax_extra_tokens(self):
        """Extra tokens after expression raise error."""
        with pytest.raises(ValueError, match="Unexpected tokens after parsing"):
            ScopeFilter("qid:QID123)")
    
    def test_invalid_syntax_malformed_quote(self):
        """Malformed quoted string raises error."""
        with pytest.raises(ValueError, match="Invalid quoted string"):
            ScopeFilter('tag:"unclosed')
    
    def test_from_legacy_args_qid_only(self):
        """Convert legacy --include-qid to scope expression."""
        filter = ScopeFilter.from_legacy_args(include_qid=["QID123", "QID456"])
        
        assert filter.matches(qid="QID123") is True
        assert filter.matches(qid="QID456") is True
        assert filter.matches(qid="QID789") is False
    
    def test_from_legacy_args_tag_only(self):
        """Convert legacy --include-tag to scope expression."""
        filter = ScopeFilter.from_legacy_args(include_tag=["InPre", "OutPre"])
        
        assert filter.matches(tags=["InPre"]) is True
        assert filter.matches(tags=["OutPre"]) is True
        assert filter.matches(tags=["foo"]) is False
    
    def test_from_legacy_args_js_only(self):
        """Convert legacy --include-js to scope expression."""
        filter = ScopeFilter.from_legacy_args(include_js=["my_script", "other_script"])
        
        assert filter.matches(js_file=Path("my_script.js")) is True
        assert filter.matches(js_file=Path("other_script.js")) is True
        assert filter.matches(js_file=Path("third.js")) is False
    
    def test_from_legacy_args_mixed(self):
        """Convert legacy args with mixed criteria."""
        filter = ScopeFilter.from_legacy_args(
            include_qid=["QID123"],
            include_tag=["InPre"],
            include_js=["my_script"],
        )
        
        # All converted to OR
        assert filter.matches(qid="QID123") is True
        assert filter.matches(tags=["InPre"]) is True
        assert filter.matches(js_file=Path("my_script.js")) is True
        assert filter.matches(qid="QID456") is False
    
    def test_from_legacy_args_empty(self):
        """Empty legacy args return match-all filter."""
        filter = ScopeFilter.from_legacy_args()
        
        assert filter.matches(qid="QID123") is True
        assert filter.matches(tags=["InPre"]) is True
    
    def test_repr(self):
        """Test string representation."""
        filter1 = ScopeFilter("qid:QID123")
        filter2 = ScopeFilter(None)
        
        assert "qid:QID123" in repr(filter1)
        assert "match_all" in repr(filter2)
    
    def test_complex_expression(self):
        """Parse complex multi-level expression."""
        filter = ScopeFilter("(qid:QID1 OR qid:QID2) AND (tag:A OR tag:B)")
        
        # QID1 with tag A
        assert filter.matches(qid="QID1", tags=["A"]) is True
        # QID1 with tag B
        assert filter.matches(qid="QID1", tags=["B"]) is True
        # QID2 with tag A
        assert filter.matches(qid="QID2", tags=["A"]) is True
        # QID1 without A or B
        assert filter.matches(qid="QID1", tags=["C"]) is False
        # QID3 with tag A
        assert filter.matches(qid="QID3", tags=["A"]) is False
