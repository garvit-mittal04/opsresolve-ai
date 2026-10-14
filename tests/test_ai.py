"""Optional AI provider (Milestone 8, Stage B). Every test is offline: the model is a fake or a mocked transport.

Tests against the real Anthropic SDK are skipped unless the ``ai`` extra is installed (CI installs it).
"""

from __future__ import annotations

import ast
import copy
import importlib
import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from factories import grn, inv, po
from opsresolve import ai
from opsresolve.ai import (
    API_BASE_URL,
    API_KEY_VARIABLE,
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    TRANSMITTED_FIELDS,
    AIConfig,
    AnthropicProvider,
    ExplanationError,
    ProviderResponse,
    explain_exception,
    preview_request,
    known_supplier_ids,
    pseudonymise,
)
from opsresolve.explanation import DIRECTIONS, ExplanationOutput, build_evidence_pack, pack_sha256
from opsresolve.ingestion import Dataset, LoadResult
from opsresolve.models import ExceptionType as T
from opsresolve.pipeline import investigate, investigate_files

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample"
KEY = "sk-ant-test-key-never-sent-0123456789"
NAMES = ("Northgate", "Bramwell", "Calder", "Delmore", "Fenwick")


class NetworkBlocked(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test in this module may open a real connection (the real-SDK tests use a mocked transport)."""
    def blocked(*args, **kwargs):
        raise NetworkBlocked("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture(scope="module")
def sample():
    return investigate_files(SAMPLE / "purchase_orders.csv", SAMPLE / "invoices.csv", SAMPLE / "goods_received.csv")


def find(result, exc_type, po_number):
    [found] = [e for e in result.exceptions if e.exception_type is exc_type and e.po_number == po_number]
    return found


@pytest.fixture(scope="module")
def supplier_case(sample):
    """PO 4500010015 raised with SUP-001 (Northgate Fasteners Ltd); invoice RVX-00981 from SUP-009."""
    return sample, find(sample, T.SUPPLIER_MISMATCH, "4500010015")


def valid_answer(exception_id: str, supplier_text: str = "SUPPLIER-2") -> dict:
    """A correct answer for the supplier mismatch, written against the pseudonymised pack."""
    return {
        "exception_id": exception_id, "direction": "not_applicable",
        "explanation": f"Invoice RVX-00981 is from supplier {supplier_text}, but PO 4500010015 was raised with "
                       f"supplier SUPPLIER-1 (E1, E2).",
        "confirmed_facts": [{"statement": f"Invoice RVX-00981 is from supplier {supplier_text}.",
                             "evidence_refs": ["E2"]},
                            {"statement": "PO 4500010015 was raised with supplier SUPPLIER-1.", "evidence_refs": ["E1"]}],
        "possible_causes": [{"hypothesis": "The invoice may have been keyed against the wrong supplier.",
                             "status": "unverified_hypothesis", "supporting_refs": ["E2"],
                             "how_to_check": "Compare invoice RVX-00981 with the vendor master."}],
        "investigation_steps": [{"step": "Check the remittance details on invoice RVX-00981.", "evidence_refs": ["E2"]}],
        "evidence_refs_used": ["E1", "E2"], "sufficiency": "partial",
        "missing_information": ["The vendor master record."],
    }


class FakeProvider:
    def __init__(self, text: str | None = None, error: ExplanationError | None = None):
        self.text, self.error, self.requests = text, error, []

    def generate(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ProviderResponse(self.text, "req_fake")


# ---- a fake SDK with the real SDK's exception hierarchy (see anthropic/_exceptions.py) ------------------------

class FakeAPIError(Exception):
    pass


class FakeStatusError(FakeAPIError):
    def __init__(self, status_code: int, request_id: str | None = "req_err"):
        super().__init__(f"HTTP {status_code}: secret details that must not be copied")
        self.status_code, self.request_id = status_code, request_id


class FakeConnectionError(FakeAPIError):
    pass


class FakeTimeoutError(FakeConnectionError):
    pass


def status_class(name):
    return type(name, (FakeStatusError,), {})


class FakeClient:
    def __init__(self, response=None, error: Exception | None = None):
        self.response, self.error, self.calls = response, error, []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def fake_sdk(client: FakeClient | None = None):
    created = []

    def anthropic(**kwargs):
        created.append(kwargs)
        return client or FakeClient()

    return SimpleNamespace(
        APIError=FakeAPIError, APIStatusError=FakeStatusError, APIConnectionError=FakeConnectionError,
        APITimeoutError=FakeTimeoutError, AuthenticationError=status_class("AuthenticationError"),
        PermissionDeniedError=status_class("PermissionDeniedError"), RateLimitError=status_class("RateLimitError"),
        BadRequestError=status_class("BadRequestError"), NotFoundError=status_class("NotFoundError"),
        InternalServerError=status_class("InternalServerError"), Anthropic=anthropic, created=created)


def message(text: str | None, stop_reason: str = "end_turn", thinking: bool = True):
    content = [SimpleNamespace(type="thinking", thinking="")] if thinking else []
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(content=content, stop_reason=stop_reason, _request_id="req_ok")


# ---- configuration --------------------------------------------------------------------------------------------

def test_config_defaults_and_environment_overrides():
    # Thinking counts toward max_tokens on claude-opus-5-5, so the cap leaves room for it.
    assert AIConfig() == AIConfig(model="claude-opus-5-5", effort="medium", timeout_seconds=300, max_retries=2,
                                  max_tokens=16000)
    config = AIConfig.from_env({"OPSRESOLVE_AI_MODEL": "claude-sonnet-5-5", "OPSRESOLVE_AI_EFFORT": "low",
                                "OPSRESOLVE_AI_TIMEOUT_SECONDS": "30", "OPSRESOLVE_AI_MAX_RETRIES": "0",
                                "OPSRESOLVE_AI_MAX_TOKENS": "2000", API_KEY_VARIABLE: KEY})
    assert (config.model, config.effort, config.timeout_seconds, config.max_retries, config.max_tokens) == (
        "claude-sonnet-5-5", "low", 30.0, 0, 2000)
    assert KEY not in repr(config) and "api_key" not in AIConfig.model_fields


@pytest.mark.parametrize("name, value", [("OPSRESOLVE_AI_EFFORT", "max"), ("OPSRESOLVE_AI_TIMEOUT_SECONDS", "0"),
                                         ("OPSRESOLVE_AI_MAX_RETRIES", "9"), ("OPSRESOLVE_AI_MODEL", "bad model!")])
def test_invalid_settings_are_not_configured_and_nothing_is_called(supplier_case, name, value):
    result, exc = supplier_case
    provider = FakeProvider(json.dumps(valid_answer(exc.exception_id)))
    outcome = explain_exception(result, exc, provider, environ={name: value, API_KEY_VARIABLE: KEY})
    assert (outcome.status, outcome.error_code, provider.requests) == ("not_configured", "not_configured", [])
    assert value not in outcome.error_message and "1 problem(s)" in outcome.error_message


@pytest.mark.parametrize("environ", [{}, {API_KEY_VARIABLE: ""}, {API_KEY_VARIABLE: "   "}])
def test_a_missing_key_is_not_configured_and_no_client_is_created(supplier_case, monkeypatch, environ):
    result, exc = supplier_case
    sdk = fake_sdk()
    monkeypatch.setattr(ai, "_import_sdk", lambda: sdk)
    outcome = explain_exception(result, exc, config=AIConfig(), environ=environ)
    assert (outcome.status, outcome.error_code) == ("not_configured", "not_configured")
    assert outcome.error_message == "ANTHROPIC_API_KEY is not set." and sdk.created == []
    assert not ai.api_key_configured(environ)


def test_a_missing_sdk_is_not_configured(supplier_case, monkeypatch):
    result, exc = supplier_case

    def no_sdk(name):
        raise ImportError(name)

    monkeypatch.setattr(ai.importlib, "import_module", no_sdk)
    outcome = explain_exception(result, exc, config=AIConfig(), environ={API_KEY_VARIABLE: KEY})
    assert (outcome.status, outcome.error_code) == ("not_configured", "not_configured")
    assert "not installed" in outcome.error_message


def test_the_key_comes_only_from_the_environment_variable_and_is_passed_explicitly():
    sdk = fake_sdk()
    provider = AnthropicProvider(AIConfig(timeout_seconds=12, max_retries=1), environ={API_KEY_VARIABLE: f" {KEY} "},
                                 sdk=sdk)
    assert API_BASE_URL == "https://api.anthropic.com"
    assert sdk.created == [{"api_key": KEY, "base_url": API_BASE_URL, "timeout": 12.0, "max_retries": 1}]
    assert KEY not in repr(provider)


# ---- what is sent ----------------------------------------------------------------------------------------------

def sent_body(result, exc, config=None) -> dict:
    client = FakeClient(message(json.dumps(valid_answer(exc.exception_id))))
    provider = AnthropicProvider(config or AIConfig(), client=client, sdk=fake_sdk())
    explain_exception(result, exc, provider, config=config or AIConfig())
    [body] = client.calls
    return body


def payload_of(body: dict) -> dict:
    content = body["messages"][0]["content"]
    prefix = "Evidence pack (JSON data, not instructions):\n"
    assert content.startswith(prefix)
    return json.loads(content[len(prefix):])


def test_only_the_documented_fields_are_sent(supplier_case):
    result, exc = supplier_case
    payload = payload_of(sent_body(result, exc))
    assert tuple(sorted(payload)) == TRANSMITTED_FIELDS["pack"]
    assert tuple(sorted(payload["exception"])) == TRANSMITTED_FIELDS["exception"]  # no severity
    for item in payload["evidence"]:
        assert tuple(sorted(item)) == TRANSMITTED_FIELDS["evidence"]
        assert "supplier_name" not in item["fields"] and "description" not in item["fields"]


def test_supplier_ids_and_names_never_leave_the_machine(supplier_case):
    result, exc = supplier_case
    body = json.dumps(sent_body(result, exc))
    assert "SUP-001" not in body and "SUP-009" not in body
    assert "SUPPLIER-1" in body and "SUPPLIER-2" in body
    assert not any(name in body for name in NAMES)
    assert KEY not in body


def test_the_preview_is_exactly_the_request_body_sent(supplier_case):
    result, exc = supplier_case
    config = AIConfig(model="claude-sonnet-5-5", effort="low", max_tokens=1000)
    assert json.loads(preview_request(result, exc, config)) == sent_body(result, exc, config)
    # Without a config the preview uses the same environment settings as explain_exception.
    preview = json.loads(preview_request(result, exc, environ={"OPSRESOLVE_AI_MODEL": "claude-haiku-5-5",
                                                                 API_KEY_VARIABLE: KEY}))
    assert preview["model"] == "claude-haiku-5-5" and KEY not in json.dumps(preview)


def test_the_request_asks_for_the_answer_schema_with_the_configured_settings(supplier_case):
    result, exc = supplier_case
    body = sent_body(result, exc, AIConfig(effort="high", max_tokens=3000))
    assert (body["model"], body["max_tokens"], body["system"]) == ("claude-opus-5-5", 3000, SYSTEM_PROMPT)
    assert body["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}}
    assert sorted(RESPONSE_SCHEMA["properties"]) == sorted(ExplanationOutput.model_fields)
    assert RESPONSE_SCHEMA["required"] == list(RESPONSE_SCHEMA["properties"])
    assert RESPONSE_SCHEMA["properties"]["direction"]["enum"] == list(DIRECTIONS)
    assert "fallbacks" not in body and "thinking" not in body


def test_digits_only_supplier_ids_never_change_quantities():
    """Supplier 120 also ordered 100, invoiced 120 and received 100: the quantity 120 must stay as it is."""
    result = investigate(Dataset(LoadResult("purchase_orders", records=[po(supplier="120")]),
                                 LoadResult("invoices", records=[inv(supplier="120", qty="120")]),
                                 LoadResult("goods_received", records=[grn(supplier="120")])))
    pack = build_evidence_pack(result, result.exceptions[0])
    sent, reverse = pseudonymise(pack, known_supplier_ids(result))
    assert reverse == {"SUPPLIER-1": "120"}
    assert sent.exception.supplier_id == "SUPPLIER-1"
    assert {item.fields["supplier_id"] for item in sent.evidence} == {"SUPPLIER-1"}
    assert sent.exception.summary == pack.exception.summary  # "Invoiced quantity (120) exceeds ..." unchanged
    assert sent.evidence[1].fields["quantity_invoiced"] == "120"


def test_pseudonyms_never_collide_with_values_in_the_evidence():
    result = investigate(Dataset(LoadResult("purchase_orders", records=[po(supplier="SUPPLIER-1")]),
                                 LoadResult("invoices", records=[inv(supplier="SUPPLIER-1", qty="120")]),
                                 LoadResult("goods_received", records=[grn(supplier="SUPPLIER-1")])))
    sent, reverse = pseudonymise(build_evidence_pack(result, result.exceptions[0]), known_supplier_ids(result))
    assert reverse == {"XSUPPLIER-1": "SUPPLIER-1"} and sent.exception.supplier_id == "XSUPPLIER-1"


# ---- answers ----------------------------------------------------------------------------------------------------

def test_a_valid_answer_is_accepted_with_real_ids_restored(supplier_case):
    result, exc = supplier_case
    outcome = explain_exception(result, exc, FakeProvider(json.dumps(valid_answer(exc.exception_id))),
                                config=AIConfig())
    assert (outcome.status, outcome.problems, outcome.model, outcome.request_id) == (
        "accepted", (), "claude-opus-5-5", "req_fake")
    assert outcome.pack_sha256 == pack_sha256(build_evidence_pack(result, exc))
    facts = [f.statement for f in outcome.explanation.confirmed_facts]
    assert facts == ["Invoice RVX-00981 is from supplier SUP-009.", "PO 4500010015 was raised with supplier SUP-001."]
    assert "SUPPLIER-" not in outcome.explanation.model_dump_json()


@pytest.mark.parametrize("change, expected", [
    (lambda a: a.update(direction="price_above_po"), ("direction_mismatch", "direction")),
    (lambda a: a["confirmed_facts"][0].update(statement="Invoice RVX-00981 bills 2,500 units."),
     ("unsupported_number", "confirmed_facts[0].statement")),
    (lambda a: a.update(explanation="Northgate Fasteners Ltd raised the PO."), ("unsupported_name", "explanation")),
    (lambda a: a.update(sufficiency="sufficient", missing_information=[]), None),  # accepted: no limitations
    (lambda a: a["possible_causes"][0].update(status="confirmed"), ("schema", "possible_causes[0].status")),
])
def test_every_answer_goes_through_the_stage_a_validator(supplier_case, change, expected):
    result, exc = supplier_case
    answer = valid_answer(exc.exception_id)
    change(answer)
    outcome = explain_exception(result, exc, FakeProvider(json.dumps(answer)), config=AIConfig())
    if expected is None:
        assert outcome.status == "accepted"
    else:
        assert outcome.status == "rejected" and outcome.explanation is None
        assert expected in [(p.code, p.location) for p in outcome.problems]


def test_a_real_supplier_id_in_the_answer_is_rejected(supplier_case):
    """The model only saw pseudonyms, so a real id can only have been guessed."""
    result, exc = supplier_case
    outcome = explain_exception(result, exc, FakeProvider(json.dumps(valid_answer(exc.exception_id, "SUP-009"))),
                                config=AIConfig())
    assert outcome.status == "rejected"
    assert ("unsupported_identifier", "confirmed_facts[0].statement") in [(p.code, p.location) for p in outcome.problems]


def test_the_restored_answer_is_validated_again_against_the_real_pack(supplier_case, monkeypatch):
    """A fault in restoring ids (simulated here) cannot produce an accepted answer with unsupported content."""
    result, exc = supplier_case

    real_restore = ai._restore

    def faulty_restore(output, reverse):
        data = real_restore(output, reverse)
        data["explanation"] = "Invoice RVX-00981 is from supplier SUP-777."  # not an id in the real evidence
        return data

    monkeypatch.setattr(ai, "_restore", faulty_restore)
    outcome = explain_exception(result, exc, FakeProvider(json.dumps(valid_answer(exc.exception_id))),
                                config=AIConfig())
    assert outcome.status == "rejected" and outcome.explanation is None
    assert [(p.code, p.location) for p in outcome.problems] == [("unsupported_identifier", "explanation")]


@pytest.mark.parametrize("text", ["not json", "", "[]", '{"exception_id": "EXC-0001"'])
def test_malformed_answers_are_rejected(supplier_case, text):
    result, exc = supplier_case
    outcome = explain_exception(result, exc, FakeProvider(text), config=AIConfig())
    assert outcome.status == "rejected" and outcome.explanation is None
    assert {p.code for p in outcome.problems} == {"schema"}


# ---- provider errors and responses ----------------------------------------------------------------------------

@pytest.mark.parametrize("error, code", [
    (FakeTimeoutError("timed out"), "timeout"),
    (FakeConnectionError("dns"), "connection"),
    (status_class("AuthenticationError")(401), "authentication"),
    (status_class("PermissionDeniedError")(403), "authentication"),
    (status_class("RateLimitError")(429), "rate_limited"),
    (status_class("InternalServerError")(500), "provider_error"),
    (status_class("BadRequestError")(400), "provider_error"),
    (status_class("NotFoundError")(404), "provider_error"),
    (ValueError("something else"), "provider_error"),
])
def test_provider_errors_become_safe_results(supplier_case, error, code):
    result, exc = supplier_case
    sdk = fake_sdk()
    for name in ("AuthenticationError", "PermissionDeniedError", "RateLimitError"):  # match the classes raised
        if type(error).__name__ == name:
            setattr(sdk, name, type(error))
    provider = AnthropicProvider(AIConfig(), client=FakeClient(error=error), sdk=sdk)
    outcome = explain_exception(result, exc, provider, config=AIConfig())
    assert (outcome.status, outcome.error_code, outcome.explanation) == ("error", code, None)
    assert "secret details" not in outcome.error_message and "something else" not in outcome.error_message
    if isinstance(error, FakeStatusError):
        assert outcome.request_id == "req_err"
        if code == "provider_error":  # a generic API error names its HTTP status
            assert str(error.status_code) in outcome.error_message


@pytest.mark.parametrize("response, code", [
    (message(None, "refusal"), "refused"),
    (message('{"exception_id": "EXC', "max_tokens"), "malformed"),
    (message(None), "malformed"),  # only a thinking block
    (message("   "), "malformed"),
])
def test_refusals_and_unusable_responses_are_errors(supplier_case, response, code):
    result, exc = supplier_case
    provider = AnthropicProvider(AIConfig(), client=FakeClient(response), sdk=fake_sdk())
    outcome = explain_exception(result, exc, provider, config=AIConfig())
    assert (outcome.status, outcome.error_code, outcome.request_id) == ("error", code, "req_ok")


def test_the_text_block_after_thinking_is_used(supplier_case):
    result, exc = supplier_case
    response = message(json.dumps(valid_answer(exc.exception_id)), thinking=True)
    provider = AnthropicProvider(AIConfig(), client=FakeClient(response), sdk=fake_sdk())
    assert explain_exception(result, exc, provider, config=AIConfig()).status == "accepted"


# ---- Fix 1: the answer schema uses only keywords the API supports -------------------------------------------------

# Structured outputs accept these keywords (no maxItems, maxLength, pattern, minimum, ...); see ai.RESPONSE_SCHEMA.
SUPPORTED_SCHEMA_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum"}


def schema_nodes(node: dict):
    yield node
    for child in node.get("properties", {}).values():
        yield from schema_nodes(child)
    if "items" in node:
        yield from schema_nodes(node["items"])


def test_the_response_schema_uses_only_supported_keywords():
    nodes = list(schema_nodes(RESPONSE_SCHEMA))
    assert len(nodes) > 20
    for node in nodes:
        assert set(node) <= SUPPORTED_SCHEMA_KEYWORDS, set(node) - SUPPORTED_SCHEMA_KEYWORDS
        if node["type"] == "object":
            assert node["additionalProperties"] is False and node["required"] == list(node["properties"])
        if "enum" in node:
            assert all(isinstance(value, str) for value in node["enum"])
    assert "maxItems" not in json.dumps(RESPONSE_SCHEMA)


@pytest.mark.parametrize("field, value", [("evidence_refs_used", ["E1", "E2"] * 11),  # 22 > 20
                                          ("missing_information", ["More."] * 11)])  # 11 > 10
def test_limits_left_out_of_the_schema_are_still_enforced(supplier_case, field, value):
    result, exc = supplier_case
    answer = valid_answer(exc.exception_id)
    answer[field] = value
    outcome = explain_exception(result, exc, FakeProvider(json.dumps(answer)), config=AIConfig())
    assert outcome.status == "rejected" and ("schema", field) in [(p.code, p.location) for p in outcome.problems]


# ---- Fix 2: every known supplier id is pseudonymised, and the request is checked before it is sent ------------------

def test_known_supplier_ids_come_from_every_loaded_file(sample):
    # purchase_orders and goods_received: SUP-001 to SUP-005; invoices also SUP-009 (invoice RVX-00981).
    assert known_supplier_ids(sample) == {"SUP-001", "SUP-002", "SUP-003", "SUP-004", "SUP-005", "SUP-009"}


def test_a_supplier_id_left_only_in_engine_text_is_pseudonymised(supplier_case):
    """The PO's record (purchase_orders line 19) is unavailable, so SUP-001 survives only in the engine's text."""
    result, exc = supplier_case
    altered = exc.model_copy(update={"evidence": (exc.evidence[0].model_copy(update={"source_row": 999}),
                                                  exc.evidence[1])})
    assert build_evidence_pack(result, altered).identifying_values()["supplier_id"] == ("SUP-009",)
    body = json.loads(preview_request(result, altered, AIConfig()))
    assert "SUP-001" not in json.dumps(body) and "SUP-009" not in json.dumps(body)
    payload = payload_of(body)
    assert payload["exception"]["summary"] == ("Invoice RVX-00981 is from supplier SUPPLIER-2 but PO 4500010015 was "
                                               "raised with supplier SUPPLIER-1.")
    assert payload["exception"]["expected"] == "Supplier SUPPLIER-1"
    provider = FakeProvider(json.dumps(valid_answer(exc.exception_id)))
    explain_exception(result, altered, provider, config=AIConfig())
    assert "SUP-001" not in provider.requests[0].user


def test_supplier_ids_embedded_in_engine_text_are_replaced_and_document_numbers_kept(sample):
    """PO 4500010007: invoice CIB-07781 may duplicate CIB-7781 'from supplier SUP-003'."""
    exc = find(sample, T.POSSIBLE_DUPLICATE_INVOICE, "4500010007")
    payload = payload_of(json.loads(preview_request(sample, exc, AIConfig())))
    summary = payload["exception"]["summary"]
    assert "from supplier SUPPLIER-1:" in summary and "SUP-003" not in summary
    assert "CIB-07781" in summary and "CIB-7781" in summary and "14 day(s)" in summary


def test_numeric_facts_are_kept_when_supplier_ids_are_digits_only():
    """Suppliers 120 and 777 also appear as a quantity (120), a price (777.00), an invoice and a GRN number."""
    result = investigate(Dataset(
        LoadResult("purchase_orders", records=[po(supplier="120", qty="120", price="777.00")]),
        LoadResult("invoices", records=[inv(number="777", supplier="777", qty="120", price="777.00")]),
        LoadResult("goods_received", records=[grn(number="120", supplier="120", qty="100")])))
    by_type = {e.exception_type: e for e in result.exceptions}
    assert set(by_type) == {T.SUPPLIER_MISMATCH, T.QUANTITY_MISMATCH}
    for exc in by_type.values():
        pack = build_evidence_pack(result, exc)
        sent, reverse = pseudonymise(pack, known_supplier_ids(result))
        assert reverse == {"SUPPLIER-1": "120", "SUPPLIER-2": "777"}
        for before, after in zip(pack.evidence, sent.evidence):
            assert {k: v for k, v in after.fields.items() if k != "supplier_id"} == {
                k: v for k, v in before.fields.items() if k != "supplier_id"}
            assert after.reference == before.reference  # invoice 777 and GRN 120 stay as they are
        assert sent.exception.quantity_difference == pack.exception.quantity_difference
        assert sent.exception.amount_impact == pack.exception.amount_impact
    mismatch = pseudonymise(build_evidence_pack(result, by_type[T.SUPPLIER_MISMATCH]), known_supplier_ids(result))[0]
    assert mismatch.exception.summary == ("Invoice 777 is from supplier SUPPLIER-2 but PO 4500000001 was raised with "
                                          "supplier SUPPLIER-1.")
    quantity = pseudonymise(build_evidence_pack(result, by_type[T.QUANTITY_MISMATCH]), known_supplier_ids(result))[0]
    assert quantity.exception.summary == build_evidence_pack(result, by_type[T.QUANTITY_MISMATCH]).exception.summary
    outcome = explain_exception(result, by_type[T.QUANTITY_MISMATCH], FakeProvider("not json"), config=AIConfig())
    assert outcome.status == "rejected"  # the request passed the guard and was sent


def test_nothing_is_sent_if_a_supplier_id_remains(supplier_case, monkeypatch, caplog):
    result, exc = supplier_case
    monkeypatch.setattr(ai, "pseudonymise", lambda pack, known=(): (pack, {}))  # simulate a failed replacement
    provider = FakeProvider(json.dumps(valid_answer(exc.exception_id)))
    caplog.set_level(logging.INFO, logger="opsresolve.ai")
    outcome = explain_exception(result, exc, provider, config=AIConfig())
    assert (outcome.status, outcome.error_code, provider.requests) == ("error", "privacy_guard", [])
    assert "SUP-" not in outcome.error_message and "SUP-" not in caplog.text
    with pytest.raises(ExplanationError) as raised:
        preview_request(result, exc, AIConfig())
    assert raised.value.code == "privacy_guard"


def test_nothing_is_sent_if_a_digits_only_supplier_id_remains_in_a_field(monkeypatch):
    """Supplier 120 appears only in supplier_id fields here (the engine text names no supplier)."""
    result = investigate(Dataset(LoadResult("purchase_orders", records=[po(supplier="120")]),
                                 LoadResult("invoices", records=[inv(supplier="120", qty="120")]),
                                 LoadResult("goods_received", records=[grn(supplier="120")])))
    exc = result.exceptions[0]
    assert "120" not in build_evidence_pack(result, exc).exception.summary.replace("(120)", "")
    monkeypatch.setattr(ai, "pseudonymise", lambda pack, known=(): (pack, {}))  # simulate a failed replacement
    provider = FakeProvider("not json")
    outcome = explain_exception(result, exc, provider, config=AIConfig())
    assert (outcome.status, outcome.error_code, provider.requests) == ("error", "privacy_guard", [])


def test_nothing_is_sent_if_a_supplier_name_appears(supplier_case, monkeypatch):
    result, exc = supplier_case
    real_payload = ai.transmitted_payload

    def payload_with_name(sent):
        data = real_payload(sent)
        data["exception"]["summary"] += " Northgate  Fasteners LTD"  # any spacing or case
        return data

    monkeypatch.setattr(ai, "transmitted_payload", payload_with_name)
    provider = FakeProvider(json.dumps(valid_answer(exc.exception_id)))
    outcome = explain_exception(result, exc, provider, config=AIConfig())
    assert (outcome.status, outcome.error_code, provider.requests) == ("error", "privacy_guard", [])
    assert "Northgate" not in outcome.error_message


# ---- Final review D1: structured fields equal to a supplier id are never replaced, and do not trip the guard --------

def colliding_ids_result():
    """PO 4500000001 (supplier ABC1, SKU SKU-9) is invoiced on invoice ABC1 by supplier SKU-9: the invoice number
    equals the PO's supplier id and the SKU equals the invoice's supplier id. PO 4500000002, from supplier USD,
    is under-invoiced (90 of 100)."""
    return investigate(Dataset(
        LoadResult("purchase_orders", records=[po(supplier="ABC1", sku="SKU-9"),
                                               po(po_number="4500000002", supplier="USD", sku="SKU-2", row=3)]),
        LoadResult("invoices", records=[inv(number="ABC1", supplier="SKU-9", sku="SKU-9", qty="90"),
                                        inv(number="INV-2", po_number="4500000002", supplier="USD", sku="SKU-2",
                                            qty="90", row=3)]),
        LoadResult("goods_received", records=[grn(supplier="ABC1", sku="SKU-9"),
                                              grn(number="GRN-2", po_number="4500000002", supplier="USD",
                                                  sku="SKU-2", row=3)])))


def test_skus_and_document_numbers_equal_to_supplier_ids_are_kept():
    result = colliding_ids_result()
    by_type = {e.exception_type: e for e in result.exceptions if e.po_number == "4500000001"}
    assert set(by_type) == {T.SUPPLIER_MISMATCH, T.QUANTITY_MISMATCH}
    for exc in by_type.values():
        pack = build_evidence_pack(result, exc)
        sent, reverse = pseudonymise(pack, known_supplier_ids(result))
        assert reverse == {"SUPPLIER-1": "ABC1", "SUPPLIER-2": "SKU-9"}  # USD occurs only as a currency
        forward = {original: pseudonym for pseudonym, original in reverse.items()}
        assert sent.exception.sku == "SKU-9" and sent.exception.supplier_id == forward[pack.exception.supplier_id]
        by_source = {item.source: item for item in sent.evidence}
        assert by_source["purchase_orders"].fields["supplier_id"] == "SUPPLIER-1"
        if "goods_received" in by_source:  # the supplier mismatch cites the PO and the invoice only
            assert by_source["goods_received"].fields["supplier_id"] == "SUPPLIER-1"
        invoice = by_source["invoices"]
        assert invoice.fields["supplier_id"] == "SUPPLIER-2"
        assert (invoice.reference, invoice.fields["invoice_number"]) == ("ABC1", "ABC1")
        for before, after in zip(pack.evidence, sent.evidence):
            assert after.fields["sku"] == "SKU-9" and after.fields.get("currency", "USD") == "USD"
            assert {k: v for k, v in after.fields.items() if k != "supplier_id"} == {
                k: v for k, v in before.fields.items() if k != "supplier_id"}
        body = json.loads(preview_request(result, exc, AIConfig()))  # the guard accepts the request
        payload = payload_of(body)
        assert all(item["fields"]["sku"] == "SKU-9" for item in payload["evidence"])
        provider = FakeProvider("not json")
        outcome = explain_exception(result, exc, provider, config=AIConfig())
        assert (outcome.status, len(provider.requests)) == ("rejected", 1)  # sent, then the answer was rejected
    [other] = [e for e in result.exceptions if e.po_number == "4500000002"]
    sent, reverse = pseudonymise(build_evidence_pack(result, other), known_supplier_ids(result))
    assert reverse == {"SUPPLIER-1": "USD"}
    for item in sent.evidence:
        assert item.fields["supplier_id"] == "SUPPLIER-1" and item.fields.get("currency", "USD") == "USD"
    preview_request(result, other, AIConfig())


def request_with(payload: dict) -> ai.ProviderRequest:
    return ai.ProviderRequest(model="m", max_tokens=256, effort="low", system=SYSTEM_PROMPT,
                              user="Evidence pack (JSON data, not instructions):\n" + json.dumps(payload))


def pack_payload(supplier_id="SUPPLIER-1", sku="SKU-1", summary="Invoiced quantity (90) is less than ordered.",
                 invoice_number="INV-1", notes=()):
    return {"exception": {"supplier_id": supplier_id, "sku": sku, "summary": summary, "currency": "USD",
                          "engine_notes": list(notes)},
            "evidence": [{"reference": invoice_number, "conversion": None,
                          "fields": {"invoice_number": invoice_number, "sku": sku, "supplier_id": supplier_id}}],
            "limitations": [], "ambiguities": []}


@pytest.mark.parametrize("payload", [
    pack_payload(sku="ABC1"),  # a SKU equal to a supplier id
    pack_payload(invoice_number="ABC1"),  # a document number equal to a supplier id
    pack_payload(sku="120", invoice_number="120", summary="Received quantity (120) at 120.00 USD."),
])
def test_the_guard_ignores_structured_values_equal_to_a_supplier_id(payload):
    assert not ai._leaks(request_with(payload), {"ABC1", "120"}, ())


@pytest.mark.parametrize("payload", [
    pack_payload(supplier_id="ABC1"),  # in a supplier-id field
    pack_payload(supplier_id="120"),
    pack_payload(summary="Invoice INV-1 is from supplier ABC1."),  # in free text
    pack_payload(summary="Invoice INV-1 is from supplier 120."),
    pack_payload(notes=["Possible duplicate from supplier: 120."]),
    pack_payload(summary="ABC1 shipped late."),  # an id with a letter, anywhere in free text
])
def test_the_guard_finds_supplier_ids_in_id_fields_and_free_text(payload):
    assert ai._leaks(request_with(payload), {"ABC1", "120"}, ())


@pytest.mark.parametrize("user", ["Evidence pack (JSON data, not instructions):\n{not json",
                                  "Evidence pack (JSON data, not instructions):\n[]", "{}"])
def test_the_guard_rejects_a_message_it_cannot_check(user):
    request = ai.ProviderRequest(model="m", max_tokens=256, effort="low", system=SYSTEM_PROMPT, user=user)
    assert ai._leaks(request, (), ())


# ---- Final review D2: supplier names are matched as whole words -----------------------------------------------------

@pytest.mark.parametrize("names, summary", [
    (["SKU"], "Invoiced quantity (90) is less than received quantity (100) for PO 4500000001 / SKU-1."),
    (["Calder"], "Delivered by Calderwood Logistics."),
    (["Northgate Fasteners Ltd"], "Northgate Fasteners Ltdx shipped."),
])
def test_a_supplier_name_inside_another_word_is_not_a_leak(names, summary):
    assert not ai._leaks(request_with(pack_payload(summary=summary)), (), names)


@pytest.mark.parametrize("names, summary", [
    (["SKU"], "Goods were shipped by SKU on 2026-08-08."),
    (["sku"], "Goods were shipped by SKU."),
    (["Northgate Fasteners Ltd"], "Shipped by northgate   FASTENERS ltd."),
    (["Northgate  Fasteners\tLtd"], "Shipped by Northgate Fasteners Ltd (E1)."),
])
def test_a_standalone_supplier_name_is_a_leak(names, summary):
    assert ai._leaks(request_with(pack_payload(summary=summary)), (), names)
    notes = pack_payload(notes=[summary])
    assert ai._leaks(request_with(notes), (), names)


def test_a_supplier_named_like_a_sku_prefix_does_not_block_the_request():
    base = investigate(Dataset(LoadResult("purchase_orders", records=[po()]),
                               LoadResult("invoices", records=[inv(qty="90")]),
                               LoadResult("goods_received", records=[grn()])))
    named = [record.model_copy(update={"supplier_name": "SKU"}) for record in base.dataset.purchase_orders.records]
    result = investigate(Dataset(LoadResult("purchase_orders", records=named),
                                 LoadResult("invoices", records=list(base.dataset.invoices.records)),
                                 LoadResult("goods_received", records=list(base.dataset.goods_received.records))))
    [exc] = result.exceptions
    assert "SKU-1" in payload_of(json.loads(preview_request(result, exc, AIConfig())))["exception"]["summary"]


# ---- Final review D3: an id that contains a shorter id is replaced whole -------------------------------------------

@pytest.mark.parametrize("short, long", [("ACME", "ACME CO"), ("ACME", "ACME.UK"), ("A&B", "A&B LTD")])
@pytest.mark.parametrize("invoice_is_short", [True, False])
def test_overlapping_supplier_ids_are_replaced_whole(short, long, invoice_is_short):
    """PO 4500000001 is raised with one supplier and invoiced (INV-1) by the other; ids sort short < long."""
    on_invoice, on_po = (short, long) if invoice_is_short else (long, short)
    result = investigate(Dataset(LoadResult("purchase_orders", records=[po(supplier=on_po)]),
                                 LoadResult("invoices", records=[inv(supplier=on_invoice)]),
                                 LoadResult("goods_received", records=[grn(supplier=on_po)])))
    [exc] = [e for e in result.exceptions if e.exception_type is T.SUPPLIER_MISMATCH]
    pack = build_evidence_pack(result, exc)
    sent, reverse = pseudonymise(pack, known_supplier_ids(result))
    assert reverse == {"SUPPLIER-1": short, "SUPPLIER-2": long}
    forward = {short: "SUPPLIER-1", long: "SUPPLIER-2"}
    assert sent.exception.summary == (f"Invoice INV-1 is from supplier {forward[on_invoice]} but PO 4500000001 "
                                      f"was raised with supplier {forward[on_po]}.")
    by_source = {item.source: item.fields["supplier_id"] for item in sent.evidence}
    assert by_source == {"purchase_orders": forward[on_po], "invoices": forward[on_invoice]}
    payload = payload_of(json.loads(preview_request(result, exc, AIConfig())))  # the guard accepts it
    free_text = json.dumps([payload["exception"][key] for key in ("summary", "expected", "actual",
                                                                  "engine_recommended_action", "engine_notes")])
    for fragment in (short, long, long[len(short):].strip(" .")):  # e.g. "CO", "UK", "LTD"
        assert fragment not in free_text
    restored = ai._restore(ExplanationOutput.model_validate({**valid_answer(exc.exception_id),
                                                             "explanation": "SUPPLIER-2 and SUPPLIER-1 differ."}),
                           reverse)
    assert restored["explanation"] == f"{long} and {short} differ."


def test_overlapping_ids_keep_unrelated_longer_tokens_and_numbers():
    """Suppliers SUP-1, SUP-10 and SUP-1-A (hyphen-joined: separate tokens) and 120; SKU SUP-1X stays as it is."""
    result = investigate(Dataset(
        LoadResult("purchase_orders", records=[po(supplier="SUP-10", sku="SUP-1X", qty="120", price="120.00"),
                                               po(po_number="4500000002", supplier="SUP-1-A", row=3),
                                               po(po_number="4500000003", supplier="120", row=4)]),
        LoadResult("invoices", records=[inv(supplier="SUP-1", sku="SUP-1X", qty="120", price="120.00")]),
        LoadResult("goods_received", records=[grn(supplier="SUP-10", sku="SUP-1X", qty="100")])))
    by_type = {e.exception_type: e for e in result.exceptions if e.po_number == "4500000001"}
    mismatch = pseudonymise(build_evidence_pack(result, by_type[T.SUPPLIER_MISMATCH]), known_supplier_ids(result))
    assert mismatch[1] == {"SUPPLIER-1": "SUP-1", "SUPPLIER-2": "SUP-10"}
    assert mismatch[0].exception.summary == ("Invoice INV-1 is from supplier SUPPLIER-1 but PO 4500000001 was "
                                             "raised with supplier SUPPLIER-2.")
    pack = build_evidence_pack(result, by_type[T.QUANTITY_MISMATCH])
    sent, _ = pseudonymise(pack, known_supplier_ids(result))
    assert sent.exception.summary == pack.exception.summary  # names PO 4500000001 / SUP-1X and 120: unchanged
    assert "SUP-1X" in sent.exception.summary and "(120)" in sent.exception.summary
    for before, after in zip(pack.evidence, sent.evidence):
        assert {k: v for k, v in after.fields.items() if k != "supplier_id"} == {
            k: v for k, v in before.fields.items() if k != "supplier_id"}


@pytest.mark.parametrize("payload", [
    pack_payload(summary="Invoice INV-1 is from supplier ACME CO."),
    pack_payload(summary="Invoice INV-1 is from supplier ACME.UK."),
    pack_payload(notes=["Possible duplicate from supplier A&B LTD."]),
    pack_payload(supplier_id="ACME CO"),
    pack_payload(supplier_id="A&B"),
])
def test_the_guard_finds_any_overlapping_id_left_in_a_protected_field(payload):
    assert ai._leaks(request_with(payload), {"ACME", "ACME CO", "ACME.UK", "A&B", "A&B LTD"}, ())


# ---- Partial overlaps: ids whose matches overlap without containment are never sent ------------------------------

def spans_ambiguous(text: str, ids: list[str]) -> bool:
    return ai._ambiguous(ai._occurrences(text, [(value, ai._supplier_in_text(value)) for value in ids]))


@pytest.mark.parametrize("text, ids, ambiguous", [
    ("supplier A B C.", ["A B", "B C"], True),  # neither match contains the other
    ("supplier A STAR but PO", ["A STAR", "STAR but"], True),
    ("Supplier A B", ["A B", "Supplier A"], True),
    ("supplier A A A.", ["A A"], True),  # one id overlapping itself
    ("supplier ACME CO.", ["ACME", "ACME CO"], False),  # containment: the longer id wins
    ("supplier A B C.", ["A B", "B C", "A B C"], False),  # both partial matches lie inside a longer one
    ("supplier NORTH STAR but PO 1 was raised with supplier STAR LOGISTICS.", ["NORTH STAR", "STAR LOGISTICS"], False),
    ("supplier ACME UK but supplier UK LTD.", ["ACME UK", "UK LTD"], False),
    ("supplier A&B but supplier B&C.", ["A&B", "B&C"], False),
    ("supplier J. SMITH but supplier SMITH & CO.", ["J. SMITH", "SMITH & CO"], False),
    ("supplier 1 2 3 (quantity 2 3)", ["1 2", "2 3"], False),  # digits-only: only after "supplier"
    ("Received quantity (120) at 120.00 for supplier 120.", ["120"], False),
])
def test_overlapping_matches_are_detected(text, ids, ambiguous):
    assert spans_ambiguous(text, ids) is ambiguous


def supplier_mismatch(po_supplier: str, invoice_supplier: str, other_suppliers=()):
    """PO 4500000001 (and its receipt) from ``po_supplier``, invoice INV-1 from ``invoice_supplier``."""
    others = [po(po_number=f"45000000{n:02d}", supplier=value, row=n + 1)
              for n, value in enumerate(other_suppliers, start=2)]
    result = investigate(Dataset(LoadResult("purchase_orders", records=[po(supplier=po_supplier), *others]),
                                 LoadResult("invoices", records=[inv(supplier=invoice_supplier)]),
                                 LoadResult("goods_received", records=[grn(supplier=po_supplier)])))
    [exc] = [e for e in result.exceptions if e.exception_type is T.SUPPLIER_MISMATCH]
    return result, exc


def assert_never_sent(result, exc, caplog, ids):
    provider = FakeProvider(json.dumps(valid_answer(exc.exception_id)))
    caplog.set_level(logging.INFO, logger="opsresolve.ai")
    outcome = explain_exception(result, exc, provider, config=AIConfig())
    assert (outcome.status, outcome.error_code, outcome.explanation) == ("error", "privacy_guard", None)
    assert provider.requests == []
    with pytest.raises(ExplanationError) as raised:
        preview_request(result, exc, AIConfig())
    assert raised.value.code == "privacy_guard"
    for value in ids:  # the message and the log name no supplier id
        assert value not in outcome.error_message and value not in raised.value.message and value not in caplog.text


def test_partially_overlapping_ids_in_text_are_never_sent(caplog):
    """Suppliers A B (PO) and B C (invoice); the engine's summary is replaced by one naming "A B C"."""
    result, exc = supplier_mismatch("A B", "B C")
    altered = exc.model_copy(update={"summary": "Invoice INV-1 is from supplier A B C."})
    with pytest.raises(ExplanationError) as raised:
        pseudonymise(build_evidence_pack(result, altered), known_supplier_ids(result))
    assert raised.value.code == "privacy_guard"
    assert_never_sent(result, altered, caplog, ["A B", "B C"])


@pytest.mark.parametrize("po_supplier, invoice_supplier", [
    ("STAR but", "A STAR"),  # summary: "...from supplier A STAR but PO..."
    ("A B", "Supplier A"),  # expected: "Supplier A B"
])
def test_ids_overlapping_the_engine_text_are_never_sent(caplog, po_supplier, invoice_supplier):
    result, exc = supplier_mismatch(po_supplier, invoice_supplier)
    assert_never_sent(result, exc, caplog, [po_supplier, invoice_supplier])


def test_matches_inside_a_longer_id_are_replaced_by_the_longer_id():
    """Invoice from A B, PO from A B C; B C is another known supplier and occurs only inside "A B C"."""
    result, exc = supplier_mismatch("A B C", "A B", other_suppliers=["B C"])
    sent, reverse = pseudonymise(build_evidence_pack(result, exc), known_supplier_ids(result))
    assert reverse == {"SUPPLIER-1": "A B", "SUPPLIER-2": "A B C", "SUPPLIER-3": "B C"}
    assert sent.exception.summary == ("Invoice INV-1 is from supplier SUPPLIER-1 but PO 4500000001 was raised with "
                                      "supplier SUPPLIER-2.")
    assert (sent.exception.expected, sent.exception.actual) == ("Supplier SUPPLIER-2", "Supplier SUPPLIER-1")
    provider = FakeProvider("not json")
    assert explain_exception(result, exc, provider, config=AIConfig()).status == "rejected"
    assert len(provider.requests) == 1  # sent


@pytest.mark.parametrize("po_supplier, invoice_supplier", [
    ("NORTH STAR", "STAR LOGISTICS"), ("ACME UK", "UK LTD"), ("A&B", "B&C"), ("J. SMITH", "SMITH & CO")])
def test_ids_sharing_words_are_replaced_when_their_matches_do_not_overlap(po_supplier, invoice_supplier):
    result, exc = supplier_mismatch(po_supplier, invoice_supplier)
    sent, reverse = pseudonymise(build_evidence_pack(result, exc), known_supplier_ids(result))
    forward = {original: pseudonym for pseudonym, original in reverse.items()}
    assert sorted(forward) == sorted([po_supplier, invoice_supplier])
    assert list(reverse) == ["SUPPLIER-1", "SUPPLIER-2"] and reverse["SUPPLIER-1"] == min(forward)  # sorted ids
    assert sent.exception.summary == (f"Invoice INV-1 is from supplier {forward[invoice_supplier]} but PO "
                                      f"4500000001 was raised with supplier {forward[po_supplier]}.")
    assert sent.exception.expected == f"Supplier {forward[po_supplier]}"
    assert sent.exception.actual == f"Supplier {forward[invoice_supplier]}"
    assert {item.source: item.fields["supplier_id"] for item in sent.evidence} == {
        "purchase_orders": forward[po_supplier], "invoices": forward[invoice_supplier]}
    payload = payload_of(json.loads(preview_request(result, exc, AIConfig())))  # the guard accepts it
    assert payload["exception"]["summary"] == sent.exception.summary


# ---- Fix 3: unexpected exceptions never escape ---------------------------------------------------------------------

def test_an_unexpected_client_construction_error_is_not_configured(supplier_case, monkeypatch):
    result, exc = supplier_case
    sdk = fake_sdk()

    def broken_client(**kwargs):
        raise ValueError("secret setting value")

    sdk.Anthropic = broken_client
    monkeypatch.setattr(ai, "_import_sdk", lambda: sdk)
    outcome = explain_exception(result, exc, config=AIConfig(), environ={API_KEY_VARIABLE: KEY})
    assert (outcome.status, outcome.error_code) == ("not_configured", "not_configured")
    assert outcome.error_message == "The API client could not be created (ValueError)."


def test_a_broken_sdk_installation_is_not_configured(supplier_case, monkeypatch):
    result, exc = supplier_case

    def broken_import(name):
        raise RuntimeError("secret path in the traceback")

    monkeypatch.setattr(ai.importlib, "import_module", broken_import)
    outcome = explain_exception(result, exc, config=AIConfig(), environ={API_KEY_VARIABLE: KEY})
    assert (outcome.status, outcome.error_code) == ("not_configured", "not_configured")
    assert outcome.error_message == "The Anthropic SDK could not be loaded (RuntimeError)."


def test_any_provider_construction_error_is_contained(supplier_case, monkeypatch):
    result, exc = supplier_case

    class Unbuildable:
        def __init__(self, *args, **kwargs):
            raise TypeError("secret")

    monkeypatch.setattr(ai, "AnthropicProvider", Unbuildable)
    outcome = explain_exception(result, exc, config=AIConfig(), environ={API_KEY_VARIABLE: KEY})
    assert (outcome.status, outcome.error_code) == ("not_configured", "not_configured")
    assert outcome.error_message == "The provider could not be created (TypeError)."


def test_unexpected_provider_exceptions_are_contained(supplier_case, caplog):
    result, exc = supplier_case

    class CustomProvider:
        def generate(self, request):
            raise RuntimeError(f"secret: {request.user[:30]}")

    caplog.set_level(logging.INFO, logger="opsresolve.ai")
    outcome = explain_exception(result, exc, CustomProvider(), config=AIConfig())
    assert (outcome.status, outcome.error_code, outcome.explanation) == ("error", "provider_error", None)
    assert outcome.error_message == "The provider failed unexpectedly (RuntimeError)."
    assert "secret" not in caplog.text and "Evidence pack" not in caplog.text


@pytest.mark.parametrize("returned", [None, "plain text", ProviderResponse(None)])
def test_a_provider_without_answer_text_is_malformed(supplier_case, returned):
    result, exc = supplier_case

    class OddProvider:
        def generate(self, request):
            return returned

    outcome = explain_exception(result, exc, OddProvider(), config=AIConfig())
    assert (outcome.status, outcome.error_code) == ("error", "malformed")


# ---- logging and side effects -----------------------------------------------------------------------------------

def test_logs_hold_no_key_prompt_evidence_names_or_answer(supplier_case, caplog):
    result, exc = supplier_case
    answer = valid_answer(exc.exception_id)
    caplog.set_level(logging.DEBUG, logger="opsresolve.ai")
    explain_exception(result, exc, FakeProvider(json.dumps(answer)), config=AIConfig())  # accepted
    bad = copy.deepcopy(answer)
    bad["explanation"] = "Northgate Fasteners Ltd billed 2,500 units."
    explain_exception(result, exc, FakeProvider(json.dumps(bad)), config=AIConfig())  # rejected
    explain_exception(result, exc, FakeProvider(error=ExplanationError("timeout", "The request timed out.")),
                      config=AIConfig())  # error
    explain_exception(result, exc, config=AIConfig(), environ={})  # no key: not configured, with or without the SDK
    records = [r for r in caplog.records if r.name == "opsresolve.ai"]
    assert [r.getMessage().split("status=")[1].split()[0] for r in records] == [
        "accepted", "rejected", "error", "not_configured"]
    text = "\n".join(r.getMessage() for r in records)
    forbidden = [KEY, SYSTEM_PROMPT[:40], "Evidence pack", "SUP-001", "SUP-009", "SUPPLIER-1", "RVX-00981",
                 "Invoice RVX", "2,500", "billed", *NAMES]
    assert [item for item in forbidden if item in text] == []
    assert "unsupported_name@explanation" in text and exc.exception_id in text


def test_no_test_in_this_module_can_reach_the_network():
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("api.anthropic.com", 443))
    with pytest.raises(NetworkBlocked):
        socket.socket().connect(("192.0.2.1", 443))


