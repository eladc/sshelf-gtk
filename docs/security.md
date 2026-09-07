# Security

## Password storage

### How it works

Passwords are stored in the **OS keychain**, not in SQLite.

When a connection is saved:
1. The password is extracted from the `Connection` object in memory.
2. An empty string is written to the `password` column in SQLite.
3. The real password is stored in Keychain under service `sshelf` and account `<connection_id>`.

When connections are loaded:
1. All rows are fetched from SQLite.
2. For each connection with an empty `password` column, the password is retrieved from Keychain.
3. The `Connection` object in memory has the password populated — the SSH worker uses it without further Keychain calls.

When a connection is deleted:
- The SQLite row is removed.
- The Keychain entry is also deleted.

### Keychain entry format

| Field | Value |
|-------|-------|
| Service | `sshelf` |
| Account | `<connection_id>` (e.g. `42`) |
| Label | Set by the `keyring` library |

You can view and manage these entries in **Keychain Access.app** → search for `sshelf`.

### Legacy plaintext passwords

Connections created before Keychain support (or on a system without `keyring` installed) may have a non-empty `password` column in SQLite. These still work — `_fill_password()` only queries Keychain when the DB column is empty.

**To migrate a legacy connection to Keychain:** open Edit for that connection, confirm (even without changes), and save. The password will be moved to Keychain and blanked in SQLite.

### What if `keyring` is not installed?

`keychain.py` catches the `ImportError` and sets `_AVAILABLE = False`. All Keychain operations become no-ops. Passwords fall back to plaintext SQLite — this is the pre-Keychain behaviour. Install `keyring` to enable secure storage:

```bash
pip install keyring
```

---

## SSH authentication

sshelf supports three authentication methods, tried in order by paramiko:

1. **SSH key file** — `private_key_file` + optional `passphrase`. Supported key types: RSA, ECDSA, Ed25519 (auto-detected by paramiko).
2. **Password** — sent over the encrypted SSH channel.
3. **SSH agent** — if `forward_agent` is enabled and an agent is running, paramiko will try agent keys automatically.

### Passphrase storage

Passphrases for encrypted key files are treated the same as passwords — stored in Keychain, never in plaintext SQLite.

### Jump hosts (ProxyJump)

When a `jump_host` is configured, sshelf:
1. Opens an SSH connection to the jump host.
2. Requests a TCP tunnel through it to the target host.
3. Opens a second SSH session over that tunnel.

This is equivalent to `ssh -J jump_host target_host`. The jump host connection uses the same credential lookup (Keychain, then key file, then agent).

---

## What is NOT stored securely

| Data | Storage | Notes |
|------|---------|-------|
| Passwords | OS keychain (macOS Keychain / GNOME Keyring / Windows Credential Manager) | Secure |
| Passphrases | OS keychain (macOS Keychain / GNOME Keyring / Windows Credential Manager) | Secure |
| SSH key files | Filesystem (your path) | sshelf only stores the path, not the key content |
| Usernames | SQLite (plaintext) | Not sensitive |
| Hostnames / IPs | SQLite (plaintext) | Not sensitive |
| Connection names, groups, colors | SQLite (plaintext) | Not sensitive |
| App preferences | SQLite (plaintext) | Not sensitive |

---

## JSON connection export

**File → Export Connections as JSON…** offers to include passwords in the export file.

- If you choose **No** (default): passwords are omitted and the export is safe to share.
- If you choose **Yes**: passwords are written as **plain text** in the JSON file.  Treat the resulting file like a password file — store it securely, do not commit it to version control, and delete it when no longer needed.

The import path (**File → Import Connections from JSON…**) reads passwords from the JSON and stores them in Keychain, just as if the connection had been saved manually.

---

## Host key verification

sshelf verifies server host keys against your existing `~/.ssh/known_hosts` (plus `/etc/ssh/ssh_known_hosts`), the same file OpenSSH uses — so a host you have already `ssh`'d to from a terminal is trusted without prompting.

- **Unknown host** — you are shown the key's SHA256 fingerprint and asked whether to trust it (trust-on-first-use). The GUI shows a dialog; the CLI asks on the terminal. Accepting appends the key to `~/.ssh/known_hosts`; declining aborts the connection.
- **Changed host key** — the connection is always refused with a warning, and no prompt is offered. This is the man-in-the-middle case. If the host was legitimately rebuilt or rekeyed, drop the stale entry with `ssh-keygen -R '<host>'` and reconnect.
- **Jump hosts** are verified the same way, since a jump host sees all traffic to the target.
- **No TTY, no prompt** — if the CLI has no terminal to ask on (a scripted run), an unknown host is refused rather than trusted silently.

`~/.ssh/known_hosts` is only ever appended to, never rewritten, so comments and `@cert-authority` / `@revoked` markers are preserved.

## Known limitations

- **No SSH certificate support** — paramiko's certificate authentication is not yet wired up.
- **Single Keychain account per connection ID** — if you delete and recreate a connection it gets a new ID, so the old Keychain entry becomes orphaned. These can be cleaned up manually in Keychain Access.
