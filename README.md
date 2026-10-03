# Snug & Co. server and admin

A Flask API that stores the catalog in a database and lets the team manage it from the admin at `/admin` on the website. The storefront reads from this API when `VITE_API_URL` is set.

What the admin can do (PRD §37):

- Sign in with an email and password (no public sign-up)
- Add, edit, hide and delete products, and change their order
- Change prices, mark a piece sold out or coming soon, feature it, or flag it as a new arrival
- Set colours (with swatches), sizes and extra options such as pants or shorts
- Upload photos, reorder them, write their descriptions and remove them
- Add, edit, reorder and delete categories and collections, and choose their cover photos
- Change their own password

Not included: staff accounts with different permissions, editing the site settings (WhatsApp number, hours, policies), testimonials, and orders. Those still live in `client/src/config/site.js` and `client/src/data/social.js`.

## Set up

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # add -postgres.txt for PostgreSQL

cp .env.example .env                                # then set SECRET_KEY (see the file)
export FLASK_APP=wsgi.py

.venv/bin/flask db upgrade                          # create the tables
.venv/bin/flask seed                                # load the 23 products from the storefront
.venv/bin/flask create-admin                        # asks for an email and password (12+ characters)
.venv/bin/flask run --port 5000
```

`.env.example` is a development setup (`FLASK_DEBUG=1`): a local SQLite database in `instance/snug.db`, photos on disk or in the `snug-co-dev` Cloudinary folder. Outside Render, the server refuses a `DATABASE_URL` on another machine, because that is most likely the live database. To run one command against it on purpose, prefix it with `ALLOW_REMOTE_DATABASE=1`.

In another terminal, run the site against it:

```bash
cd client
echo "VITE_API_URL=/api" >> .env
npm run dev                                         # http://localhost:5173, admin at /admin
```

The dev server proxies `/api` and `/uploads` to port 5000, so the site and API behave as one domain.

Forgot a password: `flask reset-password`.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Deploying

The admin's sign-in cookie only works when the site and the API share one domain, so serve both from the same host: the built site from `client/dist`, and `/api` and `/uploads` proxied to Flask.

```nginx
location /api/     { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; proxy_set_header X-Forwarded-For $remote_addr; proxy_set_header X-Forwarded-Proto $scheme; }
location /uploads/ { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
location /         { root /var/www/snug/dist; try_files $uri /index.html; }
```

```bash
.venv/bin/gunicorn -w 2 -b 127.0.0.1:8000 wsgi:app
```

Checklist:

- `SECRET_KEY` is set to a long random value and never committed. Changing it signs everyone out.
- `FLASK_DEBUG` is `0` (or unset). Cookies are then marked `Secure`, so the site must use HTTPS.
- The proxy passes the original `Host` header. The admin API rejects requests whose `Origin` doesn't match it (or an address in `ALLOWED_ORIGINS`). Set `TRUSTED_PROXIES=1` if the proxy sets `X-Forwarded-*` headers.
- `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY` and `CLOUDINARY_API_SECRET` are set (see below). Photos are never written to the server's disk in production: without Cloudinary, photo uploads are refused with a 503.
- For PostgreSQL, install `requirements-postgres.txt` and set `DATABASE_URL`.
- Build the client with `VITE_API_URL` pointing at this API, then run `flask db upgrade` after every schema change.
- Add rate limiting to `/api/admin/login` at the proxy as well. The app's own limit (5 wrong tries per 15 minutes per address and email) resets on restart and isn't shared between workers.

## Deploying the client on its own domain

If the client is a separate app (its own repo, its own host — Vercel, Netlify, …) rather than built into this server's `CLIENT_DIST`, cookies and requests cross a domain boundary and need a bit more:

- Recommended: let the client's host proxy `/api` and `/uploads` to this server (`client/vercel.json` does this) and build the client with `VITE_API_URL=/api`. The sign-in cookie is then first-party, so browsers that block third-party cookies (Safari, Brave) still work.
- Set `ALLOWED_ORIGINS` to the client's exact origin(s), e.g. `https://snug-co-client.vercel.app`. The admin rejects change requests from any other origin, and CORS headers are sent only to these.
- Only if the browser calls this API's own domain directly (no proxy): set `SESSION_COOKIE_SAMESITE=None` and `VITE_API_URL` to the full API address. Both sites must use HTTPS.
- Photos are served by Cloudinary's CDN straight from its own URLs, so it does not matter which host serves the site.

## Storing photos: Cloudinary

New photos go to **Cloudinary** when `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY` and `CLOUDINARY_API_SECRET` are set (server-side only: never in the client, Vercel or git). The admin uploads to this server as before; the server checks the session and the file, uploads the untouched original to `snug-co/products/<photo id>` as a *private* asset, and saves its `public_id`, version and 4:5 crop in `product_images`. The original (which may carry camera GPS data) is only reachable with a signed URL; the public, metadata-free copies are generated on demand:

```
https://res.cloudinary.com/<cloud>/image/private/c_crop,h_1500,w_1200,x_0,y_120/f_auto,q_auto,c_limit,w_{w}/v<version>/snug-co/products/<id>
```

The API sends that URL with `{w}` left in; the client fills in the widths for its `srcset`. Every new upload gets a new public ID and the version is in the URL, so CDN copies are cached for good and never go stale. Deleting a photo in the admin deletes the Cloudinary asset (with a CDN purge) unless another row still uses it.

Cloudinary is the only photo store in production. Without the Cloudinary variables, uploads fail with a clear 503 rather than falling back to the server's disk. Local-disk storage exists only for development: it turns on with `FLASK_DEBUG=1` so you can work without a Cloudinary account (files go to `UPLOAD_DIR`, served from `/uploads`; in production `/uploads` returns 404). A fresh deployment needs no `SUPABASE_*` variables at all.

### Legacy: moving photos stored before Cloudinary

Only needed when carrying over an older database. A fresh deployment skips this. These commands live in `app/legacy.py` and read `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` and `SUPABASE_BUCKET` directly from the environment; the running API never uses them.

```bash
export FLASK_APP=wsgi.py
flask migrate-images-to-cloudinary --dry-run     # what would be copied
flask migrate-images-to-cloudinary --limit 3     # a trial batch
flask migrate-images-to-cloudinary               # the rest
```

It copies the largest stored size of each photo (Supabase Storage, local disk, or the client's bundled files via `--client-dir`) into Cloudinary, checks that a resized copy is served, then saves the asset details on that row only. It never deletes anything and never overwrites an asset, so it can be stopped and re-run: finished photos are skipped and a half-finished one reuses its existing asset. The old `is_upload`/`widths` columns are kept.

**Rollback:** set `IMAGE_DELIVERY=legacy` and the API serves the old Supabase/bundled copies again for every photo that has one (photos uploaded after the switch only exist in Cloudinary). While Cloudinary is on, photos stored before it are never deleted.

## Wishlist and cart

Shoppers don't sign in. Their wishlist and cart live in a signed `snug_shopper` cookie (HttpOnly, Secure in production, SameSite=Lax, 90 days, sent only to `/api/shopper`). It holds product ids and choices, nothing personal. Every read checks the ids against the catalog, so hidden or deleted products drop out. Limits: 60 saved pieces, 20 cart lines, 10 of each.

| Method | Path | |
| --- | --- | --- |
| GET | `/api/shopper` | `{wishlist: [id], cart: [{key, productId, color, size, options, quantity}], products: {id: {slug, name, priceKES, availability, madeToOrder}}}` (every response below has the same shape) |
| GET | `/api/shopper/check?ids=p1,p2` | `{products: {...}}`: current price and availability of up to 20 pieces; hidden or deleted ones are left out |
| PUT / DELETE | `/api/shopper/wishlist/<id>` | Save or remove a piece |
| POST | `/api/shopper/cart` | Add `{productId, color, size, options, quantity}`; same choices add up |
| PATCH / DELETE | `/api/shopper/cart/<key>` | Change quantity or remove a line |
| DELETE | `/api/shopper/cart` | Empty the cart |

`/api/shopper` responses are never cached (`private, no-store`), unlike the storefront routes. The site checks an order against them right before WhatsApp opens, so a price or availability from a cached page is never what gets sent.

Writes need `X-Requested-With: snug-shop` (and an allowed `Origin`), like the admin. Responses are `private, no-store`. Checkout is still WhatsApp: the client's bag page sends the whole cart as one message.

## Security notes

- Passwords are hashed with scrypt. The same message and timing is used for a wrong email and a wrong password.
- The session is a signed cookie (`HttpOnly`, `SameSite=Lax`, 8 hours idle, 7 days at most). Changing the password (in the admin or with `flask reset-password`) signs out every other browser. Rotate `SECRET_KEY` to sign everyone out.
- Sign-in attempts are limited per browser: one that has signed in before carries a signed device cookie and has its own limit, so someone hammering the login from a shared proxy address can't lock the owner out. Browsers without that cookie share limits per address (20 failures in 15 minutes) and per account (50), so guesses spread over many addresses still run out. The limits are kept in memory by each gunicorn worker and reset on restart.
- The admin session cookie is only ever sent on `/api/admin` responses, never on storefront, sitemap or page responses a CDN might cache.
- Every response forbids framing (`X-Frame-Options: DENY`, `frame-ancestors 'none'`).
- Outside development the server refuses to start with a `SECRET_KEY` under 32 characters, and it refuses `FLASK_DEBUG=1` on Render.
- Requests are capped at 64 KB, except photo uploads (40 MB). Photos over 40 megapixels (PNG/WebP) are rejected before decoding; large JPEGs are decoded at reduced size.
- Every state-changing admin request must carry `X-Requested-With: snug-admin` and, when the browser sends one, a matching `Origin`. Other websites can't send that header.
- Uploads are fully decoded with Pillow before anything is stored, so the file's real contents are checked, whatever its name or type says. Limits: JPEG, PNG or WebP, 600px wide or more, 10 MB each (Cloudinary's free-plan limit), 12 per product. Only signed-in admins can upload or delete; Cloudinary credentials never leave this server.
- The public API only returns products marked visible.

## Data protection

- **Deleting is permanent.** Deleting a product removes its row and its Cloudinary photos (with a CDN purge). Hiding a product is the reversible option. `flask seed` only runs on an empty database and asks before loading sample products outside development.
- **Development can't touch live photos.** Development uploads go to `snug-co-dev/`, and the server only ever deletes Cloudinary assets in its own mode's folder.
- **Admins:** `flask list-admins`, `flask create-admin`, `flask reset-password` and `flask delete-admin` (it won't delete the last admin; the deleted admin's browsers are signed out at once).
- **Backups are not built in.** Supabase's free plan keeps no downloadable backups. Use a paid plan with daily backups, or take a dump yourself, for example before each deploy: `pg_dump "$DATABASE_URL" --format=custom --file=snug-$(date +%F).dump` (restore with `pg_restore --clean --dbname "$DATABASE_URL" snug-<date>.dump`). Turn on Cloudinary's backup if your plan includes it, since deleted photos can't be recovered otherwise.
- **No audit log.** There is no record of who changed what.

## Supabase: row level security

The browser never talks to Supabase. Only Flask connects, with `DATABASE_URL`, as the owner of the tables. A table is reachable through Supabase's Data API only if the `public` schema (or that table) is exposed there and the `anon` or `authenticated` roles have grants on it; whether that is the case depends on the project's settings, and Supabase's defaults have changed over time. Migration `e5f6a7b8c9d0` turns on row level security for every table, with no public policies, as defense in depth against accidental Data API exposure: if a table is ever exposed, the Data API sees no rows and can change nothing, while Flask (the owner) is unaffected. Any new table needs a migration that does the same; `tests/test_environment.py` fails until it does. Switching off the Data API in the Supabase dashboard (or removing `public` from its exposed schemas) closes the same door.

The old Supabase Storage bucket (`product-photos`) is only read by `flask migrate-images-to-cloudinary`. Once every photo is in Cloudinary, delete the bucket, or check that its policies allow no public uploads or deletes.

## Layout

| Path | What it holds |
| --- | --- |
| `app/models.py` | Tables: products, images, categories, collections, admin users |
| `app/routes/public.py` | Storefront data, one request per page: `/api/home`, `/api/products` (card-sized list), `/api/products/<slug>`, `/api/settings`. `/api/catalog` (everything) stays for older builds. All are CDN-cacheable for 60s, served stale while refreshing. |
| `app/routes/auth.py` | Sign in, sign out and change password |
| `app/routes/admin.py` | Product, photo, category and collection endpoints |
| `app/validation.py` | Input checks, with a message for each field |
| `app/images.py` | Photo cropping and Cloudinary storage (local disk for development) |
| `app/cli.py` | `seed`, `create-admin`, `reset-password`, `list-admins`, `delete-admin` |
| `app/legacy.py` | Old Supabase Storage photos: `import-bundled-photos`, `migrate-images-to-cloudinary` |
| `migrations/` | Database migrations |
| `seed/catalog.json` | Starter catalog loaded by `flask seed` |

## Production: Vercel + Render + Supabase + Cloudinary

```
Browser -> Vercel (React site) -> /api rewrite -> https://snug-co-api.onrender.com/api -> Flask
```

- **Render** runs this server only (`gunicorn wsgi:app`). Run `flask db upgrade` before each deploy (a Render pre-deploy command works).
- **Supabase** is the PostgreSQL database only, through `DATABASE_URL`. A new database starts empty: no products, categories, shoppers or settings. `flask seed` is optional and is not part of a clean start. Connections to Supabase require TLS, and pooled connections are checked before use.
- **Cloudinary** stores every product photo under `snug-co/products`.
- **Vercel** serves the site and proxies `/api/*` to Render (`client/vercel.json`), so the browser only talks to the site's own domain and the admin cookie stays first-party.

Environment variables on Render:

| Variable | Value |
| --- | --- |
| `SECRET_KEY` | long random value, at least 32 characters (the server won't start otherwise) |
| `DATABASE_URL` | the Supabase PostgreSQL connection string |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | from the Cloudinary console |
| `IMAGE_DELIVERY` | `cloudinary` |
| `ALLOWED_ORIGINS` | the site's exact origin(s), e.g. `https://www.snugandco.co.ke`. Needed because Vercel forwards the browser's `Origin` but Render sees its own host. |
| `TRUSTED_PROXIES` | `1` |
| `SITE_URL` | the public site address, for the sitemap |

Leave `FLASK_DEBUG` unset: the server refuses to start with it on Render. Render sets `RENDER` itself, which is what allows the remote `DATABASE_URL` there; on any other host with a remote database, set `ALLOW_REMOTE_DATABASE=1`. Do not set `UPLOAD_DIR`, `UPLOAD_URL_BASE` or any `SUPABASE_*` variable.

After the first deploy, create the first admin from the Render shell (it prompts for the password, which is never stored in git or the environment):

```bash
cd server && FLASK_APP=wsgi.py flask create-admin --email you@example.com
```

Then sign in at `/admin`, add categories (a product needs one), then products and photos.
