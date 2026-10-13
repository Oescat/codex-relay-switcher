# Codex Relay Switcher

Windows GUI for switching Codex relay providers and global model defaults.

## Features

- Manage multiple relay profiles locally.
- Fetch available models from a relay's `/models` endpoint.
- Set the global Codex provider, model, model catalog, and default reasoning effort.
- Supported reasoning effort choices: `light`, `medium`, `high`, `xhigh`, `max`, `ultra`.
- Default sub-agent reasoning effort is `max`.
- Keep a rollback copy of the previous Codex configuration.
- Protect saved relay profiles and rollback data with Windows DPAPI.
- Keep runtime data under the Codex user directory instead of the program directory.

## Requirements

- Windows
- Python 3.11 or newer
- A Codex installation with a user configuration directory

The included launcher expects Python 3.13 at `%LocalAppData%\\Programs\\Python\\Python313\\pythonw.exe`. Run `relay_switcher.pyw` directly with another supported Python installation if that path is different.

## Usage

Run `启动中转站.bat`, or run:

```powershell
python relay_switcher.pyw
```

The default Codex home is `%USERPROFILE%\\.codex`. Set `CODEX_HOME` or use the GUI to select another configuration directory. Saved profiles, backups, and generated catalogs are stored below:

```text
%USERPROFILE%\\.codex\\relay-switcher\\
```

After switching or restoring the global configuration, completely close and reopen Codex so the new configuration is loaded.

## Security

Do not commit `config.toml`, `auth.json`, API keys, DPAPI files, backups, or verification output. The repository ignore rules exclude these paths. The relay key is needed locally to query models and is written to the active Codex provider configuration by the current Codex integration; use a private repository and review the staged file list before publishing.

## Tests

```powershell
python -m unittest -v
```