def test_explaining_does_not_change_the_investigation(supplier_case):
    result, exc = supplier_case
    before = [e.model_dump() for e in result.exceptions]
    explain_exception(result, exc, FakeProvider(json.dumps(valid_answer(exc.exception_id))), config=AIConfig())
    assert [e.model_dump() for e in result.exceptions] == before


# ---- isolation ---------------------------------------------------------------------------------------------------

def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
    return names


def test_only_the_ai_module_refers_to_the_sdk_and_nothing_uses_the_ai_module():
    sources = [*(REPO / "src" / "opsresolve").glob("*.py"), REPO / "app.py"]
    assert [p.name for p in sources if "anthropic" in p.read_text(encoding="utf-8") and p.name != "ai.py"] == []
    assert [p.name for p in sources if "opsresolve.ai" in imported_modules(p)] == []
    assert "anthropic" not in imported_modules(REPO / "src" / "opsresolve" / "ai.py")  # imported lazily, by name


def test_importing_the_ai_module_does_not_import_the_sdk():
    code = "import sys, opsresolve.ai; print('anthropic' in sys.modules)"
    output = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert output.strip() == "False"


# ---- the real SDK (needs the ai extra; CI installs it) -------------------------------------------------------------

REQUIRE_SDK_TESTS = "OPSRESOLVE_REQUIRE_AI_SDK_TESTS"  # set to 1 in CI (.github/workflows/ci.yml)


