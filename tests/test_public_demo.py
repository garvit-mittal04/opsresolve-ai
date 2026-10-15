"""The public portfolio demo (demo/streamlit_app.py): bundled synthetic sample only, no uploads, no AI.

UI checks use Streamlit's AppTest like tests/test_app_ui.py; repository checks read files only.
"""

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="install the app extra: pip install -e '.[app]'")
from packaging.requirements import Requirement  # noqa: E402  (a Streamlit dependency)
from packaging.version import Version  # noqa: E402
from streamlit import config as streamlit_config  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

import opsresolve.ai as ai  # noqa: E402
import opsresolve.webapp as webapp  # noqa: E402
from opsresolve.config import MatchConfig  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "demo" / "streamlit_app.py"
APP = REPO / "app.py"
FAKE_KEY = "sk-ant-test-key-never-sent-0123456789"  # fake; nothing may ever read it in the demo


@pytest.fixture(autouse=True)
def hosted_settings():
    """No server address, as on a hosting platform; options the tests or the demo change are restored."""
    saved = {name: streamlit_config.get_option(name) for name in ("server.address", "client.showErrorDetails")}
    streamlit_config.set_option("server.address", None)
    yield
    for name, value in saved.items():
        streamlit_config.set_option(name, value)


def bind_locally() -> None:
    """As launched with --server.address localhost, which the local app requires."""
    streamlit_config.set_option("server.address", "localhost")


def start(path: Path = DEMO) -> AppTest:
    return AppTest.from_file(str(path), default_timeout=30).run()


def run(at: AppTest) -> AppTest:
    return at.button(key="run_button").click().run()


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def no_errors(at: AppTest) -> None:
    assert list(at.exception) == [], [e.value for e in at.exception]


def shown_text(at: AppTest) -> str:
    groups = (at.markdown, at.caption, at.success, at.error, at.info, at.warning, at.text, at.code)
    return "\n".join(e.value for group in groups for e in group)


def finding_ids(result) -> list[tuple]:
    return [(e.exception_id, e.exception_type, e.po_number, e.sku, e.amount_impact, e.quantity_difference,
             e.severity, e.summary) for e in result.exceptions]


@pytest.fixture
def no_ai(monkeypatch):
    """A configured key and an 'installed' SDK, but every AI entry point fails the test if reached."""
    monkeypatch.setenv(ai.API_KEY_VARIABLE, FAKE_KEY)
    monkeypatch.setattr(ai, "sdk_available", lambda: True)
    monkeypatch.setattr(ai, "_import_sdk", lambda: pytest.fail("the SDK was loaded in the public demo"))
    for name in ("ai_status", "preview_ai_request", "request_ai_explanation"):
        monkeypatch.setattr(webapp, name, lambda *a, _n=name, **k: pytest.fail(f"webapp.{_n} called in the demo"))


# ---- only the bundled synthetic sample --------------------------------------------------------------------------

def test_demo_shows_the_synthetic_data_notice_and_offers_only_the_sample():
    at = start()
    no_errors(at)
    assert at.title[0].value == "OpsResolve"
    notice = " ".join(i.value for i in at.info)
    assert "Public portfolio demo: synthetic data only" in notice
    assert "not approved for real supplier data, payment decisions or any real financial transaction" in notice
    assert "data/sample/" in " ".join(c.value for c in at.caption)
    assert not at.radio  # no choice of data source
    assert not at.button(key="run_button").disabled  # the sample is ready to run


def test_demo_says_so_when_the_bundled_sample_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "SAMPLE_DIR", tmp_path)  # as in an installation without the data folder
    at = start()
    no_errors(at)
    assert "bundled sample data is missing" in at.error[0].value
    assert at.button(key="run_button").disabled and at.get("file_uploader") == []


def test_demo_has_no_upload_template_or_profile_widgets_before_or_after_a_run():
    at = start()
    assert at.get("file_uploader") == []
    assert [d.key for d in at.download_button] == []  # no input templates or example profile either
    at = run(at)
    no_errors(at)
    assert at.get("file_uploader") == []
    assert {d.key for d in at.download_button} == {"dl_exceptions", "dl_validation", "dl_report"}


