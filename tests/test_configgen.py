"""Tests for the device configuration generator.

They run against the EXAMPLE schema and role templates under configgen/data/.
Replace those with your own device's and adapt the key names used here.

The properties worth guarding are not "does it render a file" - they are the
ones whose failure is SILENT on a device:

  * the value reader must stop at the end of a line, or an empty value swallows
    the next one and the operator is shown a setting the station never had
  * a generated file must be CRLF, must carry no byte-order mark and no tab in a
    value, and must keep the padding on the keys whose oldest firmware matched
    their spacing
  * a key that is mandatory for the role must be refused when missing, because a
    device with a config error skips its persistent backup copy
  * a key that is merely inapplicable must NOT be refused, because real field
    stations carry whole blocks their role ignores
  * the loopback guard must hold even when the socket binds to every interface
"""

import io
import json
import re
from pathlib import Path

import pytest

from configgen import layout, reader, service
from configgen.schema import load as load_schema

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "configgen" / "data" / "templates"


@pytest.fixture(scope="module")
def schema():
    return load_schema()


def read(path):
    with io.open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


# ------------------------------------------------------------------ reader
class TestReader:
    def test_empty_value_does_not_swallow_the_next_line(self):
        """The bug a convenient regex would have: `\\s*` crosses the newline."""
        buf = ("streaming_cli_secret =\r\n\r\n"
               "--- Downlink ---\r\nntrip_port = 2101\r\n")
        assert reader.read_value(buf, "streaming_cli_secret") == ""
        assert reader.read_value(buf, "ntrip_port") == "2101"

    def test_comment_lines_are_skipped(self):
        for prefix in "[#;-":
            buf = "%sstation = A999\r\nstation = A001\r\n" % prefix
            assert reader.read_value(buf, "station") == "A001"

    def test_key_boundary(self):
        """phone_number must not match phone_number2."""
        buf = "phone_number2 = 222\r\nphone_number = 111\r\n"
        assert reader.read_value(buf, "phone_number") == "111"
        assert reader.read_value(buf, "phone_number2") == "222"

    def test_spaces_semicolons_and_equals_are_dropped(self):
        assert reader.read_value("apn    = gprs; swisscom\r\n", "apn") == "gprsswisscom"

    def test_missing_key_is_none(self):
        assert reader.read_value("station = A001\r\n", "ntrip_host") is None

    def test_over_long_value_is_truncated_like_the_firmware(self):
        v = reader.read_value("station = " + "A" * 500 + "\r\n", "station")
        assert 0 < len(v) < 100

    def test_shadow_check_reports_both_values(self):
        buf = ("[survey_in_min_dur = 0 means never]\r\n"
               "survey_in_min_dur = 120\r\n")
        hits = reader.shadow_check(buf, ["survey_in_min_dur"])
        assert "survey_in_min_dur" in hits
        assert hits["survey_in_min_dur"]["assignment_value"] == "120"

    def test_prose_mentioning_a_key_is_not_a_shadow(self):
        buf = "[station names the site]\r\nstation = A001\r\n"
        assert reader.shadow_check(buf, ["station"]) == {}


# ------------------------------------------------------------------ layout
class TestLayout:
    @pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.TXT")), ids=lambda p: p.stem)
    def test_replay_is_byte_identical(self, path):
        """Without this, every generated file carries invisible changes and a
        reviewer cannot tell a real edit from a parser artefact."""
        original = read(path)
        assert layout.render(layout.parse(original)) == original

    def test_substitution_keeps_padding_and_prose(self):
        src = ("T\r\n-\r\n\r\n[a note]\r\n--- Sec ---\r\n"
               "station = A001\r\napn    = old\r\nsecret =\r\n")
        out = layout.render(layout.parse(src), {"station": "B002", "apn": "new"})
        assert "station = B002" in out
        assert "apn    = new" in out          # padding preserved
        assert "secret =\r\n" in out          # empty stays empty
        assert "[a note]" in out and "--- Sec ---" in out

    @pytest.mark.parametrize("text,needle", [
        ("﻿station = A001\r\n", "BOM"),
        ("station = A001\n", "CRLF"),
        ("station\t= A001\r\n", "tab"),
        ("station = " + "A" * 120 + "\r\n", "characters"),
        ("[survey_in_min_dur = 0]\r\nsurvey_in_min_dur = 120\r\n", "shadows"),
    ])
    def test_hazards_are_reported(self, text, needle):
        found = layout.emit_findings(text, keys={"station", "survey_in_min_dur"})
        assert any(needle in f for f in found), found

    def test_clean_text_has_no_findings(self):
        assert not layout.emit_findings("station = A001\r\n", keys={"station"})


