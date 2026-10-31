# Security

Chat Search indexes local conversation exports and opens the source message for each result. It suppresses request logging so search terms in query strings are not written to the HTTP access log. Runtime folders, database files, and SQLite sidecars are checked for owner-only permissions. Imported conversations remain plaintext on disk. SHA-256 helps identify duplicate imports; it does not make chat content private. Use fake conversations in reports and do not attach real chat exports.

The native app has an ad-hoc code signature. This lets the installer check whether its files changed. It does not prove who made the app. The build is not notarized. Account permissions limit access. Use your Mac’s disk encryption to protect saved data.

Report security issues through [GitHub private vulnerability reporting](https://github.com/gmperkinsmba/chat-search/security/advisories/new).