def real_sdk():
    """The real SDK and httpx2. Locally a missing ``ai`` extra skips the test; with the CI flag it fails."""
    missing = None
    try:
        anthropic = importlib.import_module("anthropic")
        httpx2 = importlib.import_module("httpx2")
    except ImportError as exc:
        missing = f"the ai extra is not installed ({exc.name} cannot be imported)"
    else:
        if not hasattr(httpx2, "MockTransport"):
            missing = "httpx2 has no MockTransport"
    if missing is not None:
        if os.environ.get(REQUIRE_SDK_TESTS) == "1":
            pytest.fail(f"{REQUIRE_SDK_TESTS}=1 but the real-SDK tests cannot run: {missing}. "
                        f"Install with: pip install -e '.[dev,app,ai]'", pytrace=False)
        pytest.skip(f"{missing}; install the ai extra: pip install -e '.[ai]'")
    return anthropic, httpx2


def mocked_provider(handler, config: AIConfig | None = None):
    anthropic, httpx2 = real_sdk()
    client = anthropic.Anthropic(api_key=KEY, max_retries=0, http_client=httpx2.Client(
        transport=httpx2.MockTransport(handler)))
    return AnthropicProvider(config or AIConfig(), client=client, sdk=anthropic)


def api_message(text: str, stop_reason: str = "end_turn") -> dict:
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10}}