# ------------------------------------------------------------------ schema
class TestSchema:
    def test_every_role_has_a_template(self, schema):
        for role in schema.roles:
            assert schema.template_path(role).exists(), role

    def test_three_states_only(self, schema):
        allowed = {"mandatory", "optional", "n/a"}
        for k in schema.keys:
            assert set(k["roles"]) == set(schema.roles), k["key"]
            assert set(k["roles"].values()) <= allowed, k["key"]

    def test_every_role_requires_something(self, schema):
        for role in schema.roles:
            assert schema.mandatory_for(role), role

    def test_form_spec_carries_help_for_every_key(self, schema):
        spec = schema.as_form_spec()
        assert len(spec["keys"]) == len(schema.keys)
        assert all(k["help"] for k in spec["keys"])

    def test_secrets_are_marked(self, schema):
        spec = schema.as_form_spec()
        secret = {k["key"] for k in spec["keys"] if k["secret"]}
        assert "streaming_cli_secret" in secret
        assert "http_api_key" in secret


# ------------------------------------------------------------------ service
class TestGenerate:
    @pytest.mark.parametrize("role", ["batch", "stream", "base", "rover"])
    def test_template_values_reproduce_the_template(self, schema, role):
        values = service.defaults_for(schema, role)
        text, findings = service.generate(schema, role, values)
        assert text == read(schema.template_path(role))
        assert not service.has_errors(findings), findings

    @pytest.mark.parametrize("role", ["batch", "stream", "rover"])
    def test_generated_file_is_crlf_without_bom(self, schema, role):
        text, _ = service.generate(schema, role, service.defaults_for(schema, role))
        assert not text.startswith("﻿")
        assert text.count("\n") == text.count("\r\n")
        assert "\t" not in text

    def test_missing_mandatory_key_is_an_error(self, schema):
        values = service.defaults_for(schema, "batch")
        values["apn"] = ""
        _, findings = service.generate(schema, "batch", values)
        assert any(f["key"] == "apn" and f["level"] == service.ERROR
                   for f in findings), findings

    def test_inapplicable_key_is_not_an_error(self, schema):
        """Real field stations carry blocks their role ignores; refusing them
        would reject working configurations."""
        values = service.defaults_for(schema, "rover")
        values["http_api_key"] = "something"
        _, findings = service.generate(schema, "rover", values)
        levels = {f["level"] for f in findings if f["key"] == "http_api_key"}
        assert service.ERROR not in levels
        assert levels <= {service.INFO}

    def test_role_and_operation_mode_must_agree(self, schema):
        values = service.defaults_for(schema, "rover")
        values["operation_mode"] = "0"
        _, findings = service.generate(schema, "rover", values)
        assert any(f["key"] == "operation_mode" and f["level"] == service.ERROR
                   for f in findings), findings

    def test_out_of_range_value_is_refused(self, schema):
        values = service.defaults_for(schema, "stream")
        values["streaming_heartbeat_interval"] = "9999"
        _, findings = service.generate(schema, "stream", values)
        assert any(f["key"] == "streaming_heartbeat_interval" and f["level"] == service.ERROR
                   for f in findings), findings

    def test_over_long_secret_is_refused(self, schema):
        """The device field is fixed-size; too long is silently truncated
        there, so it has to be caught here."""
        values = service.defaults_for(schema, "stream")
        values["streaming_cli_secret"] = "x" * 64
        _, findings = service.generate(schema, "stream", values)
        assert any(f["key"] == "streaming_cli_secret" and f["level"] == service.ERROR
                   for f in findings), findings

    def test_prefill_skips_keys_the_role_ignores(self, schema):
        values = service.defaults_for(schema, "rover")
        service.apply_prefill(schema, "rover", values,
                              {"http_api_key": "leak", "streaming_server_port": "9000"})
        assert "http_api_key" not in values or values["http_api_key"] != "leak"
        assert values["streaming_server_port"] == "9000"


