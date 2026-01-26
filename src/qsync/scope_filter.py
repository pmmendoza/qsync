"""
Unified scope filter parser for qsync dimensions.

Provides a boolean expression DSL for filtering questions/items/translations/eos
by QID, tag, or JavaScript file matching.

Grammar:
    expr := term (OR term)*
    term := factor (AND factor)*
    factor := criterion | '(' expr ')'
    criterion := qid:VALUE | tag:VALUE | js:VALUE

Examples:
    --scope "qid:QID123"
    --scope "tag:InPre OR tag:OutPre"
    --scope "qid:QID123 AND tag:foo"
    --scope "(qid:QID123 OR qid:QID456) AND tag:foo"
    --scope 'tag:"my tag"'  # quoted value for spaces
"""

from __future__ import annotations

import difflib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

CriterionType = Literal["qid", "tag", "js"]


def _fuzzy_match_tag(tag_value: str, available_tags: list[str]) -> Optional[str]:
    """
    Find closest fuzzy match for a tag value.

    Args:
        tag_value: Tag value to match
        available_tags: List of available tags

    Returns:
        Closest match if found, None otherwise
    """
    if not available_tags:
        return None

    # Exact match (case-insensitive)
    for tag in available_tags:
        if tag.lower() == tag_value.lower():
            return tag

    # Fuzzy match using difflib
    matches = difflib.get_close_matches(tag_value, available_tags, n=1, cutoff=0.6)
    return matches[0] if matches else None


@dataclass
class Criterion:
    """Single filter criterion (qid:X, tag:Y, js:Z)."""

    type: CriterionType
    value: str

    def validate(self, available_tags: Optional[list[str]] = None) -> Optional[str]:
        """
        Validate criterion and suggest corrections if invalid.

        Args:
            available_tags: List of available tags for fuzzy matching

        Returns:
            Error message if invalid, None if valid
        """
        if self.type == "tag" and available_tags:
            # Check if tag exists (case-insensitive exact match)
            if not any(tag.lower() == self.value.lower() for tag in available_tags):
                # Try fuzzy match
                suggestion = _fuzzy_match_tag(self.value, available_tags)
                if suggestion:
                    return f"Tag '{self.value}' not found. Did you mean '{suggestion}'?"
                else:
                    return f"Tag '{self.value}' not found in available tags."

        return None

    def matches(
        self,
        qid: Optional[str] = None,
        tags: Optional[list[str]] = None,
        js_file: Optional[Path] = None,
        survey_id: Optional[str] = None,
    ) -> bool:
        """Check if this criterion matches the given context."""
        if self.type == "qid":
            return qid == self.value if qid else False

        elif self.type == "tag":
            if not tags:
                return False
            # Case-insensitive tag matching
            return any(tag.lower() == self.value.lower() for tag in tags)

        elif self.type == "js":
            if not js_file:
                return False
            # Match against stem (without .js extension)
            return js_file.stem == self.value or str(js_file) == self.value

        return False


@dataclass
class OrExpr:
    """Disjunction of terms."""

    terms: list[AndExpr | Criterion]

    def matches(
        self,
        qid: Optional[str] = None,
        tags: Optional[list[str]] = None,
        js_file: Optional[Path] = None,
        survey_id: Optional[str] = None,
    ) -> bool:
        """Evaluate OR expression."""
        return any(term.matches(qid, tags, js_file, survey_id) for term in self.terms)


@dataclass
class AndExpr:
    """Conjunction of factors."""

    factors: list[Criterion | OrExpr]

    def matches(
        self,
        qid: Optional[str] = None,
        tags: Optional[list[str]] = None,
        js_file: Optional[Path] = None,
        survey_id: Optional[str] = None,
    ) -> bool:
        """Evaluate AND expression."""
        return all(
            factor.matches(qid, tags, js_file, survey_id) for factor in self.factors
        )


