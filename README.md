# Sale Lookup: the store app

A phone web app (installs to the iPhone home screen) that searches your whole library offline, to check
at a book sale whether you already own something and in which binding.

    Shelf database --(cron, read-only)--> export_store_snapshot.py --> data/ (2 small files)
                                                                         |
                                            nginx container (read-only) -+-> Tailscale HTTPS -> iPhone
    The container never sees Shelf's database. It only serves the two JSON files and the app.

## What is in this folder
| Path | What it is |
|---|---|
| `export_store_snapshot.py` | Turns a Shelf database into `snapshot.<hash>.json` + `version.json`. Stdlib only, so it runs on the TrueNAS host from cron. Only an allowlist of fields leaves Shelf (title, subtitle, authors, media type, series, barcode, binding, tags, locations): never notes, prices, borrowers, reading history or accounts. |
| `app/` | The web app: `index.html`, `app.js` (search + sync), `styles.css`, `sw.js` (offline support), manifest and icons. |
| `nginx/`, `docker-compose.yml` | The container: unprivileged nginx, read-only filesystem, no capabilities, serving `app/` and `data/` read-only. |
| `dev_server.py` | Preview on your PC: `python dev_server.py --data <folder the exporter wrote>` then open http://localhost:8480 |
| `test_export_store_snapshot.py`, `test_pwa.py` | Tests. `test_pwa.py` drives a real headless Chromium (offline, slow, damaged downloads). Needs `pip install playwright` and the Chromium it installs. |

## Try it on your PC first (no container)
1. `python export_store_snapshot.py` (edit the config block at the top: it defaults to the newest `shelf_*.db` in your backups folder and writes to `shelf-tools\store-app\data`).
2. `python dev_server.py` (it serves the folder the exporter just wrote; add `--data <folder>` to use another). `dev_server.py` must sit next to the `app` folder.
3. Open http://localhost:8480. localhost counts as a secure context, so offline storage and the service worker work for real.

## Setting up the container on TrueNAS
1. **Dataset.** Create a dataset for it, e.g. `POOL/store-app`, with folders `app/`, `data/`, `nginx/`.
2. **Copy files.** Put the contents of this folder's `app/` into `app/`, and `nginx/default.conf` + `nginx/security-headers.inc` into `nginx/`. Make sure "others" can read them (the container runs as an unprivileged user).
3. **Cron job** (TrueNAS: System, Advanced, Cron Jobs; run as root; nightly is plenty):
   `python3 /mnt/POOL/store-app/export_store_snapshot.py --db /mnt/POOL/<your shelf data>/shelf.db --out /mnt/POOL/store-app/data`
   It reads Shelf through SQLite's backup API (a consistent copy; safe while Shelf is running) and never writes to it. If the export looks wrong (empty, or under 80% of the previous count) it refuses to publish and the phone keeps the good library.
4. **Container.** Apps, Discover Apps, Custom App, Install via YAML: paste `docker-compose.yml` after replacing `/mnt/POOL/store-app` with your dataset path.
5. **HTTPS.** iPhones only allow offline web apps over HTTPS. Put Tailscale in front the way you did for Shelf, on a different port, e.g. `tailscale serve --bg --https=8480 http://127.0.0.1:8480` (adjust to however your Tailscale is set up).
6. **On the iPhone:** open the HTTPS address in Safari once, Share, Add to Home Screen, open it from the new icon. Wait for the pill to say "Offline-ready". Then turn on airplane mode, close the app completely and reopen it: you should still be able to search. Do this the day before the sale.

Not yet verified here: the nginx config and compose file were written carefully but not run (no container runtime in my environment), so step 4 is the first real test of them. The app, the exporter and the sync logic are tested.

## How sync works
**Is it automatic?** Yes, but only while the app is open. It checks for a newer library:
- every time you open the app (and have a connection),
- when the connection comes back (airplane mode off, wifi joins),
- when you return to the app after it has been in the background for more than 10 minutes,
- or when you tap the status pill and Update now.

iPhones do not let a web app sync in the background while closed, so there is no overnight sync on the phone: the habit is "open it at home on wifi before leaving".

**What happens on each check.** The phone fetches `version.json` (about 200 bytes). It holds a hash of the library it has saved. If the hash is the same, nothing else happens (no download). If it differs, the phone downloads the new snapshot (about 130 KB over the wire for roughly 3,900 titles), then checks it: size, SHA-256, item count matches, and not suspiciously smaller than what it already has. Only if all of that passes does it replace the saved copy, in one step. Then the pill updates and a toast says how many titles are new.

**Does it freeze on a poor connection?** No, by design.
- Search runs only on the copy saved on the phone and never waits for the network. The sync happens in the background; you can type and get results while "Checking for updates" is showing (tested).
- The version check gives up after 5 seconds; a snapshot download gives up after 60 seconds. A half-finished download is thrown away, never applied, and your saved copy stays. The pill's panel says what happened ("did not answer in time", "download was cut short", etc.).
- It tries again at the next trigger above; you can also tap Update now.
- A truncated, corrupted or wrong file is rejected by the checks, and a failed sync never leaves you with less than you had.

**What the pill shows.** `Offline-ready · 3,922 titles · synced Oct 20`. The date is the last time the phone successfully confirmed it had the latest library. If the phone has not synced for 30 days the dot turns amber and says how old it is. If the copy on the server is over 14 days old when you sync (the cron job has stopped), the panel warns you that newly added books will not appear.

**First ever open** needs a connection once, to download the library. With none, the app says so plainly and keeps search switched off rather than showing empty results.

**Storage.** The library lives in the phone's IndexedDB. Safari can clear website storage it considers unused, which is why you add the app to the Home Screen (installed apps are not subject to the 7-day clean-up) and why it asks the phone to keep the data. The panel shows whether the copy is saved. If the data is ever cleared, the next open with a connection fetches it again.

**Updating the app itself** (new features) is picked up in the background and takes effect the next time you open the app.

## Not built yet
Special-edition and collection tags in the display order (the exporter already sorts `special edition` first and has a `COLLECTION_TAGS` list ready); showing location on the row when there is room; a "you own N copies" hint.
