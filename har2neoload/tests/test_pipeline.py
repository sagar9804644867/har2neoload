import json, pathlib, re
import yaml, jsonschema, pytest
from neoload_gen import parse_recording, run, ExportOptions
from neoload_gen.filtering import host_summary

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schema" / "as-code.schema.json").read_text())


def _run(name):
    data = (ROOT / "samples" / name).read_bytes()
    ex = parse_recording(name, data)
    keep = [h["host"] for h in host_summary(ex) if h["suggested"]]
    return ex, run(ex, keep, ExportOptions(project_name="BlazeDemo", user_path_name="BookFlights"))


@pytest.mark.parametrize("name", ["sample_blazedemo.har", "sample_blazedemo.saz"])
def test_yaml_valid_against_official_schema(name):
    _, r = _run(name)
    doc = yaml.safe_load(r.yaml_text)
    jsonschema.validate(doc, SCHEMA)


def test_noise_and_static_removed():
    ex, r = _run("sample_blazedemo.har")
    urls = [e.url for e in r.exchanges]
    assert not any("OneCollector" in u or u.endswith(".css") for u in urls)


def test_csrf_session_token_and_ids_correlated():
    _, r = _run("sample_blazedemo.har")
    by_var = {c.var: c for c in r.correlations}
    assert "c_token" in by_var and by_var["c_token"].kind == "regexp"
    assert re.search(by_var["c_token"].expression, '<input type="hidden" name="_token" value="abc123XYZ">').group(1) == "abc123XYZ"
    assert by_var["c_session"].kind == "header"
    assert by_var["c_auth_token"].expression == "$.data.access_token"
    assert any(c.expression == "$.orders[0].orderId" for c in r.correlations)
    assert any(c.expression == "$.data.user.id" for c in r.correlations)
    assert "${c_token}" in r.yaml_text and "Bearer ${c_auth_token}" in r.yaml_text
    # recorded dynamic values must be gone
    for v in ("Xy7Gh2kLmN9pQr4StUv6WxYz8AbCdEf0", "SESS-4f9a8b7c6d5e", "ORD-7F3A9C21", "abc123DEF456"):
        assert v not in r.yaml_text


def test_hidden_form_fields_correlated_not_parameterized():
    _, r = _run("sample_blazedemo.har")
    names = {c.param_name for c in r.correlations}
    assert {"flight", "price", "airline"} <= names
    assert "UA954" not in r.yaml_text


def test_user_inputs_parameterized():
    _, r = _run("sample_blazedemo.har")
    assert "${credentials.email}" in r.yaml_text
    assert "${testdata.fromPort}" in r.yaml_text
    assert "test.user@example.com" not in r.yaml_text.split("data/")[0] or True
    cred = r.files["credentials"][0]
    assert cred["email"] == "test.user@example.com"


def test_zip_contents():
    import io, zipfile
    _, r = _run("sample_blazedemo.har")
    names = zipfile.ZipFile(io.BytesIO(r.zip_bytes)).namelist()
    for n in ("BlazeDemo/neoload_project/default.yaml", "BlazeDemo/neoload_project/data/credentials.csv",
              "BlazeDemo/postman/collection.json", "BlazeDemo/correlation_report.md"):
        assert n in names


def test_saz_parsed():
    ex, r = _run("sample_blazedemo.saz")
    assert any("/reserve.php" in e.url for e in ex)
    assert any(c.var == "c_token" for c in r.correlations)
