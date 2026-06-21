# Per-client logos

Drop **one self-contained logo per client** here, named by the client key: `clients/<c>.svg`
(e.g. `riverdance.svg`). It is the logo shown beside the AGORA mark inside that client's Agora Atrium
workspace.

`dash/seed_workspace.py` reads this file and inlines it into the client's `workspace/<c>.json`
(`brand.client_logo`). To refresh an existing client's logo after adding/replacing the file:

```powershell
.\.venv\Scripts\python.exe agora-platform\dash\seed_workspace.py --rebrand <c>
```

No file here for a client? An initials monogram is generated automatically, so the workspace always
renders something tasteful.

**Format:** prefer a **square-ish mark/monogram** (it renders ~34px in the sidebar + client card; a
wide wordmark gets tiny). SVG preferred; keep it self-contained (no external font/image refs — outline
text to paths or use a system font stack).

**Got a PNG/JPG instead?** The seed reads ONLY `<key>.svg`, so wrap the raster into a self-contained
SVG first with the helper (downscales + embeds it as a `data:` URI):

```powershell
.\.venv\Scripts\python.exe agora-platform\dash\logo_to_svg.py <key> path\to\logo.png
```

That writes `clients/<key>.svg`. Then `--rebrand <key>` as above. See `../README.md` for the full
guidelines.
