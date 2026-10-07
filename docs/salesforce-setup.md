# Salesforce setup (Week 0)

Everything here is free. It takes about 45 minutes, most of it waiting for emails and for the
Connected App to become active. **Do this yourself.** Nothing in this repo signs up for or
calls Salesforce.

Week 0 is done when `sf org display` works against your org and a JWT login works without
a browser. The Python JWT login comes with the real client in push 3/3.

> Salesforce renames Setup pages fairly often. If a label below doesn't match what you see,
> search Setup's Quick Find for the key word (for example "App Manager" or "External Client App").

## 1. Sign up for a Developer Edition org

1. Go to <https://developer.salesforce.com/signup>.
2. Use your real email. The **username** has to look like an email and be unique across
   all of Salesforce, for example `you.billing@dev.example`. It doesn't need to be a real
   inbox. Write it down.
3. Confirm the email, set a password, and log in once in the browser.
4. Note your **My Domain** URL (Setup → My Domain), for example
   `https://yourname-dev-ed.develop.my.salesforce.com`.

## 2. Install the Salesforce CLI (`sf`)

```bash
npm install --global @salesforce/cli   # or the macOS .pkg from developer.salesforce.com/tools/salesforcecli
sf --version
sf update                              # if already installed (this machine had 2.131.7)
```

## 3. Log in once with the browser and deploy the metadata

From this repo's `salesforce/` folder:

```bash
cd salesforce
sf org login web --alias billing-dev --set-default
sf org display --target-org billing-dev            # <- Week 0 check #1

# Deploy Account.Billing_Customer_Id__c, Usage_Summary__c, Invoice__c and the permission set
sf project deploy start --source-dir force-app --target-org billing-dev

# Give your user field-level access. Without it the API can't see the new fields.
sf org assign permset --name Billing_Integration --target-org billing-dev

# Sanity checks
sf sobject describe --sobject Invoice__c --target-org billing-dev | grep -E '"name": "(Invoice_Ext_Id__c|Total__c)"'
sf data query --query "SELECT Id, Billing_Customer_Id__c FROM Account LIMIT 5" --target-org billing-dev
sf org list limits --target-org billing-dev | grep -E 'DailyApiRequests|DataStorageMB'
```

Write down the real API and storage limits from the last command (SCOPE.md says not to
assume them).

## 4. Create a certificate for the JWT bearer flow

The private key never goes in the repo. `.gitignore` blocks `*.key`, `*.pem` and `*.crt`
anyway, but keep the key outside the repo too.

```bash
mkdir -p ~/.salesforce/metering-jwt && cd ~/.salesforce/metering-jwt
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 365 \
  -keyout server.key -out server.crt -subj "/CN=metering-billing-jwt"
chmod 600 server.key
```

You upload `server.crt` (the public certificate) to Salesforce. `server.key` stays on your machine.

## 5. Create the app for headless (JWT) login

Newer orgs push you toward **External Client Apps**, and creating classic Connected Apps
may be turned off by default. Either works for the JWT bearer flow. Use whichever your org
offers.

**Option A: External Client App** (Setup → Quick Find "External Client App Manager" → *New External Client App*)
1. Name: `Metering Billing Sync`. Contact email: yours. Distribution state: **Local**.
2. **API (Enable OAuth Settings)**: on.
   - Callback URL: `http://localhost:1717/OauthRedirect` (not used by JWT, but required)
   - OAuth scopes: **Manage user data via APIs (api)** and
     **Perform requests at any time (refresh_token, offline_access)**
   - Flow enablement: **Enable JWT Bearer Flow**, then upload `server.crt`.
3. Save. Then open the app's **Policies** tab → OAuth policies → Permitted users:
   **Admin approved users are pre-authorized**, and add your profile
   (System Administrator) or a permission set under the app's policies.
4. **Settings** tab → OAuth settings → *Consumer Key and Secret*. Copy the **Consumer Key**.

**Option B: Connected App** (Setup → App Manager → *New Connected App*)
1. Name `Metering Billing Sync`, your email, **Enable OAuth Settings**.
2. Callback URL `http://localhost:1717/OauthRedirect`. **Use digital signatures**, then upload `server.crt`.
3. Scopes: `api` and `refresh_token, offline_access`. Save.
4. *Manage* → *Edit Policies* → Permitted Users: **Admin approved users are pre-authorized**. Save.
   Then *Manage Profiles* (or *Manage Permission Sets*) → add **System Administrator**.
5. *View* → *Manage Consumer Details*. Copy the **Consumer Key**.

Changes can take **2 to 10 minutes** to take effect.

## 6. Test the JWT login (no browser)

```bash
sf org login jwt \
  --username you.billing@dev.example \
  --jwt-key-file ~/.salesforce/metering-jwt/server.key \
  --client-id <CONSUMER_KEY> \
  --instance-url https://login.salesforce.com \
  --alias billing-dev-jwt
sf org display --target-org billing-dev-jwt         # <- Week 0 check #2
```

## 7. Put the settings in `.env` (never commit it)

```bash
cp .env.example .env
# SF_USERNAME=you.billing@dev.example
# SF_CONSUMER_KEY=<CONSUMER_KEY>
# SF_PRIVATE_KEY_PATH=~/.salesforce/metering-jwt/server.key
# SF_DOMAIN=login
```

The Python client (push 3/3) reads these, for example with simple-salesforce:
`Salesforce(username=..., consumer_key=..., privatekey_file=..., domain="login")`.

For the optional manual GitHub Actions workflow, store the same values as **repository
secrets** (`SF_USERNAME`, `SF_CONSUMER_KEY`, `SF_PRIVATE_KEY` with the key file's
*contents*) and write the key to a temp file inside the job. Normal CI never uses them.

## Troubleshooting

| Error | Usual cause |
|-------|-------------|
| `user hasn't approved this consumer` | Permitted users isn't set to *Admin approved users are pre-authorized*, or your profile/permission set isn't added to the app |
| `invalid_grant: audience is invalid` | Wrong login URL. A Developer Edition org uses `https://login.salesforce.com` (`test.salesforce.com` is for sandboxes only) |
| `invalid_client_id` | Consumer Key typo, or the app hasn't finished propagating (wait about 10 minutes) |
| `INVALID_FIELD` / field missing over the API | The `Billing_Integration` permission set isn't assigned (step 3) |
| `DUPLICATE_VALUE` on an External ID | Expected for a plain *insert*. Always *upsert* by the External ID |
| `REQUEST_LIMIT_EXCEEDED` | Daily API limit hit. Sync aggregates only, and use Bulk API 2.0 |