class ScopeFilter:
    """Parse and evaluate scope filter expressions."""

    def __init__(self, expression: Optional[str] = None):
        """
        Initialize scope filter from expression.

        Args:
            expression: Boolean expression like "qid:X OR tag:Y"
                       If None/empty, matches everything.
        """
        self.expression = expression
        self.root: Optional[OrExpr] = None

        if expression:
            self.root = self._parse(expression)

    @classmethod
    def parse(
        cls,
        expression: Optional[str],
        **_unused: object,
    ) -> Optional["ScopeFilter"]:
        if expression is None or str(expression).strip() == "":
            return None
        if isinstance(expression, ScopeFilter):
            return expression
        return cls(str(expression))

    def validate_with_context(
        self, available_tags: Optional[list[str]] = None
    ) -> list[str]:
        """
        Validate filter with available context (e.g., tag list).

        Args:
            available_tags: List of available tags for validation

        Returns:
            List of validation warnings/errors
        """
        warnings = []

        if not self.root:
            return warnings

        # Collect all criteria from the AST
        def collect_criteria(node) -> list[Criterion]:
            if isinstance(node, Criterion):
                return [node]
            elif isinstance(node, (OrExpr, AndExpr)):
                result = []
                items = node.terms if isinstance(node, OrExpr) else node.factors
                for item in items:
                    result.extend(collect_criteria(item))
                return result
            return []

        criteria = collect_criteria(self.root)

        # Validate each criterion
        for criterion in criteria:
            error = criterion.validate(available_tags=available_tags)
            if error:
                warnings.append(error)

        return warnings

    @classmethod
    def from_legacy_args(
        cls,
        include_qid: Optional[list[str]] = None,
        include_tag: Optional[list[str]] = None,
        include_js: Optional[list[str]] = None,
    ) -> ScopeFilter:
        """
        Convert legacy --include-qid/--include-tag/--include-js to scope expression.

        Args:
            include_qid: List of QIDs to include
            include_tag: List of tags to include
            include_js: List of JS file names to include

        Returns:
            ScopeFilter with equivalent expression
        """
        parts = []

        if include_qid:
            parts.extend(f"qid:{qid}" for qid in include_qid)
        if include_tag:
            parts.extend(f"tag:{tag}" for tag in include_tag)
        if include_js:
            parts.extend(f"js:{js}" for js in include_js)

        if not parts:
            return cls(None)  # Match everything

        expression = " OR ".join(parts)
        return cls(expression)

    def matches(
        self,
        qid: Optional[str] = None,
        tags: Optional[list[str]] = None,
        js_file: Optional[Path] = None,
        survey_id: Optional[str] = None,
    ) -> bool:
        """
        Check if the given context matches this filter.

        Args:
            qid: Question ID
            tags: List of question tags
            js_file: Path to JavaScript file
            survey_id: Survey ID for tag resolution

        Returns:
            True if matches (or no filter), False otherwise
        """
        if self.root is None:
            return True  # No filter = match everything

        return self.root.matches(qid, tags, js_file, survey_id)

    def _parse(self, expression: str) -> OrExpr:
        """Parse boolean expression into AST."""
        tokens = self._tokenize(expression)
        expr, remaining = self._parse_or_expr(tokens)

        if remaining:
            raise ValueError(
                f"Unexpected tokens after parsing: {remaining}. "
                f"Check for unmatched parentheses or invalid syntax."
            )

        return expr

    def _tokenize(self, expression: str) -> list[str]:
        """
        Tokenize expression into operators, criteria, and parentheses.

        Handles quoted values like tag:"my tag".
        """
        # Replace logical operators with space-padded versions for splitting
        normalized = expression.replace("(", " ( ").replace(")", " ) ")
        normalized = re.sub(r"\bOR\b", " OR ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\bAND\b", " AND ", normalized, flags=re.IGNORECASE)

        # Use shlex to handle quoted strings
        try:
            tokens = shlex.split(normalized)
        except ValueError as e:
            raise ValueError(f"Invalid quoted string in expression: {e}")

        # Normalize operators to uppercase
        return [tok.upper() if tok.upper() in ("OR", "AND") else tok for tok in tokens]

    def _parse_or_expr(self, tokens: list[str]) -> tuple[OrExpr, list[str]]:
        """Parse OR expression: term (OR term)*"""
        terms = []
        term, tokens = self._parse_and_expr(tokens)
        terms.append(term)

        while tokens and tokens[0] == "OR":
            tokens = tokens[1:]  # consume OR
            term, tokens = self._parse_and_expr(tokens)
            terms.append(term)

        return OrExpr(terms), tokens

    def _parse_and_expr(
        self, tokens: list[str]
    ) -> tuple[AndExpr | Criterion, list[str]]:
        """Parse AND expression: factor (AND factor)*"""
        factors = []
        factor, tokens = self._parse_factor(tokens)
        factors.append(factor)

        while tokens and tokens[0] == "AND":
            tokens = tokens[1:]  # consume AND
            factor, tokens = self._parse_factor(tokens)
            factors.append(factor)

        # Optimize: single factor = return criterion directly
        if len(factors) == 1 and isinstance(factors[0], Criterion):
            return factors[0], tokens

        return AndExpr(factors), tokens

    def _parse_factor(self, tokens: list[str]) -> tuple[Criterion | OrExpr, list[str]]:
        """Parse factor: criterion | '(' expr ')'"""
        if not tokens:
            raise ValueError("Unexpected end of expression")

        if tokens[0] == "(":
            tokens = tokens[1:]  # consume (
            expr, tokens = self._parse_or_expr(tokens)

            if not tokens or tokens[0] != ")":
                raise ValueError("Missing closing parenthesis")

            tokens = tokens[1:]  # consume )
            return expr, tokens

        # Must be a criterion
        return self._parse_criterion(tokens)

    def _parse_criterion(self, tokens: list[str]) -> tuple[Criterion, list[str]]:
        """Parse criterion: TYPE:VALUE with enhanced error messages."""
        if not tokens:
            raise ValueError("Expected criterion, got end of expression")

        token = tokens[0]
        tokens = tokens[1:]

        if ":" not in token:
            raise ValueError(
                f"Invalid scope syntax: expected 'type:value', got '{token}'. "
                f"Valid types: qid, tag, js"
            )

        parts = token.split(":", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid scope syntax: expected 'type:value', got '{token}'. "
                f"Format should be: qid:QID123, tag:MyTag, or js:filename"
            )

        criterion_type, value = parts
        criterion_type_lower = criterion_type.lower()

        if criterion_type_lower not in ("qid", "tag", "js"):
            # Provide fuzzy suggestion for common typos
            valid_types = ["qid", "tag", "js"]
            suggestion = difflib.get_close_matches(
                criterion_type_lower, valid_types, n=1, cutoff=0.6
            )

            if suggestion:
                raise ValueError(
                    f"Unknown scope type '{criterion_type}'. Valid: qid, tag, js. "
                    f"Did you mean '{suggestion[0]}'?"
                )
            else:
                raise ValueError(
                    f"Unknown scope type '{criterion_type}'. Valid: qid, tag, js"
                )

        return Criterion(type=criterion_type_lower, value=value), tokens  # type: ignore

    def __repr__(self) -> str:
        """String representation."""
        if self.expression is None:
            return "ScopeFilter(match_all=True)"
        return f"ScopeFilter({self.expression!r})"
