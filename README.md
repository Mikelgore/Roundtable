# Roundtable

One conversation with Claude, ChatGPT, Gemini, and Meta at the same time. Each model sees what the others said, so the five of you can reason a question out together.

Roundtable is a single Python file. It runs a small web server on your own computer and opens a page in your browser. There is nothing to install beyond Python.

## Requirements

- Python 3.8 or newer
- An API key from each provider you want at the table:

| Participant | Where to get a key | Key starts with |
|---|---|---|
| Claude | console.anthropic.com | `sk-ant-` |
| ChatGPT | platform.openai.com | `sk-proj-` or `sk-` |
| Gemini | aistudio.google.com | usually `AIza` |
| Meta | llama.developer.meta.com | issued in the Llama API dashboard |

API access is billed per use and is separate from a Claude, ChatGPT, or Gemini chat subscription. Each API account needs its own billing set up.

Meta is the odd one out: there is no public API for the Meta AI chat app. Roundtable uses Meta's hosted **Llama API** (`api.llama.com`, OpenAI-compatible) instead. At time of writing that API is in preview behind a waitlist and reported as US-only, so request access first and paste in the key once approved.

## Quick start

1. Put `roundtable.py` in a folder of its own.
2. Run it:

   ```
   python roundtable.py
   ```

3. Your browser opens to `http://127.0.0.1:8765`. On first launch the Settings window appears.
4. Paste in your API keys, set your name, and click **Save**.
5. Type a question and press Enter.

To stop the program, press Ctrl+C in the terminal window.

## How a conversation works

| Control | What it does |
|---|---|
| **Send** | Every ticked model answers your message independently. None of them sees the others' answers for that round. |
| **Cross-examine** | Each ticked model reads the others' latest answers, points out errors and gaps, and says whether it is keeping or changing its position. |
| **Who answers** | Untick a model to leave it out of a round. It still sees the exchange afterward. |
| **Synthesize** | The model chosen in the dropdown summarizes what is agreed, what is still disputed, its best answer, and what would settle the open questions. |
| **Retry** | Appears on a card that failed. Re-asks that one model with the same context. |
| **copy** | Copies one reply to the clipboard. |
| **Export** | Downloads the whole conversation as a Markdown file. |

Enter sends a message. Shift+Enter adds a new line.

Conversations are saved automatically and listed in the sidebar. Hover over one and click × to delete it.

### Getting good results

- Read the three independent answers before cross-examining. Where they differ is where to dig.
- Agreement among the models is weak evidence. The models share much of their training, so they can be wrong in the same way.
- You are the verifier. The app shows you where to look; it does not check facts. The models answer from memory and cannot browse the web, so confirm any citation before relying on it.
- When a dispute narrows to two models, untick the third and question those two directly.
- One cross-examination round is usually enough. Once the replies turn polite and vague, more rounds add little.

## Settings

- **Your name**: how the models see you in the transcript. The default is "Moderator".
- **API key**: one per provider. A saved key is never shown again. Leave the box blank to keep the current key, or click **Remove** to delete it.
- **Model**: the model each provider uses. **List models** fetches the models your key can use. Clear the Model box and click it to pick from the list.
- **Workspace ID** (Claude only): needed only if your Anthropic key is not tied to a workspace. Find the ID in the Anthropic Console under your workspace settings.

Default models:

| Participant | Default model |
|---|---|
| Claude | `claude-opus-5-5` |
| ChatGPT | `gpt-6.1-sol` |
| Gemini | `gemini-4-argon` |
| Meta | `Llama-4-Maverick-17B-128E-Instruct-FP8` |

Providers retire models over time. If a default stops working, pick a current one with **List models**.

## Command-line options

```
python roundtable.py --port 8766     # use a different port
python roundtable.py --no-browser    # don't open the browser automatically
```

## Environment variables

All are optional.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude key, used if none is saved in Settings |
| `OPENAI_API_KEY` | ChatGPT key, used if none is saved in Settings |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Gemini key, used if none is saved in Settings |
| `ANTHROPIC_WORKSPACE_ID` | Claude workspace ID, used if none is saved in Settings |
| `LLAMA_API_KEY` | Meta (Llama API) key, used if none is saved in Settings |
| `ROUNDTABLE_DATA` | Folder for settings and conversations, in place of the default |

A key saved in Settings takes priority over an environment variable.

## Where your data lives

Everything is stored in a `roundtable_data` folder next to `roundtable.py`:

```
roundtable_data/
  config.json           your name, model choices, API keys, workspace ID
  conversations/        one JSON file per saved conversation
```

To back up or move your conversations, copy this folder. To start fresh, delete it.

## Security

- The server listens on `127.0.0.1` only, so other machines on your network cannot reach it.
- Requests that do not come from the page on your own machine are refused.
- API keys are sent only to their own provider's API, and are never sent back to the browser.
- Keys are stored in plain text in `roundtable_data/config.json`. If you would rather not keep keys on disk, use the environment variables above and leave the key boxes empty.
- Your questions and the full transcript are sent to all three providers. Do not put anything into a conversation that you would not send to each of them.

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `This API key is not scoped to a workspace...` (Claude) | Enter your Workspace ID in Settings, or create a new key inside a specific workspace in the Anthropic Console. |
| `HTTP 401: Incorrect API key provided` | The wrong key is in that box. Check the prefix against the table under Requirements. |
| `HTTP 404` or "model not found" | The model name is wrong or retired. Use **List models** in Settings to pick a current one. |
| `HTTP 429` or a quota error | The API account has no credit or has hit a rate limit. Check billing on that provider's site. |
| `No API key saved for ...` | Add the key in Settings, or untick that model. |
| `Can't reach the Roundtable program` | `roundtable.py` is not running. Start it again and reload the page. |
| `Couldn't start on port 8765` | Another program is using the port. Run with `--port 8766`. |
| `CERTIFICATE_VERIFY_FAILED` | Python cannot verify the provider's certificate. On macOS, run "Install Certificates.command" from your Python folder. On a managed network, point `SSL_CERT_FILE` at your organization's CA bundle. |
| A card says "thinking" for a long time | Replies arrive whole, not word by word, and reasoning models can take a minute or more. A request gives up after 10 minutes. |

## How it works

- The browser page keeps the transcript and sends each model its own view of it through the local server.
- A model's own earlier replies are sent as its turns. Everything else is labeled, for example `[Moderator]:` or `[Gemini]:`, so each model knows who said what.
- When a round starts, every model receives the transcript only up to the message that opened the round. That is what keeps first answers independent.
- Each model is told to speak only for itself, to aim for the correct answer over agreement, and to say what specifically changed its mind if it changes position.

## Limitations

- Text only. No images or file attachments.
- No streaming. Each reply appears when it is complete.
- The models cannot search the web or run code.
- Every round resends the whole transcript to each model, so long conversations cost more per round as they grow.