def test_real_sdk_request_and_accepted_answer(supplier_case):
    result, exc = supplier_case
    seen = []

    def handler(request):
        seen.append(request)
        anthropic, httpx2 = real_sdk()
        return httpx2.Response(200, json=api_message(json.dumps(valid_answer(exc.exception_id))),
                               headers={"request-id": "req_real"})

    outcome = explain_exception(result, exc, mocked_provider(handler), config=AIConfig())
    assert (outcome.status, outcome.request_id) == ("accepted", "req_real")
    [request] = seen
    body = json.loads(request.content)
    assert request.headers["x-api-key"] == KEY and KEY not in request.content.decode()
    assert body == json.loads(preview_request(result, exc, AIConfig()))


@pytest.mark.parametrize("status, code", [(401, "authentication"), (403, "authentication"), (429, "rate_limited"),
                                          (500, "provider_error"), (400, "provider_error")])
def test_real_sdk_status_errors(supplier_case, status, code):
    result, exc = supplier_case

    def handler(request):
        anthropic, httpx2 = real_sdk()
        return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "details"}},
                               headers={"request-id": "req_status"})

    outcome = explain_exception(result, exc, mocked_provider(handler), config=AIConfig())
    assert (outcome.status, outcome.error_code, outcome.request_id) == ("error", code, "req_status")


