---
name: har2recipe
description: Turn a website's own JSON traffic (a HAR exported from browser DevTools) into a Python client and a recipe.json that replays with plain HTTP and no model. Use when the user wants data from a site that has no public API, or when a browser-automation or scraping step should become a fast, repeatable fetch.
---

# har2recipe

Use the `har2recipe` CLI. Install it if it is missing: `uv tool install git+https://github.com/trungtin/har2recipe`.

## Steps

1. **Get a HAR.** Ask the user to open DevTools > Network on the page, tick Preserve log, do the action that loads the data, and export the HAR. If you drive a browser yourself, record a HAR of that one action. Keep it small.
2. **Look first.** Run `har2recipe calls site.har` and confirm the data the user wants appears in one of the listed calls.
3. **Learn.** Run `har2recipe learn site.har --goal "<what the user wants, with the fields>"`. It uses the user's own `claude` and writes a folder with `recipe.json`, `api_client.py`, `example_usage.py` and `README.md`, then replays the recipe once.
4. **Replay.** Run `har2recipe run <folder>/recipe.json [-p key=value] --format json`. Use this from now on instead of a browser: no model, no tokens.
5. **Keep it fresh.** Before you trust old output, run `har2recipe verify <folder>/recipe.json`. Exit code 2 means stale: get a fresh HAR and run `har2recipe verify <folder>/recipe.json --har fresh.har`.

## Rules

- Never paste a HAR's cookies or tokens into chat. The CLI redacts them; secrets go in environment variables named in the recipe's `needs`.
- Show the user the generated code before running it on anything sensitive.
- Only fetch data the user is allowed to read, at a polite rate.
