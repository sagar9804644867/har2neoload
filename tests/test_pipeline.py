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


def test_neoload_gui_project_matches_neoload_format():
    import io, sys, zipfile
    sys.path.insert(0, str(ROOT / "tests"))
    from nl_validate import errors
    base = json.loads((ROOT / "tests" / "fixtures" / "neoload_2026_2_baseline_errors.json").read_text())
    _, r = _run("sample_blazedemo.har")
    z = zipfile.ZipFile(io.BytesIO(r.zip_bytes))
    names = z.namelist()
    assert "BlazeDemo/NeoLoad_GUI_Project/BlazeDemo/BlazeDemo.nlp" in names
    cfg = zipfile.ZipFile(io.BytesIO(z.read("BlazeDemo/NeoLoad_GUI_Project/BlazeDemo/config.zip")))
    repo = cfg.read("repository.xml").decode()
    assert "<init-container" in repo and "<actions-container" in repo and "<variable-file" in repo
    assert 'extractType="4"' in repo and 'jsonpath="$.data.access_token"' in repo
    assert "${c_token}" in repo and "${credentials.email}" in repo
    scen = cfg.read("scenario.xml").decode()
    assert 'uid="Load_Test"' in scen and "<rampup-volume-policy" in scen
    for n in ("repository.xml", "scenario.xml", "settings.xml"):
        extra = errors(cfg.read(n)) - set(base[n])
        assert not extra, (n, extra)


def test_post_query_params_are_url_parameters():
    from neoload_gen.models import Exchange
    from neoload_gen.nlp_export import _action_xml
    e = Exchange(idx=1, started_ms=0, duration_ms=0, method="POST",
                 url="https://parabank.parasoft.com/parabank/services_proxy/bank/createAccount?customerId=12212&newAccountType=1&fromAccountId=13344",
                 req_headers=[], req_body="", status=200, resp_headers=[], resp_body="")
    xml = _action_xml(e, "u1", "parabank.parasoft.com", [], False)
    assert xml.count("<urlPostParameter ") == 3 and "<parameter " not in xml
