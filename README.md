# Book Sale

An offline phone app (installs to the iPhone home screen) that searches your whole [Shelf](https://github.com/) library, so at a book sale you can check whether you already own a title, and in which binding.

```
Shelf database --(read-only)--> export_store_snapshot.py --> data/ (2 small JSON files)
                                                               |
                                  nginx container (read-only) -+-> Tailscale HTTPS -> iPhone
The container never sees Shelf's database. It only serves the app and the two JSON files.
```

## What's in this repo
| Path | What it is |
|---|---|
| `export_store_snapshot.py` | Turns a Shelf database into `snapshot.<hash>.json` + `version.json`. Stdlib only. Only an allowlist of fields leaves Shelf (title, subtitle, authors, media type, series, barcode, binding, tags, locations): never notes, prices, borrowers, reading history or accounts. |
| `app/` | The web app: `index.html`, `app.js` (search + sync), `styles.css`, `sw.js` (offline support), manifest and icons. |
| `Dockerfile`, `nginx/`, `.github/workflows/build.yml` | The container: unprivileged nginx, read-only filesystem, no capabilities. GitHub builds the image and publishes it to `ghcr.io`. |
| `docker-compose.yml` | The TrueNAS Custom App definition (pulls the ready-made image). `docker-compose.local.yml` is a hand-managed fallback. |
| `dev_server.py` | Preview on your PC. |
| `test_export_store_snapshot.py`, `test_pwa.py` | Tests. `test_pwa.py` drives headless Chromium (needs `pip install playwright`). |

## Try it on your PC first
1. `python export_store_snapshot.py` (edit the config block at the top; it defaults to the newest `shelf_*.db` in your backups folder and writes to `shelf-tools\book-sale\data`).
2. `python dev_server.py` (add `--data <folder>` to use another folder). It must sit next to the `app` folder.
3. Open http://localhost:8480. localhost counts as a secure context, so offline storage and the service worker work for real.

## Deploy
### 1. Publish the image from GitHub
1. Create a GitHub repository named `book-sale` and push this folder to `main`. `.gitignore` keeps databases, `data/`, snapshots and `.env` files out.
2. Open the **Actions** tab. The push starts "Build and publish image". When it is green, the image exists at `ghcr.io/<your-username>/book-sale:latest`.
3. Make it pullable: your profile, **Packages**, `book-sale`, Package settings. Either set visibility to Public (it holds only app code, no library data), or keep it private and give TrueNAS a token with `read:packages`.

### 2. TrueNAS dataset
Create one dataset, `book-sale`, with a `data` folder inside it:
```
/mnt/POOL/book-sale/data
```
Make sure "others" can read it (the container runs as an unprivileged user): `chmod 755 /mnt/POOL/book-sale/data`.

### 3. Install the app
Apps, Discover Apps, Custom App, **Install via YAML**. Paste `docker-compose.yml` and change the two lines marked `EDIT`: the image (`ghcr.io/<your-username>/book-sale:latest`) and the data path (`/mnt/POOL/book-sale/data`).

### 4. Put the library in `data/`
Run the exporter on a copy of Shelf's database (never edit or export from the live file over SMB), then copy **the two JSON files it writes** (not the database) into the dataset: snapshot first, `version.json` last, so the phone never sees a pointer to a missing file.
```
SRC=/mnt/POOL/<share>/Books/Shelf/shelf-tools/book-sale/data     # where the exporter wrote
DST=/mnt/POOL/book-sale/data
cp "$SRC"/snapshot.*.json "$DST"/
cp "$SRC"/version.json "$DST"/
chmod 644 "$DST"/*.json
curl http://127.0.0.1:8480/data/version.json                      # should print the version JSON
```
The exporter refuses to publish an empty library or one under 80% of the previous count (`--force` overrides), and keeps the last 3 snapshots.

### 5. HTTPS via Tailscale
iPhones only allow offline web apps over HTTPS. In the Tailscale app's shell on TrueNAS:
```
tailscale serve --bg --https=8480 http://127.0.0.1:8480
```
Use a host port different from Shelf's, since `tailscale serve` and a container binding the same port can clash depending on start order.

### 6. On the iPhone
Open the HTTPS address in Safari once, Share, **Add to Home Screen**, and open it from the new icon. Wait for the pill to say "Offline-ready". Then try airplane mode, close the app fully, reopen: search should still work. Do this the day before the sale.

### Updating
- **App changes:** edit, commit, push. When the build is green, restart the app on TrueNAS (`pull_policy: always` fetches the new image).
- **Library changes:** run the exporter again and copy the new files as in step 4.

## How sync works
**Automatic, while the app is open.** It checks for a newer library on every open, when the connection comes back, when you return after more than 10 minutes in the background, or when you tap the status pill and Update now. iPhones don't allow background sync for web apps, so the habit is "open it at home on wifi before leaving".

**Each check** fetches `version.json` (about 200 bytes). If its hash matches the saved library, nothing else happens. If not, the phone downloads the new snapshot and validates it (size, SHA-256, item count, and not suspiciously smaller than what it has) before replacing the saved copy in one atomic step.

**It never freezes.** Search runs only on the saved copy and never waits on the network. The version check gives up after 5 s and a snapshot download after 60 s; a half-finished download is discarded and the saved library stays. A failed sync never leaves you with less than you had.

**The pill** reads `Offline-ready · N titles · synced <date>`. It turns amber after 30 days without a sync, and warns if the server's copy is over 14 days old.

**First open** needs a connection once. **Storage** is IndexedDB; adding the app to the Home Screen and requesting persistent storage protects it from Safari's clean-up. If it is ever cleared, the next connected open fetches it again.

## Not built yet
Special-edition and collection tags in display order (the exporter already sorts `special edition` first and has a `COLLECTION_TAGS` list ready); location on the row; a "you own N copies" hint.

## Status
The app, exporter and sync logic are tested. The Dockerfile, nginx config, compose file and GitHub workflow have not been run in a container runtime, so the first build and first TrueNAS start are their first real test.
