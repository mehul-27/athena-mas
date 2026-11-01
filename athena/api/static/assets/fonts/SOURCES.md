# Geist and Geist Mono — vendored fonts

The Asciify background page computes to:

- body / headings: `Geist, ui-sans-serif, system-ui, sans-serif`
- technical/code: `"Geist Mono", "JetBrains Mono", ui-monospace, monospace`

Both families are licensed under the SIL Open Font License 1.1. `LICENSE` is the
upstream Geist license text. The fonts are served locally; Athena has no runtime
font-provider dependency.

| File | Source | SHA-256 |
|---|---|---|
| `geist-latin-variable.woff2` | `https://asciify.org/fonts/Geist-Latin-9b6f5ff45b27.woff2` (the exact reference page asset; upstream: Geist by Vercel) | `9b6f5ff45b278c744b5f379a2c4ecbaf858a842b8eaf82ac8d21b699ca16c608` |
| `geist-mono-latin-variable.woff2` | Google Fonts CSS v6 latin subset, `Geist+Mono` (upstream: Geist Mono by Vercel) | `684ad5b531f81d43c1e8c7038262d5db7cdc1f68006e04d6c7769efa8d33c8cc` |
| `LICENSE` | `https://raw.githubusercontent.com/vercel/geist-font/main/LICENSE.txt` | — |

The Geist file is the Latin variable subset (weight 100–900). The Geist Mono file
is the Latin variable subset used by the reference page's Google Fonts CSS.
