"""Build sample recordings (HAR + SAZ) that exercise correlation and parameterization."""
import io, json, zipfile, pathlib

HERE = pathlib.Path(__file__).parent
T0 = "2026-10-09T10:00:{:02d}.000Z"

def entry(sec, method, url, status=200, mime="text/html", body="", req_headers=None, post=None, resp_headers=None, page="page_1"):
    e = {"startedDateTime": T0.format(sec), "time": 120, "pageref": page,
         "request": {"method": method, "url": url, "headers": req_headers or [{"name": "Accept", "value": "text/html"}]},
         "response": {"status": status, "headers": (resp_headers or []) + [{"name": "Content-Type", "value": mime}],
                      "content": {"mimeType": mime, "text": body}}}
    if post is not None:
        e["request"]["postData"] = post
    return e

LOGIN = '<html><head><title>Login</title></head><body><form method="POST" action="/login"><input type="hidden" name="_token" value="Xy7Gh2kLmN9pQr4StUv6WxYz8AbCdEf0"><input name="email"><input name="password"></form></body></html>'
HOME = '<html><head><title>BlazeDemo</title></head><body><select name="fromPort"><option>Paris</option><option>Boston</option></select></body></html>'
RESERVE = '<html><head><title>BlazeDemo - reserve</title></head><body><form action="purchase.php"><input type="hidden" name="flight" value="UA954"><input type="hidden" name="price" value="472.56"><input type="hidden" name="airline" value="United Airlines"></form></body></html>'
CONF = '<html><head><title>BlazeDemo Confirmation</title></head><body><h1>Thank you for your purchase today!</h1><table><tr><td>Id</td><td>1728468320123</td></tr></table></body></html>'
API_LOGIN = json.dumps({"data": {"access_token": "eyJhbGciOiJIUzI1NiJ9.abc123DEF456.sig789", "user": {"id": 98765}}})
API_ORDERS = json.dumps({"orders": [{"orderId": "ORD-7F3A9C21", "status": "NEW"}]})
FORM = {"name": "Content-Type", "value": "application/x-www-form-urlencoded"}
JSONH = {"name": "Content-Type", "value": "application/json"}

entries = [
    entry(0, "GET", "https://blazedemo.com/", body=HOME),
    entry(0, "GET", "https://blazedemo.com/assets/app.css", mime="text/css", body="body{}"),
    entry(1, "POST", "https://browser.events.data.microsoft.com/OneCollector/1.0/", mime="application/json", body="{}"),
    entry(3, "GET", "https://blazedemo.com/login", body=LOGIN, page="page_2"),
    entry(6, "POST", "https://blazedemo.com/login", status=302, body="", req_headers=[FORM],
          post={"mimeType": FORM["value"], "text": "_token=Xy7Gh2kLmN9pQr4StUv6WxYz8AbCdEf0&email=test.user%40example.com&password=Secret%40123"},
          resp_headers=[{"name": "Location", "value": "https://blazedemo.com/home?session=SESS-4f9a8b7c6d5e"}], page="page_3"),
    entry(6, "GET", "https://blazedemo.com/home?session=SESS-4f9a8b7c6d5e", body=HOME, page="page_3"),
    entry(9, "POST", "https://blazedemo.com/reserve.php", req_headers=[FORM],
          post={"mimeType": FORM["value"], "text": "fromPort=Paris&toPort=Buenos+Aires"}, body=RESERVE, page="page_4"),
    entry(13, "POST", "https://blazedemo.com/purchase.php", req_headers=[FORM],
          post={"mimeType": FORM["value"], "text": "flight=UA954&price=472.56&airline=United+Airlines&fromPort=Paris&toPort=Buenos+Aires"},
          body="<html><head><title>BlazeDemo Purchase</title></head><body>form</body></html>", page="page_5"),
    entry(20, "POST", "https://blazedemo.com/confirmation.php", req_headers=[FORM],
          post={"mimeType": FORM["value"], "text": "inputName=Sagar+Test&address=1+MG+Road&city=Bengaluru&creditCardNumber=4111111111111111"},
          body=CONF, page="page_6"),
    entry(24, "POST", "https://api.blazedemo.com/v1/auth/login", mime="application/json", req_headers=[JSONH],
          post={"mimeType": "application/json", "text": json.dumps({"username": "apiuser01", "password": "ApiPass#1"})},
          body=API_LOGIN, page="page_7"),
    entry(25, "GET", "https://api.blazedemo.com/v1/users/98765/orders", mime="application/json",
          req_headers=[{"name": "Authorization", "value": "Bearer eyJhbGciOiJIUzI1NiJ9.abc123DEF456.sig789"},
                       {"name": "Accept", "value": "application/json"}], body=API_ORDERS, page="page_7"),
    entry(27, "GET", "https://api.blazedemo.com/v1/orders/ORD-7F3A9C21?expand=items", mime="application/json",
          req_headers=[{"name": "Authorization", "value": "Bearer eyJhbGciOiJIUzI1NiJ9.abc123DEF456.sig789"},
                       {"name": "Accept", "value": "application/json"}], body='{"orderId":"ORD-7F3A9C21"}', page="page_8"),
]
har = {"log": {"version": "1.2", "creator": {"name": "sample", "version": "1"},
               "pages": [{"id": f"page_{i}", "title": t} for i, t in enumerate(
                   ["", "BlazeDemo", "Login", "Home", "Reserve", "Purchase", "Confirmation", "API", "Order"])],
               "entries": entries}}
(HERE / "sample_blazedemo.har").write_text(json.dumps(har, indent=1))

# Equivalent SAZ (Fiddler) for the web part
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    for n, e in enumerate(entries[:9], start=1):
        r = e["request"]; s = e["response"]
        body = (r.get("postData") or {}).get("text", "")
        hdrs = "".join(f"{h['name']}: {h['value']}\r\n" for h in r["headers"])
        host = r["url"].split("/")[2]
        z.writestr(f"raw/{n:02d}_c.txt", f"{r['method']} {r['url']} HTTP/1.1\r\nHost: {host}\r\n{hdrs}\r\n{body}")
        sh = "".join(f"{h['name']}: {h['value']}\r\n" for h in s["headers"])
        z.writestr(f"raw/{n:02d}_s.txt", f"HTTP/1.1 {s['status']} OK\r\n{sh}\r\n{s['content']['text']}")
        z.writestr(f"raw/{n:02d}_m.xml", f'<Session><SessionTimers ClientBeginRequest="{e["startedDateTime"]}" ClientDoneResponse="{e["startedDateTime"]}"/></Session>')
(HERE / "sample_blazedemo.saz").write_bytes(buf.getvalue())
print("samples written")
