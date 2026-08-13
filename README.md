# cross-shop

`cross-shop` batches product search, exact product detail, and anonymous destination shipping quotes across public storefronts and marketplaces. Its compact JSON output is built for AI agents: store constants are hoisted, empty fields disappear, search results use handles instead of refs, and image URLs stay in the run cache.

## Install

Install the CLI from PyPI:

```sh
uv tool install cross-shop
cross-shop search '[{"store":"https://example.com","query":"bearing"}]'
```

Run a one-off command without installing:

```sh
uvx cross-shop search '[{"store":"https://example.com","query":"bearing"}]'
```

Other commands follow the same form:

```sh
cross-shop product '["r1.1.1"]'
cross-shop quote '[{"store":"https://example.com","lines":[{"item":"r1.1.1.2","quantity":3}]}]'
cross-shop images r1.1.1 1:3
cross-shop config show
```

From a source checkout, replace `cross-shop` with `uv run --project <package-dir> cross-shop`. Run tests with `uv run --project <package-dir> pytest`.

## MCP

The stdio MCP server exposes `search`, `product`, `quote`, and `images` with the same compact results as the CLI. Register it with Claude Code:

```sh
claude mcp add cross-shop -- uvx cross-shop mcp
```

Equivalent `.mcp.json`:

```json
{
  "mcpServers": {
    "cross-shop": {
      "command": "uvx",
      "args": ["cross-shop", "mcp"]
    }
  }
}
```

Pi can read the same config through [pi-mcp-adapter](https://www.npmjs.com/package/pi-mcp-adapter), installed with `pi install npm:pi-mcp-adapter`.

## Commands

- `search <entries-json> [--limit 20] [--description-chars 300] [--redetect] [--debug]` accepts 1–100 `{store,query}` entries. Repeated stores share one session and detection, and products are deduplicated.
- `product <items-json> [--description-chars 2000] [--redetect] [--debug]` accepts 1–100 product URLs, run handles, or ref objects.
- `quote <quotes-json> [--destination <json>] [--redetect] [--debug]` accepts 1–20 stores with 1–20 lines each. One cart contains every line for a store entry.
- `images <item-handle> [N|START:END]` downloads at most ten cached product images and prints absolute paths.
- `config set-destination <json>`, `config show`, and `config import-vendors <path>` manage persistent configuration. `config show` reports configured credential blocks without printing secrets or private-key paths.
- `mcp` runs the stdio MCP server.

Search and product write monotonic run IDs. `r7.2.5` means item 5 from store 2 in run 7; `r7.2.5.3` means its third variant. Handles expire after seven days. Product detail emits strict, self-contained durable refs such as `{"platform":"shopify","store":"https://example.com","variant_id":"gid://shopify/ProductVariant/123"}`. Product URLs with query strings or fragments are rejected because those components can carry identity; Amazon product URLs are normalized to their ASIN.

Store workers run concurrently, up to five at a time. Operations for one store remain sequential in one isolated cookie jar. An unexpected adapter exception becomes an `api_error` for that store without aborting other workers. Exit status is 1 when any entry has `status:api_error`.

## Storefronts and marketplaces

Storefront adapters cover Shopify, WooCommerce, Magento GraphQL/HTML/guest REST, BigCommerce Stencil/Storefront REST, Squarespace, Wix, Ecwid, and Salesforce Commerce Cloud boundaries. Wix, Ecwid, and customized SFCC or OpenCart checkout remain explicit browser boundaries.

These origins use marketplace adapters without live platform detection:

| Origin | Backend | Detail and quote boundary |
| --- | --- | --- |
| `https://shop.app` | Shopify Global Catalog UCP MCP | Detail preserves seller domains and handoff links; quote a merchant offer |
| `https://www.aliexpress.com` | AliExpress Affiliate API | Affiliate search only; no quote |
| `https://shopping.google.com` | SerpApi Google Shopping | Unverified leads; quote the merchant |
| `https://www.amazon.com` | SerpApi search and Amazon all-offers display detail | Exact ASIN detail; no anonymous cart API |
| `https://www.ebay.com` | eBay Browse API | Detail includes shipping; checkout APIs are restricted-tier |

Google Shopping, Amazon search, and AliExpress results are leads. Re-verify the exact listing, variant, stock, and delivered price.

## Data and settings

The default data directory comes from `platformdirs.user_data_path("cross-shop")`. Set `CROSS_SHOP_DATA_DIR` to override it. Runtime data never writes into the package tree.

- `settings.json` stores destination and optional integrations.
- `vendors.json` is the atomic, lock-protected canonical-origin platform registry.
- `run-counter` never resets, so deleted runs cannot make old handles point at new data.
- `runs/` stores full seven-day result payloads, including refs and image URLs omitted from stdout.
- `images/` stores files downloaded by `images`.

Destination precedence is `--destination`, then `settings.json`, then the built-in San Francisco address. `country` and `postal_code` are required; `region`, `city`, and `address1` are optional.

```json
{
  "destination": {"country":"US","region":"CA","city":"San Francisco","address1":"747 Howard St","postal_code":"94103"},
  "web_bot_auth": {"private_key_path":"/secure/private.pem","key_directory_url":"https://agent.example/.well-known/http-message-signatures-directory"},
  "ebay": {"client_id":"…","client_secret":"…"},
  "shopify_global": {"profile_url":"https://agent.example/profile.json"}
}
```

## Credentials

- `SERPAPI_API_KEY`: SerpApi Google Shopping and Amazon search.
- `ALIEXPRESS_APP_KEY` and `ALIEXPRESS_APP_SECRET`: AliExpress Affiliate Product Query.
- `settings.ebay.client_id` and `settings.ebay.client_secret`: eBay Developers Program production Browse API keyset. Production access requires eBay marketplace account-deletion notification compliance.
- `settings.shopify_global.profile_url`: public UCP agent profile required by Shopify's Global Catalog contract.

Missing marketplace credentials produce a structured setup error only when that marketplace is requested.

## Web Bot Auth

Shopify product URLs resolve through the public Ajax product endpoint; durable variant refs resolve through Storefront GraphQL. Unsigned Shopify HTTP is the default. `settings.web_bot_auth` opts into Ed25519 HTTP Message Signatures. The signer validates the key type and JWK thumbprint, creates fresh nonce and expiry material for every request, refuses pre-signed requests, and signs redirects only after the Shopify adapter verifies the HTTPS authority and API path. A configured but missing or unreadable key is an `api_error`; the tool never falls back to unsigned traffic.

Host your public key directory with the included [Cloudflare Worker](key-directory/README.md).
