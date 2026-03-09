"""Tests for checkpoint field-type validation (_has_valid_field_types and helpers).

Extracted from test_checkpoint.py to keep each test file focused and under
~500 lines. Covers: boolean-as-int rejection, bounds checks, type checks
for all checkpoint fields, and cross-field validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

from checkloop import checkpoint
from checkloop.checkpoint import (
    _has_valid_field_types,
    _is_strict_int,
    _is_strict_number,
    _is_string_list,
)
from tests.helpers import assert_checkpoint_field_rejected, make_checkpoint_data


# =============================================================================
# _has_valid_field_types — invalid field types
# =============================================================================

class TestHasValidFieldTypes:
    """Tests for _has_valid_field_types() checkpoint validation."""

    def test_invalid_int_field_rejects(self, tmp_path: Path) -> None:
        """Checkpoint with non-int current_cycle is rejected."""
        assert_checkpoint_field_rejected(tmp_path, current_cycle="not_an_int")

    def test_int_below_minimum_rejects(self, tmp_path: Path) -> None:
        """Checkpoint with current_cycle < 1 is rejected."""
        assert_checkpoint_field_rejected(tmp_path, current_cycle=0)

    def test_invalid_list_field_rejects(self, tmp_path: Path) -> None:
        """Checkpoint with non-list check_ids is rejected."""
        assert_checkpoint_field_rejected(tmp_path, check_ids="not_a_list")

    def test_invalid_workdir_type_rejects(self, tmp_path: Path) -> None:
        """Checkpoint with non-string workdir is rejected."""
        assert_checkpoint_field_rejected(tmp_path, workdir=12345)


# =============================================================================
# _has_valid_field_types — edge cases for boolean-as-int and bounds
# =============================================================================

class TestFieldTypeEdgeCases:
    """Edge case tests for checkpoint field validation."""

    def test_boolean_true_rejected_as_int(self, tmp_path: Path) -> None:
        """JSON true should not pass as a valid integer field.

        In Python, isinstance(True, int) is True because bool is a subclass
        of int, so we must explicitly reject booleans.
        """
        assert_checkpoint_field_rejected(tmp_path, current_cycle=True)

    def test_boolean_false_rejected_as_int(self, tmp_path: Path) -> None:
        """JSON false should not pass as a valid integer field."""
        assert_checkpoint_field_rejected(tmp_path, current_check_index=False)

    def test_empty_check_ids_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with empty check_ids list should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, check_ids=[])

    def test_empty_active_check_ids_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with empty active_check_ids list should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, active_check_ids=[])

    def test_non_string_items_in_check_ids_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with non-string items in check_ids should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, check_ids=[1, 2, 3])

    def test_non_string_items_in_active_check_ids_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with non-string items in active_check_ids should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, active_check_ids=[True, None])

    def test_check_index_exceeds_active_ids_length(self, tmp_path: Path) -> None:
        """Checkpoint with current_check_index > len(active_check_ids) should be rejected."""
        assert_checkpoint_field_rejected(
            tmp_path, active_check_ids=["a", "b"], current_check_index=3,
        )

    def test_check_index_equal_to_length_is_valid(self, tmp_path: Path) -> None:
        """current_check_index == len(active_check_ids) means 'all completed' and is valid."""
        data = make_checkpoint_data(
            workdir=str(tmp_path),
            active_check_ids=["a", "b"],
            current_check_index=2,
            check_ids=["a", "b"],
        )
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["current_check_index"] == 2

    def test_mixed_types_in_check_ids_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with mixed string/non-string items in check_ids should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, check_ids=["valid", 42, "also-valid"])

    def test_non_string_started_at_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with non-string started_at should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, started_at=12345)

    def test_null_started_at_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with null started_at should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, started_at=None)

    def test_string_convergence_threshold_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with string convergence_threshold should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, convergence_threshold="not_a_number")

    def test_boolean_convergence_threshold_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with boolean convergence_threshold should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, convergence_threshold=True)

    def test_null_convergence_threshold_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with null convergence_threshold should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, convergence_threshold=None)

    def test_valid_int_convergence_threshold_accepted(self, tmp_path: Path) -> None:
        """Checkpoint with integer convergence_threshold (e.g. 0) should be accepted."""
        data = make_checkpoint_data(workdir=str(tmp_path), convergence_threshold=0)
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["convergence_threshold"] == 0

    def test_string_prev_change_pct_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with string prev_change_pct should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, prev_change_pct="bad")

    def test_boolean_prev_change_pct_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with boolean prev_change_pct should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, prev_change_pct=True)

    def test_null_prev_change_pct_accepted(self, tmp_path: Path) -> None:
        """Checkpoint with null prev_change_pct should be accepted (means not yet computed)."""
        data = make_checkpoint_data(workdir=str(tmp_path), prev_change_pct=None)
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["prev_change_pct"] is None

    def test_non_list_previously_changed_ids_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with non-list previously_changed_ids (when not None) should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, previously_changed_ids="not_a_list")

    def test_previously_changed_ids_with_non_string_items_rejected(self, tmp_path: Path) -> None:
        """Checkpoint with non-string items in previously_changed_ids should be rejected."""
        assert_checkpoint_field_rejected(tmp_path, previously_changed_ids=[1, 2, 3])

    def test_null_previously_changed_ids_accepted(self, tmp_path: Path) -> None:
        """Checkpoint with null previously_changed_ids should be accepted."""
        data = make_checkpoint_data(workdir=str(tmp_path), previously_changed_ids=None)
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["previously_changed_ids"] is None

    def test_valid_previously_changed_ids_accepted(self, tmp_path: Path) -> None:
        """Checkpoint with a valid list of previously_changed_ids should be accepted."""
        data = make_checkpoint_data(
            workdir=str(tmp_path),
            previously_changed_ids=["readability", "dry"],
        )
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["previously_changed_ids"] == ["readability", "dry"]

    def test_empty_previously_changed_ids_accepted(self, tmp_path: Path) -> None:
        """Checkpoint with empty previously_changed_ids list should be accepted."""
        data = make_checkpoint_data(
            workdir=str(tmp_path),
            previously_changed_ids=[],
        )
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["previously_changed_ids"] == []


# =============================================================================
# Validation helper edge cases (from test_edge_cases.py)
# =============================================================================

class TestCheckpointValidationEdgeCases:
    """Edge cases for checkpoint field validation helpers."""

    def test_is_strict_int_with_bool_true(self) -> None:
        """Booleans should not be accepted as ints."""
        assert _is_strict_int(True) is False
        assert _is_strict_int(False) is False

    def test_is_strict_int_at_minimum(self) -> None:
        assert _is_strict_int(0, min_value=0) is True
        assert _is_strict_int(1, min_value=1) is True

    def test_is_strict_int_below_minimum(self) -> None:
        assert _is_strict_int(0, min_value=1) is False
        assert _is_strict_int(-1, min_value=0) is False

    def test_is_strict_int_with_float(self) -> None:
        assert _is_strict_int(1.0) is False
        assert _is_strict_int(0.0) is False

    def test_is_strict_number_with_bool(self) -> None:
        assert _is_strict_number(True) is False
        assert _is_strict_number(False) is False

    def test_is_strict_number_with_zero(self) -> None:
        assert _is_strict_number(0) is True
        assert _is_strict_number(0.0) is True

    def test_is_strict_number_with_negative(self) -> None:
        assert _is_strict_number(-1) is True
        assert _is_strict_number(-0.5) is True

    def test_is_strict_number_with_string(self) -> None:
        assert _is_strict_number("1") is False

    def test_is_string_list_empty_allowed(self) -> None:
        assert _is_string_list([], allow_empty=True) is True

    def test_is_string_list_empty_disallowed(self) -> None:
        assert _is_string_list([], allow_empty=False) is False

    def test_is_string_list_with_mixed_types(self) -> None:
        assert _is_string_list(["a", 1, "b"]) is False

    def test_is_string_list_with_none_item(self) -> None:
        assert _is_string_list(["a", None]) is False

    def test_is_string_list_with_nested_list(self) -> None:
        assert _is_string_list(["a", ["b"]]) is False

    def test_is_string_list_not_a_list(self) -> None:
        assert _is_string_list("abc") is False
        assert _is_string_list({"a": 1}) is False

    def test_current_cycle_exceeds_num_cycles(self) -> None:
        """current_cycle > num_cycles should be rejected."""
        data = make_checkpoint_data(current_cycle=5, num_cycles=3)
        assert _has_valid_field_types(cast(dict[str, object], data)) is False

    def test_current_cycle_equals_num_cycles(self) -> None:
        """current_cycle == num_cycles is valid (last cycle)."""
        data = make_checkpoint_data(current_cycle=2, num_cycles=2)
        assert _has_valid_field_types(cast(dict[str, object], data)) is True

    def test_check_index_at_boundary(self) -> None:
        """current_check_index == len(active_check_ids) means 'all done'."""
        data = make_checkpoint_data(
            current_check_index=4,
            active_check_ids=["a", "b", "c", "d"],
        )
        assert _has_valid_field_types(cast(dict[str, object], data)) is True

    def test_check_index_beyond_boundary(self) -> None:
        """current_check_index > len(active_check_ids) is invalid."""
        data = make_checkpoint_data(
            current_check_index=5,
            active_check_ids=["a", "b", "c", "d"],
        )
        assert _has_valid_field_types(cast(dict[str, object], data)) is False


class TestLoadCheckpointMissingOptionalFields:
    """Edge cases for load_checkpoint when fields are missing."""

    def test_missing_convergence_threshold_rejected(self, tmp_path: Path) -> None:
        """A checkpoint missing convergence_threshold should be rejected."""
        raw: dict[str, Any] = dict(make_checkpoint_data(workdir=str(tmp_path)))
        del raw["convergence_threshold"]
        path = tmp_path / checkpoint._CHECKPOINT_FILENAME
        path.write_text(json.dumps(raw))
        assert checkpoint.load_checkpoint(str(tmp_path)) is None

    def test_missing_prev_change_pct_still_valid(self, tmp_path: Path) -> None:
        """prev_change_pct missing is treated as None (not in required_keys)."""
        raw: dict[str, Any] = dict(make_checkpoint_data(workdir=str(tmp_path)))
        if "prev_change_pct" in raw:
            del raw["prev_change_pct"]
        path = tmp_path / checkpoint._CHECKPOINT_FILENAME
        path.write_text(json.dumps(raw))
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None

    def test_missing_previously_changed_ids_still_valid(self, tmp_path: Path) -> None:
        """previously_changed_ids missing is treated as None."""
        raw: dict[str, Any] = dict(make_checkpoint_data(workdir=str(tmp_path)))
        if "previously_changed_ids" in raw:
            del raw["previously_changed_ids"]
        path = tmp_path / checkpoint._CHECKPOINT_FILENAME
        path.write_text(json.dumps(raw))
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None


class TestIsStrictIntEdgeCases:
    """Additional boundary tests for _is_strict_int()."""

    def test_none_is_rejected(self) -> None:
        assert _is_strict_int(None) is False

    def test_string_number_is_rejected(self) -> None:
        assert _is_strict_int("5") is False

    def test_max_python_int(self) -> None:
        """Very large ints should still be accepted."""
        assert _is_strict_int(2**63) is True

    def test_list_is_rejected(self) -> None:
        assert _is_strict_int([1]) is False

    def test_dict_is_rejected(self) -> None:
        assert _is_strict_int({"a": 1}) is False


class TestIsStringListEdgeCases:
    """Additional edge cases for _is_string_list()."""

    def test_tuple_is_rejected(self) -> None:
        """Tuples are not lists."""
        assert _is_string_list(("a", "b")) is False

    def test_single_string_item(self) -> None:
        assert _is_string_list(["hello"]) is True

    def test_unicode_strings(self) -> None:
        assert _is_string_list(["こんにちは", "🎉"]) is True

    def test_empty_strings(self) -> None:
        assert _is_string_list(["", "", ""]) is True

    def test_int_is_rejected(self) -> None:
        assert _is_string_list(42) is False

    def test_none_is_rejected(self) -> None:
        assert _is_string_list(None) is False
