"""normalizer.validate_value and snow-mock's pac_validate_value must accept and refuse the same values.

Runs the PHP side through php.exe; skipped when PHP or ..\\snow-mock is not there.
"""

import glob
import json
import os
import shutil
import subprocess

import pytest

from pac_sync.normalizer import validate_value

from .conftest import ROOT

VALIDATION_PHP = ROOT.parent / "snow-mock" / "src" / "validation.php"

CASES = [
    # host_exact
    ("host_exact", "intranet.corp.local"), ("host_exact", "a"), ("host_exact", "Intranet.corp"),
    ("host_exact", "-bad.corp"), ("host_exact", "bad-.corp"), ("host_exact", "a..b"), ("host_exact", "*.corp"),
    ("host_exact", "a.corp:8080"), ("host_exact", "exämple.com"), ("host_exact", "a" * 64 + ".com"),
    ("host_exact", ".".join(["a" * 63] * 4)), ("host_exact", "a b"), ("host_exact", "a\n"), ("host_exact", ""),
    # domain_suffix
    ("domain_suffix", "corp.local"), ("domain_suffix", "local"), ("domain_suffix", "co.uk"),
    ("domain_suffix", "partner.co.uk"), ("domain_suffix", ".corp.local"), ("domain_suffix", "*.corp.local"),
    ("domain_suffix", "ma"), ("domain_suffix", "x.ma"),
    # ip_cidr
    ("ip_cidr", "10.20.0.0/16"), ("ip_cidr", "10.20.0.1/16"), ("ip_cidr", "10.0.0.0/7"), ("ip_cidr", "10.0.0.1/32"),
    ("ip_cidr", "10.0.0.0/33"), ("ip_cidr", "256.0.0.0/8"), ("ip_cidr", "010.0.0.0/8"), ("ip_cidr", "10.0.0.0/08"),
    ("ip_cidr", "10.0.0.0"), ("ip_cidr", "::1/128"), ("ip_cidr", "10.0.0.0/16/1"),
    # url_pattern
    ("url_pattern", "https://sso.partner.com/*"), ("url_pattern", "http://a.com:8080/x"), ("url_pattern", "ftp://a.com"),
    ("url_pattern", "https://*.*.*.a.com/*"), ("url_pattern", "https://a.com/*/*/*"), ("url_pattern", "https://A.com/"),
    ("url_pattern", "https://a.com:123456/"), ("url_pattern", "https://a.com:١٢/"),
    ("url_pattern", "https://a.com/x y"), ("url_pattern", "https://a.com/x y"), ("url_pattern", "https://a.com"),
    # control characters, one per family where Python and PHP regexes differ
    ("host_exact", "a.com\x00"), ("url_pattern", "https://a.com/x\x0by"), ("url_pattern", "https://a.com/x\x1cy"),
    ("url_pattern", "https://a.com/x\x7fy"),
    # other
    ("regex", "a.com"),
]


def find_php():
    for path in sorted(glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\PHP.PHP.*\php.exe")),
                       reverse=True):
        return path
    return shutil.which("php")


def test_python_and_php_validators_agree():
    php = find_php()
    if not php or not VALIDATION_PHP.exists():
        pytest.skip("needs php and ../snow-mock")
    script = ("require $argv[1]; $out = [];"
              "foreach (json_decode(stream_get_contents(STDIN), true) as [$t, $v]) { $out[] = pac_validate_value($t, $v); }"
              "echo json_encode($out);")
    proc = subprocess.run([php, "-r", script, str(VALIDATION_PHP)], input=json.dumps(CASES), capture_output=True,
                          text=True, encoding="utf-8", check=True)
    php_errors = json.loads(proc.stdout)
    mismatches = [(t, v, validate_value(t, v), e) for (t, v), e in zip(CASES, php_errors)
                  if (validate_value(t, v) is None) != (e is None)]
    assert not mismatches, "\n".join(f"{t} {v!r}: python={p!r} php={e!r}" for t, v, p, e in mismatches)
