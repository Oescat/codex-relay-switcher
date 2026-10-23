# Codex Relay Switcher

Windows GUI for switching Codex relay providers and global model defaults.

## Features

- Manage language and image relay profiles in two independent tabs.
- Connect a separate text-to-image API through a local Codex MCP tool.
- Save generated images to `D:\codex-\image` and return image content with its local path.
- Fetch available models from a relay's `/models` endpoint.
- Set the global Codex provider, model, model catalog, and default reasoning effort.
- Choose an optional per-relay Auto Review model override from `/models` or enter a model ID manually.
- Auto Review model selection is separate from Auto Review activation and the `/review` model setting.
- Supported reasoning effort choices: `light`, `medium`, `high`, `xhigh`, `max`, `ultra`.
- Default sub-agent reasoning effort is `max`.
- Set `conversationDetailMode = "STEPS_COMMANDS"` for the current desktop client behavior.
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
Leave the Auto Review model blank to let Codex choose its default reviewer. A selected override is written to the relay model catalog for each model from that relay; the model ID must be supported by the relay. This does not enable Auto Review or configure the separate `/review` model.

## Text-to-image setup

1. Open **生图中转** and add an image relay with its own API key. For Bajie, use `https://bajie-api.com/v1`.
2. Fetch the model list or enter a model ID such as `gpt-image-2`. A relay's model list may also include language models; select one that supports `/images/generations`.
3. Select **当前生图模型** and click **应用**, then completely exit and restart Codex after first registration. After first registration, change **当前生图模型** and click **应用** to use that model for the next request without restarting Codex. A request already submitted is unaffected. Keep the program folder in place: the MCP registration uses the absolute path to `relay_image_mcp.py` and the current Python executable.
4. Explicitly ask Codex to generate an image. The tool description asks Codex to clarify ambiguous intent, display the returned image, and keep its local path. Actual tool discovery and image display must also be supported by the Codex client and the conversation model.

PNG output works with the standard library. JPEG and WebP results require the optional Pillow package for full decoding; if unavailable, those results are rejected without resending the generation request. No dependency is installed automatically.

All future images are saved to `D:\codex-\image`. The folder is created and checked for write access before sending a generation request. Each call generates one image; failures and timeouts are not automatically retried. The current integration supports text-to-image through OpenAI-compatible `/v1/images/generations`, using `prompt`, `size`, and `quality`. Image editing, video, and native Anthropic Messages adaptation are not included.

Use **启用生图工具** in the Switcher to block or permit new requests while Codex is running. Closing the Switcher does not stop the MCP service. Turning the switch off cannot cancel a request already submitted. Changing the active relay's URL, key, or model turns generation off until you apply or enable it again. The tool remains registered while switched off and returns a disabled message without sending a generation request. Codex can still require approval under its own host policy; this program does not change global approval settings.

Image profiles and their independent key are encrypted with Windows DPAPI in `<CODEX_HOME>\relay-switcher\image-profiles.dpapi`. Only the local MCP command is added under `[mcp_servers.relay_switcher_image]`; the image key is not written into `config.toml`, command-line arguments, or environment variables. An encrypted initial MCP configuration backup is kept as `image-mcp-backup.dpapi`. An existing conflicting registration is reported without overwriting it.

## Security

Do not commit `config.toml`, `auth.json`, API keys, DPAPI files, backups, or verification output. The repository ignore rules exclude these paths. The relay key is needed locally to query models and is written to the active Codex provider configuration by the current Codex integration; use a private repository and review the staged file list before publishing.

## Tests

```powershell
python -m unittest -v
```

