# Chat Search

Search saved chats and open the source message behind each result.

The Mac app starts with an empty notebook. Browser development mode includes a labeled synthetic sample. It has overlapping launch-date statements and a later correction. Use it to try search and source inspection. The data is fictional.

**Install and run on macOS**

The native app opens as a local Mac window. No browser tab or manual server start is needed. In this source folder, build and install it with:

```sh
python3.12 -m venv .desktop-venv
.desktop-venv/bin/python -m pip install -r requirements-desktop.lock.txt
.desktop-venv/bin/python build_desktop.py
.desktop-venv/bin/python install_desktop.py
```

Then open `~/Applications/Chat Search.app` in Finder. Use File → Quit Chat Search or close the window to stop the local listener. To remove only the marked app copy, run `.desktop-venv/bin/python uninstall_desktop.py`. The SQLite notebook remains in `~/.local/share/evidence-notebook` until its sources are deleted in Settings.

## Browser development

Requires Python 3.10 or later. No packages need to be installed.

```sh
python3 server.py
```

Open <http://127.0.0.1:8345>. The server listens only on loopback.

## Supported imports

- JSON arrays of message objects, or an object with a `messages` array.
- JSONL or NDJSON with one message object per non-empty line.
- CSV with a `content`, `text`, or `message` column. Header matching ignores case. Optional fields include role, speaker, timestamp, and conversation ID.
- Plain TXT, one message per non-empty line.

ZIP, PDF, audio, and other formats are not supported. An unsupported or malformed file fails during preview. The app does not save a partial import.

The app rejects JSON objects with duplicate keys. These keys can make a source offset unclear.

Each supported record can use `content`, `text`, or `message` for its text. Optional fields include `role`, `speaker`, `author`, `name`, `timestamp`, `created_at`, `time`, `conversation_id`, and `thread_id`.

## Privacy and storage

The browser reads imports and sends them to this local app. The preview applies your selected redactions before the app saves records. The app does not keep an unredacted copy of the uploaded file. Offsets mark character positions in the original text. They stay stable after redaction.

SQLite data lives at `~/.local/share/evidence-notebook/notebook.sqlite3`. Set `EVIDENCE_NOTEBOOK_DATA` before launch to choose another local folder. Reimporting the same contents and format does not create new records. Delete a selected source to remove its records from search. Or use Settings to delete all notebook data.

## Checks

```sh
python3 -m unittest discover -s tests -v
```

Tests cover all four adapters, stable offsets, redaction, duplicate imports, and search. They also cover source deletion, unsupported and malformed input, partial-import prevention, escaping, and loopback request boundaries.

## License

MIT. See [LICENSE](LICENSE).

For browser development, use the command and local address above. The native app targets macOS; other operating systems have not been tested.

The app is now named Chat Search. Existing local data remains in `~/.local/share/evidence-notebook`.
