# Code signing the desktop app

`release-desktop.yml` signs both platforms when the secrets below exist, and
builds unsigned (with a notice in the job log) when they do not. Nothing is
committed for signing: certificates and credentials live only in the
repository's GitHub Actions secrets.

What each platform does:

- **macOS** -- the workflow imports a Developer ID Application certificate
  into a throwaway keychain, PyInstaller signs every binary with the
  hardened runtime and `build/entitlements.plist` (`build/postmortem.spec`
  reads `POSTMORTEM_CODESIGN_IDENTITY`), then the `.app` is submitted to
  Apple's notary service, stapled, and only then zipped. Gatekeeper opens
  the result with no dialog, including the copy the in-app updater
  extracts (the staple ticket travels inside the bundle).
- **Windows** -- Azure Trusted Signing signs `Postmortem.exe` before it is
  zipped and packed into the installer, and then the installer itself.
  The certificate never leaves Azure. SmartScreen stops warning once the
  certificate has a little download reputation; Trusted Signing certs
  are short-lived but reputation follows the publisher identity, so it
  carries across renewals.

## One-time setup

### macOS (Apple Developer Program account required)

1. **Certificate.** developer.apple.com -> Certificates -> "+" -> *Developer
   ID Application*. Create the CSR with Keychain Access (Certificate
   Assistant -> Request a Certificate from a Certificate Authority, saved to
   disk), upload it, download the `.cer`, double-click to add it to your
   login keychain.
2. **Export as .p12.** Keychain Access -> My Certificates -> right-click the
   "Developer ID Application: <name> (<TEAMID>)" entry -> Export -> file
   format *.p12*, choose a password. Then in Terminal:
   `base64 -i DeveloperID.p12 | pbcopy`
3. **Notarization credentials.** appleid.apple.com -> Sign-In and Security
   -> App-Specific Passwords -> generate one (name it "postmortem
   notarytool"). Your Team ID is on developer.apple.com -> Membership.
4. **Secrets** (repo -> Settings -> Secrets and variables -> Actions):

   | secret | value |
   |---|---|
   | `MACOS_CERT_P12` | the base64 from step 2 |
   | `MACOS_CERT_PASSWORD` | the .p12 password |
   | `APPLE_ID` | the Apple ID email of the developer account |
   | `APPLE_APP_PASSWORD` | the app-specific password from step 3 |
   | `APPLE_TEAM_ID` | the 10-character Team ID |

### Windows (Azure Trusted Signing)

1. **Azure account** with a subscription (pay-as-you-go is fine; Trusted
   Signing's Basic tier is about 10 USD/month).
2. **Trusted Signing account.** Azure portal -> search "Trusted Signing
   Accounts" -> Create. Pick a region that offers the service (East US,
   West US 2, West Europe, ...), the *Basic* SKU, and a name
   (e.g. `postmortem`). Note the **Account URI** on its overview page --
   that is the `endpoint` (looks like `https://eus.codesigning.azure.net`).
3. **Identity validation.** In the account -> Identity validation -> New
   identity -> *Public*. Individuals can validate as themselves; the name
   entered here is what Windows shows as the publisher. Approval takes
   from hours to a few days, and you may be asked for ID documents.
4. **Certificate profile.** In the account -> Certificate profiles -> Create
   -> type *Public Trust*, pick the validated identity, give it a name
   (e.g. `postmortem-public`). That name is `certificate-profile-name`.
5. **Service principal for CI.** Microsoft Entra ID -> App registrations
   -> New registration (name `postmortem-ci`). On it: Certificates &
   secrets -> New client secret -> copy the VALUE at once (it is shown
   once). Overview shows the Application (client) ID and the Directory
   (tenant) ID.
6. **Give it signing rights.** Back on the Trusted Signing account ->
   Access control (IAM) -> Add role assignment -> role *Trusted Signing
   Certificate Profile Signer* -> assign to the `postmortem-ci` app.
7. **Secrets:**

   | secret | value |
   |---|---|
   | `AZURE_TENANT_ID` | Directory (tenant) ID |
   | `AZURE_CLIENT_ID` | Application (client) ID |
   | `AZURE_CLIENT_SECRET` | the client secret value |
   | `AZURE_TRUSTED_SIGNING_ENDPOINT` | the Account URI, e.g. `https://eus.codesigning.azure.net` |
   | `AZURE_TRUSTED_SIGNING_ACCOUNT` | the Trusted Signing account name |
   | `AZURE_CERTIFICATE_PROFILE` | the certificate profile name |

## Verifying a release

- macOS: `spctl --assess --type execute --verbose=2 Postmortem.app` should
  print `accepted` with `source=Notarized Developer ID`.
- Windows: `Get-AuthenticodeSignature .\Postmortem-Setup-windows.exe`
  should show `Valid` and the validated identity as the subject. The
  workflow's "Show the Windows signatures" step prints the same.
- Both: the signed bytes are what `SHA256SUMS-*.txt` and the build
  attestation cover, since those are computed last.

## Things to know

- The in-app updater is unaffected by signing: it verifies the SHA-256
  from the release, extracts the zip itself, and relaunches. On macOS the
  extracted bundle is the stapled one, so Gatekeeper is happy after an
  update too.
- If notarization is rejected, the job prints Apple's log (the usual
  causes are a binary that was not signed with the hardened runtime, or
  a missing entitlement). The zip is not published in that case.
- Missing Apple notary secrets with a certificate present produce a
  *signed but not notarized* build and a warning -- Gatekeeper still
  warns on that, so treat the warning as a failure to fix.
- Inno Setup's uninstaller stays unsigned (see the workflow comment).
  Windows does not prompt on uninstall, so nothing is user-visible.