class TestTemplateOverride:
    """A role can have more than one layout.

    A base either surveys its own position or is told where it stands, and the
    two files read very differently. Without a way to pick, the second one is
    unreachable through generate() - which is an invitation to render it by hand
    and lose every check that lives in there. That is not hypothetical: doing
    exactly that produced files carrying the template's foreign phone numbers,
    because the rename/drop rule lives in generate() and nowhere else."""

    def test_a_different_layout_can_be_chosen_for_the_same_role(self, schema, tmp_path):
        surveyin = tmp_path / "base-surveyin.TXT"
        surveyin.write_bytes(read(schema.template_path("base")).replace(
            "base_mode = fixed\r\n",
            "base_mode = survey_in\r\nsurvey_in_min_dur = 300\r\n").encode("utf-8"))
        values = service.form_defaults(schema, "base", template=str(surveyin))
        values["base_mode"] = "survey_in"
        text, findings = service.generate(schema, "base", values,
                                          template=str(surveyin))
        assert not service.has_errors(findings), findings
        assert reader.read_value(text, "base_mode") == "survey_in"
        assert reader.read_value(text, "survey_in_min_dur") is not None

    def test_an_override_still_obeys_the_switched_off_key_rule(self, schema):
        """The whole point of routing through generate() instead of the
        renderer: the rules do not become optional because the layout did."""
        other = schema.template_path("rover")
        values = service.form_defaults(schema, "rover", template=str(other))
        text, _ = service.generate(schema, "rover", values, template=str(other))
        assert "phone_xnumber2" not in text
        assert reader.read_value(text, "phone_number2") is None


class TestSwitchedOffKeys:
    """The extra phone numbers are switched off by RENAMING them.

    The template ships the disabled spelling, so a value typed into the live
    field matched no line and was silently dropped - the generated file kept
    the template's number instead of the operator's. That is the exact failure
    shape this tool exists to prevent, so it gets its own tests."""

    def test_a_filled_number_reaches_the_file(self, schema):
        values = service.form_defaults(schema, "batch")
        values["phone_number2"] = "0791234567"
        text, _ = service.generate(schema, "batch", values)
        assert reader.read_value(text, "phone_number2") == "0791234567"
        assert reader.read_value(text, "phone_xnumber2") is None

    def test_an_empty_number_produces_no_line_at_all(self, schema):
        values = service.form_defaults(schema, "batch")
        text, _ = service.generate(schema, "batch", values)
        assert reader.read_value(text, "phone_number2") is None
        assert reader.read_value(text, "phone_xnumber2") is None
        assert "phone_number2" not in text and "phone_xnumber2" not in text

    def test_the_template_number_is_not_offered_as_a_starting_value(self, schema):
        """It belongs to somebody; handing it to the form puts it in a
        customer's file the moment nobody looks."""
        values = service.form_defaults(schema, "batch")
        assert "phone_xnumber2" not in values
        assert "phone_xnumber3" not in values

    def test_the_primary_number_is_still_required(self, schema):
        for role in schema.roles:
            assert schema.state("phone_number", role) == "mandatory", role
        values = service.form_defaults(schema, "batch")
        values["phone_number"] = ""
        _, findings = service.generate(schema, "batch", values)
        assert any(f["key"] == "phone_number" and f["level"] == service.ERROR
                   for f in findings), findings

    def test_a_renamed_line_does_not_inherit_the_old_padding(self, schema):
        values = service.form_defaults(schema, "batch")
        values["phone_number2"] = "079"
        text, _ = service.generate(schema, "batch", values)
        assert "phone_number2 = 079\r\n" in text


class TestNoMaskedFields:
    """Every input is shown in clear text.

    Masking protected nothing - the value is in the preview, the download and on
    the card in clear text anyway - while making a token impossible to check by
    eye. The SSH tunnel to loopback is the boundary."""

    def test_the_form_has_no_password_inputs(self):
        page = (REPO / "configgen" / "static" / "index.html").read_text(
            encoding="utf-8")
        assert 'type="password"' not in page

    def test_the_schema_still_marks_which_values_are_sensitive(self, schema):
        # kept as metadata even though the form no longer hides them
        assert any(k["type"] == "secret" for k in schema.keys)


