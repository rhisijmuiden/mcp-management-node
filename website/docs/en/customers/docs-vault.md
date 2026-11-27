# Documentation and password vault

## Documentation

**Knowledge & admin → Documentation** keeps the knowledge about each customer in one place, for your team and for
the AI.

1. Click **New page**, choose the customer and a category (network, procedure, notes, contacts, how-to, hardware,
   software, security, other). Start from a template if you like: network plan, procedure, contacts, hardware list.
2. Write in Markdown in the editor; the preview next to it shows the result.
3. Link the machines the page is about, add tags, and pin important pages to the top.
4. Add attachments (drawings, PDFs, exports up to 25 MB). They are stored encrypted.

Every save keeps the previous version (the last 100), so you can always look back and restore one. If two people
save the same page at the same time, the second one gets a warning instead of overwriting.

**For the AI:** before it works on a customer, the AI sees the list of pages and can read them. Switch off *The AI may
read this page* for pages the AI should never see. With the scope `docs` the AI can also write pages, for example
add a dated line to a log page.

## Password vault

**Knowledge & admin → Password vault** holds the credentials of every customer: logins, servers, network devices,
databases, API keys, WiFi, certificates, licences and secure notes.

### Adding an item

1. Click **New credential**, choose the customer and the kind.
2. Fill in username, password (or use the **generator**), URL and notes. Extra fields can be secret or not.
3. Link the machines and, if you want a reminder, a rotation period. The list shows which items are due.

Changing a password keeps the previous ones (10 by default).

### Who sees what

- **People**: everyone sees names and usernames. Admins see and copy secrets after typing their password again
  (unlocked for 15 minutes).
- **The AI**: sees the vault *without* secrets. Using a secret always needs an admin's approval, whatever the
  policies say, and an item can be set to *Never* for the AI.
    - The preferred way is **run with credential**: after approval, a script runs on the machine with the username
      and password in its environment. The secret never passes through the AI and is wiped from the output.
    - **Reveal** asks for approval first; once approved the AI may read the secret once, within 15 minutes.

Every reveal, copy, use and change is in the **access log**, including failed attempts and who approved what. Open
AI grants can be withdrawn.

### How it is protected

Every item has its own random key and is encrypted with AES-256-GCM. Those keys are protected by the vault key, a
file outside the database (`/var/lib/mcp-management-node/vault.key`), so a database dump alone reveals nothing. The
vault key is included in [app backups](../admin/backups.md).

!!! warning "Set a backup passphrase"
    Without an app backup with a passphrase, losing the server means losing the vault. The vault warns you when a
    backup location has no passphrase.
