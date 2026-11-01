# Asciify Capillary ? vendored source

- Package: `asciify-engine` 4.32.8 (MIT).
- Official docs: https://asciify.org/docs/backgrounds/capillary
- Package: https://unpkg.com/asciify-engine@4.32.8/
- Entry: `capillary.js` re-exports `background` and `capillary` only.

The Asciify skill states the site no longer offers downloadable template files;
the official Capillary integration is the kit export. No standalone helper was
invented. The nine JS files below are the complete import closure.

| File | SHA-256 | Bytes |
|---|---|---|
| `capillary.js` | `88971e07148c3ee3aab52b61834d45cb5a3a6ad79cdbb46b7f53c580f0b99828` | 386 |
| `field.js` | `bf0680b5ac0912ead0eae68de26361c7980a327b4efb46a7b2370c5e253e0b3a` | 114924 |
| `kit/chunk-22CNXLJH.js` | `022ed5388bf7fd826726bf6b4ee51e8b10329c4e759517fe98c02ead7891e628` | 4209 |
| `kit/chunk-3AKXSVWP.js` | `0cf6fff239d5a19a1b426256569c0c3726087653c148cbabb5b7b8065f6dc7b7` | 2833 |
| `kit/chunk-3WSUMOZD.js` | `3d102ca895932d7bfc1922f80c5685a6c3768fef325e23ee687a8b8003d90070` | 8413 |
| `kit/chunk-IPYPFSYJ.js` | `fa5e2875f8a1dc1a949dc456254405cec082387f7182c6ddfc0e8b9527da39e7` | 13632 |
| `kit/chunk-LRICD7AF.js` | `2d5f73d60c3ec5690948ba0ddf5cbc22bb20530eb06bc7c391012443f1a8a88e` | 3208 |
| `kit/chunk-TIDLQIUB.js` | `cae0fb60a89b0bcb62f45c3f5ab20081edfec86ff0663808275b54ca25d3c966` | 2099 |
| `kit/chunk-WFSPPRPQ.js` | `50ffea5dea94aad638127ebed644d82b2975d8a48f795bbb263b4c68a8a78b3f` | 5652 |
| `kit/chunk-ZCHGV2CV.js` | `babca83d3381cb95931cf509b5afe6c67a64395a948b743872e43e9e5133a6bd` | 1491 |
| `LICENSE` | `60c568870b997b7082eadfaed333334b28e65921906a1f0b4f2566934cc42783` | 1067 |

## Update

1. Download the same file set from the new package version.
2. Keep the `dist/field.js` + `dist/kit/chunk-*.js` relative layout.
3. Recompute the hashes above and update this file.
4. Run `pytest tests/test_assets_and_tutorial.py` (import-graph check).

Do not vendor `dist/kit.js`: it re-exports every component and would make the
browser fetch the whole kit.