def test_demo_runs_the_bundled_sample_with_the_documented_counts():
    at = run(start())
    no_errors(at)
    assert metrics(at) == {"Rows read": "62", "Validation issues": "3", "Exceptions": "18",
                           "High severity": "8", "Medium severity": "7", "Low severity": "3"}
    assert [t.label for t in at.tabs] == ["Exceptions (18)", "Validation issues (3)", "Downloads"]


def test_demo_results_are_the_local_apps_sample_results_and_the_engines():
    demo = run(start()).session_state["last_run"]["result"]
    bind_locally()
    local_at = start(APP)
    local_at = run(local_at.radio(key="source").set_value("Use bundled sample data").run())
    local = local_at.session_state["last_run"]["result"]
    direct = webapp.run_reconciliation(webapp.sample_uploads(), MatchConfig())
    assert finding_ids(demo) == finding_ids(local) == finding_ids(direct)
    assert len(demo.exceptions) == 18
    assert [(i.file, i.row, i.field) for i in demo.dataset.issues] == \
           [(i.file, i.row, i.field) for i in direct.dataset.issues]
    # Same deterministic report content as the local app (apart from the generation time).
    def report(result) -> list[str]:
        return [line for line in webapp.summary_markdown(result).decode("utf-8").splitlines()
                if not line.startswith("Generated:")]
    assert report(demo) == report(local)
    assert webapp.exceptions_csv(demo) == webapp.exceptions_csv(local)


# ---- no AI, whatever is configured ------------------------------------------------------------------------------

def test_demo_never_offers_or_prepares_ai_even_with_a_key_configured(no_ai):
    at = run(start())
    for option in at.selectbox(key="detail_id").options:  # every finding's detail panel
        at = at.selectbox(key="detail_id").set_value(option).run()
        no_errors(at)
        assert not at.toggle and not at.checkbox
        assert [b.key for b in at.button if b.key and b.key.startswith("ai_")] == []
        assert "AI explanation" not in " ".join(m.value for m in at.markdown)
    assert "Not available in the public demo" in " ".join(c.value for c in at.sidebar.caption)
    assert "ai_enabled" not in at.session_state and "ai_views" not in at.session_state
    assert FAKE_KEY not in shown_text(at)


def test_demo_ai_cannot_be_switched_on_through_session_state(no_ai):
    at = start()
    at.session_state["ai_enabled"] = True  # a crafted widget state has no effect: there is no toggle to read it
    at = run(at.run())
    no_errors(at)
    assert not at.toggle
    assert [b.key for b in at.button if b.key and b.key.startswith("ai_")] == []


# ---- isolation, protections and local mode -----------------------------------------------------------------------

def test_each_browser_session_keeps_its_own_results():
    first = run(start())
    second = start()
    assert "last_run" in first.session_state
    assert "last_run" not in second.session_state and not second.metric
    second = second.number_input(key="cfg_quantity_tolerance").set_value(5.0).run()
    second = run(second)
    assert first.session_state["last_run"]["result"] is not second.session_state["last_run"]["result"]
    assert first.session_state["last_run"]["result"].config.quantity_tolerance == 0
    assert metrics(first.run())["Exceptions"] == "18"  # unaffected by the other session's settings


def test_demo_downloads_use_the_formula_protected_builders(monkeypatch):
    seen = []
    for name in ("exceptions_csv", "validation_csv", "summary_markdown"):
        real = getattr(webapp, name)
        monkeypatch.setattr(webapp, name, lambda result, _real=real, _name=name: seen.append(_name) or _real(result))
    at = run(start())
    assert set(seen) == {"exceptions_csv", "validation_csv", "summary_markdown"}
    result = at.session_state["last_run"]["result"]
    csv_text = webapp.exceptions_csv(result).decode("utf-8")
    # The builders neutralise formula cells (tested in tests/test_webapp.py); a plain negative amount stays a number.
    assert ",-50.00," in csv_text and ",'-50.00," not in csv_text


def test_local_app_keeps_uploads_the_import_profile_and_the_ai_toggle():
    bind_locally()
    at = start(APP)
    no_errors(at)
    assert at.radio(key="source").options == ["Upload my files", "Use bundled sample data"]
    assert len(at.get("file_uploader")) == 5  # four data files and the import profile
    assert at.toggle(key="ai_enabled").value is False
    assert not any("Public portfolio demo" in i.value for i in at.info)


