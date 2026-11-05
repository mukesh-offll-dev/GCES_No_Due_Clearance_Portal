# Task: Add "Open No Due Portal" single sign-on to the Hostel Admin App

You are working in the **Hostel Admin App** codebase. Add a way for a logged-in
hostel admin to open the **GCES No Due Clearance Portal** already signed in as
the **Hostel Office**, without typing the portal's username and password.

The No Due Portal side is already built. Your job is only the Hostel Admin App
side: build a signed, single-use link (or auto-submitting form) and send the
admin's browser to it.

First, inspect this codebase to learn its language, framework, how admins log
in, how configuration and secrets are loaded (env vars or a config file), and
where the admin navigation/dashboard lives. Follow the existing conventions.

---

## 1. What the portal expects

**Endpoint**

```
POST {NODUE_PORTAL_URL}/sso/hostel/     ← preferred (auto-submitting HTML form)
GET  {NODUE_PORTAL_URL}/sso/hostel/?ts=…&nonce=…&user=…&sig=…   ← also accepted
```

`NODUE_PORTAL_URL` is `https://nodue.gces.net.in` in production. Make it
configurable (e.g. env var `NODUE_PORTAL_URL`) so it can point at a test server.
Do not add a trailing slash to the base URL; the path is exactly `/sso/hostel/`
(with the trailing slash).

Prefer **POST** because web servers write query strings into access logs. GET
works too.

**Parameters** (form fields for POST, query string for GET)

| Name    | Required | Rules |
|---------|----------|-------|
| `ts`    | yes | Current Unix time in **seconds**, UTC, as a decimal string, e.g. `1730803200` |
| `nonce` | yes | Fresh random value for **every** link. 16–128 chars, only `A-Z a-z 0-9 _ -`. Use a CSPRNG, e.g. 24 random bytes → URL-safe base64 without padding |
| `user`  | no  | The hostel admin's username, for the portal's audit log. 0–64 chars, only `A-Z a-z 0-9 . _ @ -`. If the username has other characters, sanitize/replace them or send an empty string. Send `""` or omit it if unknown |
| `sig`   | yes | **Lowercase hex** HMAC-SHA256 of the message below, keyed with the shared secret (64 hex chars) |

**Message to sign** — exactly four lines joined by a single `\n` (LF), no
trailing newline, UTF-8:

```
HOSTEL_SSO
<ts>
<nonce>
<user>
```

If `user` is not sent, the last line is the empty string (the message then ends
with `\n` after the nonce). The `user` value you sign must be byte-for-byte the
value you send.

**Portal behaviour**

- Valid → portal starts a Hostel Office session and redirects (302) to `/hostel/`.
- Invalid signature, older than 60 s, more than 30 s in the future, malformed,
  or the nonce was already used → **403** "This sign-in link is not valid" page.
- Portal has no secret configured → **404**.
- Every link works **once**. Generate a new one on every click; never cache,
  reuse, email or store links.

## 2. Test vector (use it in a unit test)

```
secret = "test-hostel-sso-secret-0123456789abcdef"
ts     = "1700000000"
nonce  = "abcdefghijklmnopqrstuvwx"
user   = "warden01"
sig    = "65c0e08c301d10c0eea23b62e294a6e99ec0a849848d84d516c346dea40a091b"
```

Your signing function must produce exactly this `sig` for these inputs.

## 3. What to build

1. **Config**: read `HOSTEL_SSO_SECRET` (and `NODUE_PORTAL_URL`) from the app's
   server-side configuration/env. Never hardcode the secret, never commit it,
   never send it to the browser or log it. If the secret is missing, hide or
   disable the button and log a clear server-side error.
2. **Signer**: a small server-side helper `build_hostel_sso_params(username)`
   that returns `{ts, nonce, user, sig}` using the rules above.
3. **Route**: a server-side route in the Hostel Admin App, e.g.
   `GET /nodue-portal` (name it to fit the app), that:
   - requires the user to be logged in to the Hostel Admin App **and** to have
     the hostel admin role/permission (reuse the app's existing auth guard —
     this check is the security boundary: whoever can reach this route gets
     Hostel Office access in the No Due Portal);
   - generates fresh params at request time;
   - returns a small HTML page with an auto-submitting POST form to
     `{NODUE_PORTAL_URL}/sso/hostel/` (template below), with
     `Cache-Control: no-store` and `Referrer-Policy: no-referrer` headers.
4. **Button/link**: add "Open No Due Portal" to the admin navigation or
   dashboard, pointing at that route. Opening in a new tab is fine
   (`target="_blank" rel="noopener"`).
5. **Tests**: unit-test the signer with the test vector; test that the route
   rejects anonymous / non-admin users and that the rendered form contains the
   four fields and the correct action URL.

**Auto-submit page template** (escape values with the framework's HTML escaping):

```html
<!doctype html>
<html><head><meta charset="utf-8"><title>Opening No Due Portal…</title></head>
<body>
  <form id="sso" method="POST" action="{NODUE_PORTAL_URL}/sso/hostel/">
    <input type="hidden" name="ts" value="{ts}">
    <input type="hidden" name="nonce" value="{nonce}">
    <input type="hidden" name="user" value="{user}">
    <input type="hidden" name="sig" value="{sig}">
    <noscript><button type="submit">Continue to No Due Portal</button></noscript>
  </form>
  <script>document.getElementById("sso").submit();</script>
</body></html>
```

If the app has a Content-Security-Policy that blocks inline scripts, use its
nonce mechanism or show the visible "Continue" button instead.

## 4. Reference signers

**Python**
```python
import hmac, hashlib, secrets, time

def build_hostel_sso_params(secret: str, username: str = "") -> dict:
    ts = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    msg = f"HOSTEL_SSO\n{ts}\n{nonce}\n{username}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {"ts": ts, "nonce": nonce, "user": username, "sig": sig}
```

**Node.js**
```js
const crypto = require("crypto");
function buildHostelSsoParams(secret, username = "") {
  const ts = Math.floor(Date.now() / 1000).toString();
  const nonce = crypto.randomBytes(24).toString("base64url");
  const sig = crypto.createHmac("sha256", secret)
    .update(["HOSTEL_SSO", ts, nonce, username].join("\n"), "utf8")
    .digest("hex");
  return { ts, nonce, user: username, sig };
}
```

**PHP**
```php
function build_hostel_sso_params(string $secret, string $username = ""): array {
    $ts = (string) time();
    $nonce = rtrim(strtr(base64_encode(random_bytes(24)), '+/', '-_'), '=');
    $sig = hash_hmac('sha256', "HOSTEL_SSO\n$ts\n$nonce\n$username", $secret);
    return ['ts' => $ts, 'nonce' => $nonce, 'user' => $username, 'sig' => $sig];
}
```

## 5. Rules

- Do **not** change the No Due Portal; only the Hostel Admin App.
- Generate params on the **server**, per click. No client-side signing.
- The server clock must be correct (NTP); links expire after 60 seconds.
- Do not log `sig` or full SSO URLs.
- Keep the change small and in the app's existing style.

## 6. Done when

- The signer unit test passes with the test vector.
- Logged-in hostel admins see "Open No Due Portal"; others cannot reach the route.
- Clicking it lands on the portal's Hostel Office dashboard (`/hostel/`) without a login form.
- Refreshing the portal's `/sso/hostel/` page or reusing the same form shows the portal's 403 page (expected: links are single-use).

Report back: files changed, the env vars the operator must set
(`HOSTEL_SSO_SECRET`, `NODUE_PORTAL_URL`), and how you tested.