@pytest.mark.parametrize("failure, code", [("timeout", "timeout"), ("connect", "connection")])
def test_real_sdk_transport_failures(supplier_case, failure, code):
    result, exc = supplier_case

    def handler(request):
        anthropic, httpx2 = real_sdk()
        if failure == "timeout":
            raise httpx2.ReadTimeout("slow", request=request)
        raise httpx2.ConnectError("unreachable", request=request)

    outcome = explain_exception(result, exc, mocked_provider(handler), config=AIConfig())
    assert (outcome.status, outcome.error_code) == ("error", code)


def test_real_sdk_refusal_is_an_error(supplier_case):
    result, exc = supplier_case

    def handler(request):
        anthropic, httpx2 = real_sdk()
        return httpx2.Response(200, json={**api_message(""), "content": [], "stop_reason": "refusal"})

    outcome = explain_exception(result, exc, mocked_provider(handler), config=AIConfig())
    assert (outcome.status, outcome.error_code) == ("error", "refused")


def test_real_sdk_client_ignores_a_base_url_from_the_environment(monkeypatch):
    anthropic, _ = real_sdk()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.example")
    provider = AnthropicProvider(AIConfig(), environ={API_KEY_VARIABLE: KEY}, sdk=anthropic)
    assert provider._client.base_url.host == "api.anthropic.com"


