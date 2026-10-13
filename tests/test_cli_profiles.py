"""CLI profile selection and report provenance (milestone 7, stage 3).

Expected values are derived by hand from the fixtures in tests/fixtures/profiles/.
"""

import csv
import dataclasses
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from opsresolve import profiles
from opsresolve.cli import main
from opsresolve.pipeline import investigate_files
from opsresolve.profiles import ProfileError, load_profile, parse_profile
from opsresolve.reporting import render_console, render_markdown, write_reports

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
ERP_DIR = FIXTURES / "erp"
ERP_PROFILE = FIXTURES / "erp_profile.json"
ERP_NAME = "Fictional ERP export (test fixture)"
FILES = ("purchase_orders", "invoices", "goods_received")


def short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def run_cli(capsys, *args):
    code = main([str(a) for a in args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def write_profile(tmp_path: Path, body: dict, name: str = "profile.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# ---- selecting a profile ---------------------------------------------------------------------------

def test_cli_reads_the_fixture_export_with_the_selected_profile(tmp_path, capsys):
    out_dir = tmp_path / "out"
    code, out, err = run_cli(capsys, "--data-dir", ERP_DIR, "--profile", ERP_PROFILE, "--output-dir", out_dir)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == f"Import profile: {ERP_NAME} (sha256 {short_hash(ERP_PROFILE.read_bytes())})"
    # uom_conversions.csv was found in --data-dir and read with the profile (';', mapped headers).
    assert lines[1:5] == [
        "purchase_orders  rows=2    accepted=2    rejected=0",
        "invoices         rows=2    accepted=2    rejected=0",
        "goods_received   rows=2    accepted=2    rejected=0",
        "uom_conversions  rows=1    accepted=1    rejected=0",
    ]
    assert "validation issues: 0" in lines and "exceptions: 2" in lines
    with (out_dir / "exceptions.csv").open(encoding="utf-8") as handle:
        amounts = {(r["exception_type"], r["sku"]): r["amount_impact"] for r in csv.DictReader(handle)}
    # (12.75 - 12.50) x 1200 and (10 - 8) x 1234.56, as in the stage 2 tests.
    assert amounts == {("PRICE_MISMATCH", "A-100"): "300.00", ("QUANTITY_MISMATCH", "B-200"): "2469.12"}


def test_profile_applies_to_an_automatically_found_conversion_file(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in FILES:
        (data_dir / f"{name}.csv").write_bytes((ERP_DIR / f"{name}.csv").read_bytes())
    # A comma-separated conversion file does not match the profile's ';' and mapped headers.
    (data_dir / "uom_conversions.csv").write_bytes((FIXTURES / "canonical" / "uom_conversions.csv").read_bytes())
    args = ("--data-dir", data_dir, "--profile", ERP_PROFILE, "--output-dir", tmp_path / "out")
    code, out, _ = run_cli(capsys, *args)
    assert code == 0
    assert "uom_conversions  rows=0    accepted=0    rejected=0" in out.splitlines()
    with (tmp_path / "out" / "validation_issues.csv").open(encoding="utf-8") as handle:
        issues = list(csv.DictReader(handle))
    assert [(i["file"], i["field"]) for i in issues] == [
        ("uom_conversions", "sku"), ("uom_conversions", "uom"),
        ("uom_conversions", "base_uom"), ("uom_conversions", "units_per_uom"),
        ("uom_conversions", ""),  # the wrong-delimiter hint: the header holds ',' but ';' was declared
    ]
    assert issues[-1]["message"].startswith("The header was read as a single column using the declared "
                                            "delimiter ';', but it contains ','")
    assert run_cli(capsys, *args, "--fail-on-exceptions")[0] == 1


def test_profile_file_problems_found_while_loading_count_for_fail_on_exceptions(tmp_path, capsys):
    # The ERP profile's mapped headers are absent from the canonical files.
    args = ("--data-dir", FIXTURES / "canonical", "--profile", ERP_PROFILE, "--output-dir", tmp_path)
    code, out, _ = run_cli(capsys, *args)
    assert code == 0 and "purchase_orders  rows=0    accepted=0    rejected=0" in out
    assert run_cli(capsys, *args, "--fail-on-exceptions")[0] == 1


# ---- invalid or unreadable profiles ----------------------------------------------------------------

def _bad_profiles(tmp_path: Path) -> dict[str, tuple[Path, str]]:
    bad = {}
    (tmp_path / "malformed.json").write_text('{"profile_version": 1,', encoding="utf-8")
    bad["malformed"] = (tmp_path / "malformed.json", "profile file: is not valid JSON")
    bad["missing"] = (tmp_path / "missing.json", "profile file: cannot be read (No such file or directory)")
    bad["unknown key"] = (write_profile(tmp_path, {"profile_version": 1, "name": "x", "colour": "red"}, "u.json"),
                          "profile: unknown key 'colour'")
    bad["separator clash"] = (
        write_profile(tmp_path, {"profile_version": 1, "name": "x",
                                 "defaults": {"thousands_separator": ","}}, "clash.json"),
        "defaults: the delimiter ',' must not also be a decimal or thousands separator")
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (profiles.MAX_PROFILE_BYTES + 1))
    bad["oversized"] = (big, f"profile file: is {profiles.MAX_PROFILE_BYTES + 1} bytes; the limit is")
    folder = tmp_path / "folder.json"
    folder.mkdir()
    bad["directory"] = (folder, "profile file: cannot be read")
    return bad


@pytest.mark.parametrize("case", ["malformed", "missing", "unknown key", "separator clash", "oversized",
                                  "directory"])
def test_invalid_profile_exits_2_with_errors_on_stderr_and_writes_nothing(tmp_path, capsys, case):
    path, problem = _bad_profiles(tmp_path)[case]
    out_dir = tmp_path / "out"
    code, out, err = run_cli(capsys, "--data-dir", ERP_DIR, "--profile", path, "--output-dir", out_dir)
    assert code == 2
    assert out == ""
    assert err.startswith(f"{path}: Invalid import profile:\n  - ")
    assert problem in err
    assert not out_dir.exists()


def test_every_profile_problem_is_listed(tmp_path, capsys):
    path = write_profile(tmp_path, {"profile_version": 2, "name": "", "defaults": {"delimiter": ":"}})
    code, _, err = run_cli(capsys, "--profile", path, "--output-dir", tmp_path / "out")
    assert code == 2
    assert [line for line in err.splitlines() if line.startswith("  - ")] == [
        "  - name: must be a non-empty string",
        "  - profile_version: must be 1",
        "  - defaults.delimiter: ':' is not supported (use one of ',', ';', '\\t', '|')",
    ]


class FakeStream:
    """A device-like file (see fake_device) serving ``content``, or spaces without end when it is None.

    It never serves more than MAX_PROFILE_BYTES + 1 bytes in total: any read that would go past that
    (an unbounded read(), one oversized read, or a chunked loop with no cap) fails the test at once,
    so a regressed implementation can neither hang the suite nor exhaust memory.
    """

    CAP = profiles.MAX_PROFILE_BYTES + 1

    def __init__(self, content: bytes | None = None):
        self.content = content
        self.served = 0
        self.requests: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        left = None if self.content is None else len(self.content) - self.served
        if size is None or size < 0:
            if left is None:
                raise AssertionError("read past the limit")
            size = left
        elif left is not None:
            size = min(size, left)
        if self.served + size > self.CAP:
            raise AssertionError("read past the limit")
        start, self.served = self.served, self.served + size
        return b" " * size if self.content is None else self.content[start:self.served]


def fake_device(monkeypatch, path: Path, stream: FakeStream) -> None:
    """Make ``path`` look like a character device of size 0 that reads from ``stream``."""
    real_stat, real_open = Path.stat, Path.open

    def fake_stat(self, *args, **kwargs):
        if self == path:
            return os.stat_result((stat.S_IFCHR | 0o666, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        return real_stat(self, *args, **kwargs)

    def fake_open(self, *args, **kwargs):
        return stream if self == path else real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(Path, "open", fake_open)


def test_an_endless_stream_is_read_only_to_one_byte_past_the_limit(tmp_path, monkeypatch):
    device = tmp_path / "endless"
    stream = FakeStream()
    fake_device(monkeypatch, device, stream)
    with pytest.raises(ProfileError) as err:
        load_profile(device)
    assert err.value.problems == [f"profile file: is longer than the limit of {profiles.MAX_PROFILE_BYTES} bytes"]
    assert stream.requests == [profiles.MAX_PROFILE_BYTES + 1]
    assert stream.served == profiles.MAX_PROFILE_BYTES + 1


def padded_profile(size: int) -> bytes:
    """A valid profile, padded with trailing spaces (still valid JSON) to exactly ``size`` bytes."""
    body = json.dumps({"profile_version": 1, "name": "Boundary"}).encode()
    return body + b" " * (size - len(body))


def test_a_valid_profile_of_exactly_the_limit_loads_from_a_stream(tmp_path, monkeypatch):
    data = padded_profile(profiles.MAX_PROFILE_BYTES)
    assert len(data) == 65_536
    device = tmp_path / "stream"
    stream = FakeStream(data)
    fake_device(monkeypatch, device, stream)
    profile = load_profile(device)
    assert profile.name == "Boundary"
    assert profile.sha256 == hashlib.sha256(data).hexdigest()
    assert stream.requests == [profiles.MAX_PROFILE_BYTES + 1]


def test_the_same_profile_one_byte_longer_is_rejected_for_size(tmp_path, monkeypatch):
    data = padded_profile(profiles.MAX_PROFILE_BYTES) + b" "
    assert len(data) == 65_537
    assert json.loads(data) == {"profile_version": 1, "name": "Boundary"}  # still valid JSON: only the size is wrong
    device = tmp_path / "stream"
    fake_device(monkeypatch, device, FakeStream(data))
    with pytest.raises(ProfileError) as err:
        load_profile(device)
    assert err.value.problems == [f"profile file: is longer than the limit of {profiles.MAX_PROFILE_BYTES} bytes"]


def test_cli_rejects_an_endless_stream_with_exit_2_and_no_reports(tmp_path, capsys, monkeypatch):
    device = tmp_path / "endless"
    fake_device(monkeypatch, device, FakeStream())
    out_dir = tmp_path / "out"
    code, out, err = run_cli(capsys, "--profile", device, "--output-dir", out_dir)
    assert (code, out) == (2, "")
    assert f"profile file: is longer than the limit of {profiles.MAX_PROFILE_BYTES} bytes" in err
    assert not out_dir.exists()


def test_invalid_configuration_is_still_checked_first(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPSRESOLVE_PRICE_TOLERANCE_PCT", "-1")
    code, _, err = run_cli(capsys, "--profile", tmp_path / "missing.json", "--output-dir", tmp_path / "out")
    assert code == 2 and err.startswith("Invalid OPSRESOLVE_* configuration")


# ---- provenance -------------------------------------------------------------------------------------

def erp_result(profile=None):
    profile = profile or load_profile(ERP_PROFILE)
    return investigate_files(*[ERP_DIR / f"{n}.csv" for n in FILES], profile=profile,
                             uom_conversions_path=ERP_DIR / "uom_conversions.csv")


def test_result_carries_the_profile_it_was_loaded_with():
    profile = load_profile(ERP_PROFILE)
    result = erp_result(profile)
    assert result.profile is profile and result.dataset.profile is profile


def test_markdown_provenance_line_follows_the_rules_line():
    result = erp_result()
    lines = render_markdown(result).splitlines()
    rules = next(i for i, line in enumerate(lines) if line.startswith("Rules: "))
    digest = short_hash(ERP_PROFILE.read_bytes())
    expected = f"Import profile: Fictional ERP export \\(test fixture\\) (sha256 {digest})"
    assert lines[rules + 1:rules + 4] == ["", expected, ""]
    assert sum(line.startswith("Import profile:") for line in lines) == 1


def test_profile_name_is_escaped_in_markdown_but_not_changed(tmp_path):
    name = "ERP *v2* [beta](http://x) <b>|#_~`"
    data = json.dumps({"profile_version": 1, "name": name}).encode()
    profile = parse_profile(data)
    base = erp_result()
    result = dataclasses.replace(base, dataset=dataclasses.replace(base.dataset, profile=profile))
    assert profile.name == name and profile.sha256 == hashlib.sha256(data).hexdigest()
    markdown = render_markdown(result)
    line = next(line for line in markdown.splitlines() if line.startswith("Import profile:"))
    assert line == ("Import profile: ERP \\*v2\\* \\[beta\\]\\(http\\://x\\) \\<b\\>\\|\\#\\_\\~\\` "
                    f"(sha256 {short_hash(data)})")
    assert render_console(result).splitlines()[0] == f"Import profile: {name} (sha256 {short_hash(data)})"


def test_hash_is_taken_from_the_original_profile_bytes(tmp_path):
    body = json.dumps({"profile_version": 1, "name": "BOM"}).encode()
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + body)
    profile = load_profile(path)
    assert profile.sha256 == hashlib.sha256(b"\xef\xbb\xbf" + body).hexdigest()
    assert profile.sha256 != hashlib.sha256(body).hexdigest()


def test_csv_columns_are_unchanged_with_a_profile(tmp_path):
    plain = write_reports(investigate_files(*[FIXTURES / "canonical" / f"{n}.csv" for n in FILES]),
                          tmp_path / "plain")
    profiled = write_reports(erp_result(), tmp_path / "profiled")
    for name in ("exceptions_csv", "validation_csv"):
        first = plain[name].read_text(encoding="utf-8").splitlines()[0]
        assert profiled[name].read_text(encoding="utf-8").splitlines()[0] == first


# ---- no profile: unchanged ---------------------------------------------------------------------------

@pytest.mark.parametrize("folder", ["data/sample", "data/evaluation"])
def test_provenance_lines_are_the_only_difference_a_profile_makes_to_reports(folder):
    """Same result rendered with and without a profile attached: only the provenance lines differ."""
    root = Path(__file__).resolve().parents[1] / folder
    plain = investigate_files(*[root / f"{n}.csv" for n in FILES])
    assert plain.profile is None
    markdown, console = render_markdown(plain), render_console(plain)
    assert "Import profile" not in markdown and "Import profile" not in console

    profile = load_profile(ERP_PROFILE)
    attached = dataclasses.replace(plain, dataset=dataclasses.replace(plain.dataset, profile=profile))
    line = f"Import profile: {ERP_NAME} (sha256 {profile.sha256[:12]})"
    assert render_console(attached) == line + "\n" + console
    escaped = line.replace("(test fixture)", "\\(test fixture\\)")
    rules = next(x for x in markdown.splitlines(keepends=True) if x.startswith("Rules: "))
    assert render_markdown(attached) == markdown.replace(rules + "\n", rules + "\n" + escaped + "\n\n", 1)


def test_cli_without_a_profile_prints_no_provenance(tmp_path, capsys, sample_dir):
    code, out, err = run_cli(capsys, "--data-dir", sample_dir, "--output-dir", tmp_path)
    assert code == 0 and err == ""
    assert out.splitlines()[0].startswith("purchase_orders  rows=")
    assert "Import profile" not in out
    assert "Import profile" not in (tmp_path / "exception_report.md").read_text(encoding="utf-8")
