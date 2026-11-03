"""validate_url_entry / validate_object_name (the PHP side is compared in test_validation_parity.py)."""

import pytest

from pac_sync.normalizer import validate_object_name, validate_url_entry, validate_value


@pytest.mark.parametrize("value", [
    ".partner-example.com", "partner-example.com", "tools.example.net/upload", "a.b.example.org/x/y_z-1.2~3%20",
    "reviews.example.com/", "x.co.uk", "partner.co.uk/a",
])
def test_url_entry_accepts(value):
    assert validate_url_entry(value) is None


@pytest.mark.parametrize("value", [
    "", " a.com", "a.com ", "exämple.com", "a b.com", 'a.com/"x', "a.com/x;y", "a.com\\x",
    "*.example.com", "a.com/*", "a.com?x=1", "a.com/x#f", "https://a.com", "a.com:8080", "localhost", ".com", "com",
    "co.uk", ".co.uk", "..a.com", "-a.com", "a..com", "a.com/A", "a.com/x y", "a.com/é", "a.com/x?y",
    "a" * 250 + ".com", "a.com/" + "x" * 250, "a.com\n", "a.com/x\x00",
])
def test_url_entry_rejects(value):
    assert validate_url_entry(value) is not None


def test_url_entry_length_limit_is_255():
    path = "/" + "a" * (255 - len("a.com") - 1)
    assert validate_url_entry("a.com" + path) is None
    assert validate_url_entry("a.com" + path + "a") is not None


@pytest.mark.parametrize("name,ok", [
    ("partner-sites", True), ("a", True), ("0abc", True), ("a" * 26, True), ("a" * 27, False), ("", False),
    ("-a", False), ("A", False), ("a_b", False), ("a b", False), ("a.b", False), ("a\n", False), ("é", False),
])
def test_object_name(name, ok):
    assert (validate_object_name(name) is None) is ok


def test_validate_value_is_unchanged():
    assert validate_value("host_exact", "a b") == "value must be printable ASCII (no spaces, control or non-ASCII characters)"
    assert validate_value("host_exact", 'a"b') == "value contains a forbidden character (quote, semicolon, backslash or whitespace)"
    assert validate_value("host_exact", "") == "empty value"