class TestUploadTargetChoice:
    """The upload block branches on upload_protocol, and both halves of that
    branch have a trap the device cannot warn about."""

    def test_the_section_declares_its_choice(self, schema):
        section = next(s for s in schema.sections if s["id"] == "upload")
        assert section["choice"]["key"] == "upload_protocol"
        assert set(section["choice"]["groups"].values()) == {"ftp", "http"}

    def test_every_upload_key_is_grouped_or_deliberately_not(self, schema):
        ungrouped = {k["key"] for k in schema.keys
                     if k["section"] == "upload" and not k.get("group")}
        # the two that steer the choice itself belong to neither side
        assert ungrouped == {"enable_upload", "upload_protocol"}

    def test_http_selected_without_a_server_is_an_error(self, schema):
        """The firmware never counts the http_* keys, in any role - a batch
        station set to HTTP with no server passes every check the device makes
        and then has nowhere to put its files."""
        values = service.form_defaults(schema, "batch")
        values["upload_protocol"] = "1"
        values["http_server"] = ""
        _, findings = service.generate(schema, "batch", values)
        assert any(f["key"] == "http_server" and f["level"] == service.ERROR
                   for f in findings), findings

    def test_http_selected_with_a_server_is_fine(self, schema):
        values = service.form_defaults(schema, "batch")
        values["upload_protocol"] = "1"
        values["http_server"] = "files.example.org"
        _, findings = service.generate(schema, "batch", values)
        assert not any(f["key"] == "http_server" and f["level"] == service.ERROR
                       for f in findings), findings

    def test_ftp_keys_stay_mandatory_under_http(self, schema):
        """cfgUploadKeysRequired() asks the ROLE, never the protocol. Dropping
        the FTP keys because the station uploads over HTTP still costs it its
        persistent config backup."""
        values = service.form_defaults(schema, "batch")
        values["upload_protocol"] = "1"
        values["http_server"] = "files.example.org"
        values["server_ftp"] = ""
        _, findings = service.generate(schema, "batch", values)
        assert any(f["key"] == "server_ftp" and f["level"] == service.ERROR
                   for f in findings), findings

    def test_a_key_is_not_reported_twice(self, schema):
        values = service.form_defaults(schema, "batch")
        values["upload_protocol"] = "0"
        values["server_ftp"] = ""
        _, findings = service.generate(schema, "batch", values)
        errs = [f for f in findings
                if f["key"] == "server_ftp" and f["level"] == service.ERROR]
        assert len(errs) == 1, errs

    def test_streaming_roles_are_left_alone(self, schema):
        """They have no HTTP and no FTP path at all."""
        values = service.form_defaults(schema, "stream")
        values["streaming_cli_secret"] = ""
        _, findings = service.generate(schema, "stream", values)
        assert not [f for f in findings
                    if f["key"] in ("http_server", "server_ftp")
                    and f["level"] == service.ERROR], findings