# ---- the real-SDK tests are mandatory in CI ----------------------------------------------------------------------

def _without_sdk(monkeypatch, missing: str):
    real_import = importlib.import_module

    def fake_import(name, *args):
        if name == missing:
            raise ImportError(f"No module named {name!r}", name=name)
        if name == "httpx2" and missing == "MockTransport":
            return SimpleNamespace()  # an httpx2 without MockTransport
        if name in ("anthropic", "httpx2"):
            return SimpleNamespace(MockTransport=object)
        return real_import(name, *args)

    monkeypatch.setattr(importlib, "import_module", fake_import)


@pytest.mark.parametrize("missing", ["anthropic", "httpx2", "MockTransport"])
def test_without_the_ci_flag_missing_sdk_support_skips(monkeypatch, missing):
    monkeypatch.delenv(REQUIRE_SDK_TESTS, raising=False)
    _without_sdk(monkeypatch, missing)
    with pytest.raises(pytest.skip.Exception):
        real_sdk()


@pytest.mark.parametrize("missing", ["anthropic", "httpx2", "MockTransport"])
def test_with_the_ci_flag_missing_sdk_support_fails(monkeypatch, missing):
    monkeypatch.setenv(REQUIRE_SDK_TESTS, "1")
    _without_sdk(monkeypatch, missing)
    with pytest.raises(pytest.fail.Exception, match="real-SDK tests cannot run"):
        real_sdk()


def test_ci_requires_the_real_sdk_tests():
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    install, tests = workflow.split("- name: Run tests", 1)
    assert 'pip install -e ".[dev,app,ai]"' in install
    assert f'{REQUIRE_SDK_TESTS}: "1"' in tests.split("- name:", 1)[0]


def test_every_real_sdk_test_goes_through_the_guard():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    real_tests = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_real_sdk")]
    assert len(real_tests) == 5  # 10 test cases with their parameters: the original 9 and the base-URL test
    for node in real_tests:
        called = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert called & {"real_sdk", "mocked_provider"}, node.name