# ---- entrypoint, hosting configuration and repository hygiene ----------------------------------------------------

def test_demo_entrypoint_only_selects_the_demo_mode():
    source = DEMO.read_text(encoding="utf-8")
    assert source.rstrip().endswith("app.main(public_demo=True)")
    for forbidden in ("file_uploader", "unsafe_allow_html", "opsresolve.ai", "import ai", "secrets", "environ"):
        assert forbidden not in source


@pytest.mark.parametrize("address", [None, "localhost"])
def test_demo_runs_unbound_as_when_hosted_and_bound_locally(address):
    streamlit_config.set_option("server.address", address)
    at = run(start())
    no_errors(at)
    assert metrics(at)["Exceptions"] == "18" and not at.error


def test_demo_hides_error_details_from_visitors_without_a_second_config_file():
    assert streamlit_config.get_option("client.showErrorDetails") != "none"
    run(start())
    assert streamlit_config.get_option("client.showErrorDetails") == "none"  # set by app.main(public_demo=True)
    # Community Cloud reads only the repository root's .streamlit/config.toml, so the demo has none of its own.
    assert not (REPO / "demo" / ".streamlit").exists()


def pyproject_requirements() -> dict[str, Requirement]:
    """The core dependencies and the app extra declared in pyproject.toml (read without tomllib, absent on 3.10)."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    core = re.search(r"^dependencies = \[(.*?)\]", text, re.M | re.S).group(1)
    app = re.search(r"^app = \[(.*?)\]", text, re.M | re.S).group(1)
    specs = re.findall(r'"([^"]+)"', core + app)
    return {Requirement(spec).name: Requirement(spec) for spec in specs}


def test_hosting_requirements_install_the_project_and_pin_within_its_declared_ranges():
    lines = [line.strip() for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    # Editable (the bundled sample is found next to the source tree), with the app extra from pyproject.toml.
    assert lines[0] == "-e .[app]"
    pins = {Requirement(line).name: Requirement(line) for line in lines[1:]}
    declared = pyproject_requirements()
    assert set(pins) == set(declared) == {"streamlit", "pandas", "pydantic"}  # nothing pyproject.toml lacks
    for name, pin in pins.items():
        (spec,) = pin.specifier
        assert spec.operator == "==", name
        assert Version(spec.version) in declared[name].specifier, f"{name} {spec.version} outside {declared[name]}"
    assert not any("anthropic" in line or "ai]" in line for line in lines)  # no AI extra, alone or combined


def test_no_other_dependency_file_competes_with_requirements_txt():
    # Community Cloud uses the first dependency file found in the entrypoint's folder, then the root, preferring
    # uv.lock, then requirements.txt, then pyproject.toml.
    others = ("uv.lock", "Pipfile", "Pipfile.lock", "environment.yml", "environment.yaml", "requirements.txt",
              "pyproject.toml", "packages.txt")
    assert [name for name in others if (REPO / "demo" / name).exists()] == []
    assert [name for name in others if (REPO / name).exists()] == ["requirements.txt", "pyproject.toml"]


SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "reports", "build", "dist", "node_modules"}
SECRET_PATTERNS = [
    re.compile(r"sk-ant-(?:api|admin)\d\d-[A-Za-z0-9_\-]{20,}"),  # Anthropic API or admin keys
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key ids
]


def repository_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in files:
            yield Path(root) / name


def test_no_secrets_or_secret_files_are_in_the_repository():
    for path in repository_files():
        relative = path.relative_to(REPO).as_posix()
        assert path.name != "secrets.toml", relative
        assert not (path.name.startswith(".env") and path.name != ".env.example"), relative
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pyc"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(text), f"{relative} matches {pattern.pattern}"


def test_secret_files_are_git_ignored():
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "secrets.toml" in ignored and ".env" in ignored


def test_the_demo_reads_only_the_bundled_synthetic_sample():
    assert webapp.SAMPLE_DIR == REPO / "data" / "sample"  # fails for a non-editable install, which has no sample
    uploads = webapp.sample_uploads()
    sample = REPO / "data" / "sample"
    for upload in uploads.values():
        if upload is not None:
            assert (sample / Path(upload.name).name).read_bytes() == upload.data
    readme = (sample / "README.md").read_text(encoding="utf-8").lower()
    assert "synthetic" in readme and "fictional" in readme