class TestImport:
    def test_round_trip_preserves_every_value(self, schema):
        source = read(schema.template_path("rover"))
        role, values, _ = service.import_text(schema, source)
        assert role == "rover"
        text, _ = service.generate(schema, role, values)
        for key, want in values.items():
            assert reader.read_value(text, key) == want, key

    def test_lf_only_input_is_flagged_and_repaired(self, schema):
        source = read(schema.template_path("batch")).replace("\r\n", "\n")
        role, values, findings = service.import_text(schema, source)
        assert any("CRLF" in f["message"] for f in findings)
        text, _ = service.generate(schema, role, values)
        assert text.count("\n") == text.count("\r\n")

    def test_bom_is_flagged(self, schema):
        source = "﻿" + read(schema.template_path("batch"))
        _, _, findings = service.import_text(schema, source)
        assert any("byte-order mark" in f["message"] for f in findings)

    def test_obsolete_key_is_explained_not_silently_dropped(self, schema):
        source = read(schema.template_path("stream")) + "legacy_upload_mode = 1\r\n"
        _, values, findings = service.import_text(schema, source)
        assert "legacy_upload_mode" not in values
        assert any(f["key"] == "legacy_upload_mode" and "no longer read"
                   in f["message"] for f in findings), findings

    def test_disabled_spelling_is_recognised(self, schema):
        _, _, findings = service.import_text(
            schema, read(schema.template_path("batch")))
        assert any(f["key"] == "phone_xnumber2" and "disabled" in f["message"]
                   for f in findings), findings

    def test_template_inherited_keys_are_named(self, schema):
        """A key the file does not set is written with the template's value.

        For anything that states a fact about the station rather than
        configuring it - the antenna model, which ends up in the header of every
        log file - that is a claim nobody made. Naming them is what turns it
        into a decision instead of a default."""
        source = read(schema.template_path("batch"))
        source = re.sub(r"^antenna[ \t]*=[^\r\n]*\r\n", "", source, flags=re.M)
        _, values, findings = service.import_text(schema, source)
        assert "antenna" not in values
        assert any("absent from the file" in f["message"] and "antenna" in f["message"]
                   for f in findings), findings

    def test_unknown_key_is_reported(self, schema):
        source = read(schema.template_path("batch")) + "not_a_real_key = 7\r\n"
        _, _, findings = service.import_text(schema, source)
        assert any(f["key"] == "not_a_real_key" for f in findings)

    def test_shadowed_key_is_reported_with_both_values(self, schema):
        source = read(schema.template_path("base")).replace(
            "base_mode = fixed", "[base_mode = survey_in]\r\nbase_mode = fixed")
        _, _, findings = service.import_text(schema, source)
        assert any(f["key"] == "base_mode" and "survey_in" in f["message"]
                   for f in findings), findings

    @pytest.mark.parametrize("values,expected", [
        ({}, "batch"),
        ({"operation_mode": "1"}, "base"),
        ({"operation_mode": "1", "rtcm_client": "1"}, "rover"),
        ({"station_mode": "rover"}, "rover"),
        ({"station_mode": "stream", "operation_mode": "0"}, "stream"),
    ])
    def test_role_derivation_matches_the_firmware(self, schema, values, expected):
        """An explicit role wins; without it the old pair decides, and that
        pair cannot express 'stream' at all - base is what you get by not
        asking. Reproducing the gap is what makes the import faithful."""
        assert service.derive_role(schema, values) == expected


# ------------------------------------------------------------------ routes
@pytest.fixture(scope="module")
def client():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from streaming import config as stream_config
    from configgen.router import build_router

    app = fastapi.FastAPI()
    app.include_router(build_router(stream_config), prefix="/config")
    # TestClient reports a non-IP client host, which the loopback guard would
    # refuse - as it should. Present as loopback so the ROUTES can be tested;
    # the guard itself is tested separately below.
    #
    # Closed on teardown: TestClient runs the app on a portal thread, and a
    # module-scoped client that is never closed leaves that thread alive for the
    # rest of the session - which is enough to starve a later async end-to-end
    # test of the timing it needs.
    with TestClient(app, client=("127.0.0.1", 12345)) as c:
        yield c


class TestRoutes:
    def test_schema_route(self, client):
        r = client.get("/config/schema")
        assert r.status_code == 200
        body = r.json()
        assert body["keys"] and body["roles"] and body["sections"]

    def test_prefill_says_when_public_host_is_unset(self, client):
        body = client.get("/config/prefill").json()
        assert ("note" in body)
        if not body["public_host_configured"]:
            assert "PUBLIC_HOST" in body["note"]

    def test_defaults_for_each_role(self, client):
        for role in client.get("/config/schema").json()["roles"]:
            r = client.get("/config/defaults/%s" % role)
            assert r.status_code == 200, role
            assert r.json()["values"]

    def test_unknown_role_is_404(self, client):
        assert client.get("/config/defaults/nope").status_code == 404

    def test_generate_then_download(self, client):
        values = client.get("/config/defaults/stream").json()["values"]
        values["streaming_cli_secret"] = ""      # keep it inside the device field
        body = {"role": "stream", "values": values}
        gen = client.post("/config/generate", json=body).json()
        assert gen["ok"], [f for f in gen["findings"] if f["level"] == "error"]
        dl = client.post("/config/download", json=body)
        assert dl.status_code == 200
        assert "attachment" in dl.headers["content-disposition"]
        assert dl.text == gen["file"]

    def test_download_refuses_a_file_with_errors(self, client):
        r = client.post("/config/download", json={"role": "batch", "values": {}})
        assert r.status_code == 422

    def test_import_route(self, client, schema):
        r = client.post("/config/import",
                        content=read(schema.template_path("rover")))
        assert r.status_code == 200
        assert r.json()["role"] == "rover"

    def test_page_is_served(self, client):
        r = client.get("/config/")
        assert r.status_code == 200 and "<form" in r.text


