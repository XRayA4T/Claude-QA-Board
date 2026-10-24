# Branding

Each board has a **name** and an **accent colour**, set with `BOARD_NAME` and `BOARD_ACCENT` in the
env file (or `--name` and `--accent` on `qaboard.py serve`). These are used in the page title, the
header, the buttons and the generated favicon.

For more than that, point `BRAND_DIR` at a folder with a `brand.css`. Start by copying
[`brand/example`](../brand/example):

```bash
cp -r brand/example brand/webapp
$EDITOR brand/webapp/brand.css            # then BRAND_DIR="brand/webapp" in config/envs/webapp.env
bash scripts/deploy-board.sh --env webapp
```

- `deploy-board.sh` copies the folder to `<board>/data/brand/` on every run.
- The board serves it at `/brand/` without needing the key, so stylesheets, fonts and images load
  normally. It adds `<link rel="stylesheet" href="/brand/brand.css?v=…">` after its own styles.
- Put fonts and a logo next to `brand.css` and reference them with relative URLs:

```css
@font-face { font-family: 'My Font'; src: url(fonts/my-font.woff2) format('woff2'); }
h1 { font-family: 'My Font', system-ui, sans-serif; }
.brand img { display: none; }
.brand::before { content: ""; width: 46px; height: 46px; background: url(logo.png) center / contain no-repeat; }
```

The board's CSS variables are `--bg`, `--card`, `--fg`, `--muted`, `--line`, `--accent`, `--open`,
`--ans`, `--res`, `--field` and `--drop`. Redefine them inside `@media (prefers-color-scheme: dark)`
for dark mode.

Only ship fonts and logos you are licensed to use. If your look uses commercial fonts or a client's
logo, keep that brand folder outside this repository (`BRAND_DIR` may be an absolute path).
