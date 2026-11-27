# App backups and restore

These are backups **of Stackpilot itself**: its database and data files, including the keys of the password vault and
the agents' certificate authority. (Backups of your customers' machines are under
[Customer backups](../care/backups.md).)

## Full and incremental

- A **full** backup stores everything.
- An **incremental** backup stores only what changed since the previous backup.
- A **restore** takes the full backup plus the incrementals up to the moment you pick.

Every backup has a SHA-256 manifest and can be encrypted with **AES-256-GCM** using your passphrase.

## Set up a backup location, step by step

1. Open **Settings → App backups** and click **Add backup location**.
2. Choose the protocol: **FTP**, **FTPS** (explicit or implicit), **SFTP** or **SCP**, and fill in the server, port,
   folder and sign-in.
3. Set a **passphrase**. Keep it somewhere safe outside Stackpilot: without it a backup can't be restored.
4. Set the schedule, for example a **full backup every Sunday** and an **incremental every night**, and how many full
   chains to keep.
5. Click **Test** and then **Back up now** to see the first one succeed.

!!! warning "Without a backup, no vault"
    The password vault's key lives on the server. If the server is lost and there is no app backup with a passphrase,
    the vault can't be opened again.

## Restore

1. Open **Settings → App backups** and click **Restore** on the location.
2. Stackpilot lists the backups that are there.
3. Pick the moment to go back to. Stackpilot takes the full backup and the incrementals up to it.
4. Confirm. This replaces all current data; Stackpilot restores and restarts.

On a fresh server: install Stackpilot, add the same location and restore from there. From the command line:
`sudo mmn restore`.
