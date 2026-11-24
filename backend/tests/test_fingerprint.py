"""The fingerprint must be stable across runs and sensitive to real change."""
import math

import fingerprint as fp


def test_key_order_does_not_matter():
    assert fp.fingerprint({"a": 1, "b": 2}) == fp.fingerprint({"b": 2, "a": 1})


def test_nested_dict_order_does_not_matter():
    left = {"book": {"net": 1.5, "trades": 3}}
    right = {"book": {"trades": 3, "net": 1.5}}
    assert fp.fingerprint(left) == fp.fingerprint(right)


def test_last_bit_float_noise_is_ignored():
    # 0.1 + 0.2 != 0.3 in binary floating point. A different BLAS or pandas
    # build producing that difference must not look like a changed result.
    assert 0.1 + 0.2 != 0.3
    assert fp.fingerprint({"x": 0.1 + 0.2}) == fp.fingerprint({"x": 0.3})


def test_real_change_is_caught():
    assert fp.fingerprint({"x": 1.0}) != fp.fingerprint({"x": 1.01})


def test_int_and_float_of_same_value_agree():
    assert fp.canonical(3) == fp.canonical(3.0) == "3"


def test_negative_zero_matches_zero():
    assert fp.fingerprint({"x": -0.0}) == fp.fingerprint({"x": 0.0})


def test_bool_is_not_confused_with_int():
    # bool is a subclass of int in Python, so an unguarded branch would
    # hash True and 1 identically and hide a flipped flag.
    assert fp.canonical(True) != fp.canonical(1)


def test_nan_and_inf_are_representable():
    assert fp.canonical(float("nan")) == "nan"
    assert fp.canonical(math.inf) == "inf"
    assert fp.canonical(-math.inf) == "-inf"


def test_list_order_is_significant():
    # Unlike dict keys, a sequence's order is part of the value.
    assert fp.fingerprint({"x": [1, 2]}) != fp.fingerprint({"x": [2, 1]})


def test_line_is_one_line_and_names_the_phase():
    out = fp.line(3, {"net": 1.0})
    assert "\n" not in out
    assert out.startswith("FINGERPRINT phase=3 ")
    assert fp.fingerprint({"net": 1.0}) in out
