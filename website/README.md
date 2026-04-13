# brainctl — marketing site

Static landing page for the brainctl token launch + open-source repo.

## Stack

Zero dependencies. Vanilla HTML / CSS / JS. Ships as a static site.

```
website/
├── index.html     # landing page
├── styles.css     # theme
├── script.js      # CA copy button + neural canvas background
├── vercel.json    # Vercel config (security headers + long-cache for assets)
└── README.md
```

## Local preview

Any static file server works:

```bash
cd website
python3 -m http.server 8000
# open http://localhost:8000
```

## Deploy to Vercel

### Option A — Vercel CLI

```bash
npm i -g vercel
cd website
vercel              # first deploy (preview)
vercel --prod       # ship to production
```

When Vercel asks for the root directory, point it at `website/`.

### Option B — GitHub import

1. Push this repo to GitHub.
2. Go to <https://vercel.com/new> and import the repo.
3. Set **Root Directory** to `website`.
4. Framework preset: **Other** (no build step, no install command).
5. Deploy.

### Option C — Drag-and-drop

Zip the `website/` folder and drop it onto <https://vercel.com/new>.

## Updating the contract address

Once the Pump.fun coin is live, edit `index.html`:

```html
<span class="ca-value" id="ca-value">YOUR_CONTRACT_ADDRESS_HERE</span>
```

The copy button auto-detects whether the value is still `TBA` and shows `SOON` vs `COPIED` accordingly.

## Notes

- No analytics, no trackers, no external JS.
- Neural-network background is a single canvas — respects `prefers-reduced-motion`.
- All assets are same-origin; only Google Fonts is loaded externally.