@pytest.fixture(scope="module")
def remote_client():
    """A client that is not on loopback - RFC 5737 documentation address."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from streaming import config as stream_config
    from configgen.router import build_router

    app = fastapi.FastAPI()
    app.include_router(build_router(stream_config), prefix="/config")
    with TestClient(app, client=("192.0.2.10", 4444)) as c:
        yield c


class TestLoopbackGuard:
    """The bind address alone is brittle. If someone points the admin API at
    every interface for an unrelated reason, this is what still refuses."""

    @pytest.mark.parametrize("path", ["/config/", "/config/schema", "/config/prefill",
                                      "/config/defaults/batch"])
    def test_get_routes_refuse_a_remote_client(self, remote_client, path):
        r = remote_client.get(path)
        assert r.status_code == 403
        assert "loopback" in r.json()["detail"]

    def test_post_routes_refuse_a_remote_client(self, remote_client):
        for path in ("/config/generate", "/config/download"):
            r = remote_client.post(path, json={"role": "batch", "values": {}})
            assert r.status_code == 403, path
        assert remote_client.post("/config/import", content="x").status_code == 403


# ------------------------------------------------------------------ CLI
class TestCli:
    def test_generates_a_file(self, tmp_path):
        from configgen.__main__ import main
        out = tmp_path / "CONFIG.TXT"
        rc = main(["--role", "stream", "--set", "station=T001",
                   "--set", "streaming_cli_secret=", "--no-prefill", "-o", str(out)])
        assert rc == 0
        text = read(out)
        assert "station = T001" in text
        assert text.count("\n") == text.count("\r\n")

    def test_exits_non_zero_on_errors(self, tmp_path):
        from configgen.__main__ import main
        out = tmp_path / "CONFIG.TXT"
        rc = main(["--role", "batch", "--set", "apn=", "--no-prefill", "-o", str(out)])
        assert rc == 1

    def test_roster(self, tmp_path):
        from configgen.__main__ import main
        roster = tmp_path / "fleet.csv"
        roster.write_text("station,role\nA001,stream\nA002,stream\n", encoding="utf-8")
        out_dir = tmp_path / "out"
        rc = main(["--roster", str(roster), "--out-dir", str(out_dir),
                   "--no-prefill", "--set", "streaming_cli_secret="])
        assert rc == 0
        assert (out_dir / "A001.TXT").exists() and (out_dir / "A002.TXT").exists()
        assert "station = A002" in read(out_dir / "A002.TXT")


class TestCliSecretLimit:
    """The device is the side that authenticates, and its field is fixed-size.

    server.run() warns at startup when STREAM_CLI_SECRET is longer than a device
    can hold; that warning reads the limit out of the schema rather than
    hard-coding it. These pin the contract it depends on - a schema that stopped
    carrying max_len would turn the warning off silently."""

    def test_schema_states_the_device_field_size(self, schema):
        entry = schema.by_key["streaming_cli_secret"]
        assert isinstance(entry.get("max_len"), int) and entry["max_len"] > 1

    def test_a_token_that_would_not_fit_is_refused(self, schema):
        limit = schema.by_key["streaming_cli_secret"]["max_len"]
        values = service.defaults_for(schema, "stream")
        values["streaming_cli_secret"] = "x" * (limit + 1)
        _, findings = service.generate(schema, "stream", values)
        assert any(f["key"] == "streaming_cli_secret" and f["level"] == service.ERROR
                   for f in findings), findings

    def test_a_token_that_fits_is_accepted(self, schema):
        limit = schema.by_key["streaming_cli_secret"]["max_len"]
        values = service.defaults_for(schema, "stream")
        values["streaming_cli_secret"] = "x" * (limit - 1)
        _, findings = service.generate(schema, "stream", values)
        assert not any(f["key"] == "streaming_cli_secret" and
                       f["level"] == service.ERROR for f in findings), findings


# ------------------------------------------------------------------ mirror
def test_no_device_name_in_the_code(schema):
    """The code must not know which device it configures: the name lives in
    the schema only, so swapping configgen/data/ is all it takes to adapt it."""
    name = re.escape(schema.device_name)
    offenders = []
    for path in (REPO / "configgen").rglob("*"):
        if not path.is_file() or "data" in path.relative_to(REPO).parts:
            continue
        if path.suffix not in (".py", ".html"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(name, text, re.I):
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, offenders
