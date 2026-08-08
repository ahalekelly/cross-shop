# cross-shop

`cross-shop` is a batch-first CLI and Python library for product search, product detail, and anonymous destination shipping quotes across public storefronts and marketplaces. Its default JSON output is compact for AI agents: store constants are hoisted, empty fields disappear, search results contain handles instead of refs, and image URLs remain in the run cache.

## Run

From this directory:

```sh
uv run cross-shop search '[{"store":"https://example.com","query":"bearing"}]'
uv run cross-shop product '["r1.1.1"]'
uv run cross-shop quote '[{"store":"https://example.com","lines":[{"item":"r1.1.1.2","quantity":3}]}]'
uv run cross-shop images r1.1.1 1:3
uv run cross-shop config show
uv run pytest
```

After installation, use the `cross-shop` console command. A source checkout runs the same command with `uv run --project <path-to-this-directory> cross-shop …` or `uvx --from . cross-shop …`.

## Commands

- `search <entries-json> [--limit 20] [--description-chars 300] [--redetect] [--debug]` accepts 1–100 `{store,query}` entries. Repeated stores share one session and detection and their products are deduplicated.
- `product <items-json> [--description-chars 2000] [--redetect] [--debug]` accepts 1–100 product URLs, run handles, or ref objects.
- `quote <quotes-json> [--destination <json>] [--redetect] [--debug]` accepts 1–20 stores with 1–20 lines each. One cart contains every line for a store entry.
- `images <item-handle> [N|START:END]` downloads at most ten cached product images and prints absolute paths.
- `config set-destination <json>`, `config show`, and `config import-vendors <path>` manage persistent configuration. `config show` reports whether credential blocks are configured without printing credential values or private-key paths.

Search and product write monotonic run IDs. `r7.2.5` means item 5 from store 2 in run 7; `r7.2.5.3` means its third variant. Handles expire seven days after creation even if garbage collection has not run. Product detail emits strict, self-contained durable refs such as `{"platform":"shopify","store":"https://example.com","variant_id":"gid://shopify/ProductVariant/123"}`. Product URLs with query strings or fragments are rejected because those components can carry product identity; use a durable ref instead.

Store workers run concurrently, up to five at a time. Operations for one store remain sequential in one isolated cookie jar. An unexpected adapter exception becomes an `api_error` for that store without aborting the other workers. Exit status is 1 when any entry has `status:api_error`.

## Storefronts and marketplaces

Storefront adapters cover Shopify, WooCommerce, Magento GraphQL/HTML/guest REST, BigCommerce Stencil/Storefront REST, Squarespace, Wix, Ecwid, and Salesforce Commerce Cloud boundaries. Wix, Ecwid, and customized SFCC/OpenCart checkout remain explicit browser boundaries.

These pseudo-store origins use marketplace discovery without live platform detection:

| Origin | Backend | Detail/quote boundary |
| --- | --- | --- |
| `https://shop.app` | Shopify Global Catalog UCP MCP | Detail preserves seller storefront/API domains and offer handoff links; quote a merchant offer |
| `https://www.aliexpress.com` | AliExpress Affiliate API | Affiliate search only; no quote |
| `https://shopping.google.com` | SerpApi Google Shopping | Unverified leads; quote the merchant |
| `https://www.amazon.com` | SerpApi Amazon | Unverified listings; no anonymous cart API |
| `https://www.ebay.com` | eBay Browse API | Detail includes shipping; checkout APIs are restricted-tier |

Google Shopping, Amazon, and AliExpress results are leads. Re-verify the exact merchant listing, variant, stock, and delivered price.

## Data and settings

The default data directory comes from `platformdirs.user_data_path("cross-shop")`. Set `CROSS_SHOP_DATA_DIR` to override it. Runtime data never writes into the package tree.

- `settings.json` stores destination and optional integrations.
- `vendors.json` is the atomic, lock-protected canonical-origin platform registry.
- `run-counter` is never reset; deleted runs cannot make old handles point at new data.
- `runs/` stores full seven-day result payloads, including refs and image URLs omitted from stdout.
- `images/` stores files downloaded by `images`.

Destination precedence is `--destination`, then `settings.json`, then the built-in San Francisco address. `country` and `postal_code` are required; `region`, `city`, and `address1` are optional.

Settings example:

```json
{
  "destination": {"country":"US","region":"CA","city":"San Francisco","address1":"747 Howard St","postal_code":"94103"},
  "web_bot_auth": {"private_key_path":"/secure/private.pem","key_directory_url":"https://agent.example/keys.json"},
  "ebay": {"client_id":"…","client_secret":"…"},
  "shopify_global": {"profile_url":"https://agent.example/profile.json"}
}
```

## Credentials

- `SERPAPI_API_KEY`: SerpApi Google Shopping and Amazon engines.
- `ALIEXPRESS_APP_KEY` and `ALIEXPRESS_APP_SECRET`: AliExpress Affiliate Product Query.
- `settings.ebay.client_id` and `settings.ebay.client_secret`: eBay Developers Program production Browse API keyset. Production access requires eBay marketplace account-deletion notification compliance.
- `settings.shopify_global.profile_url`: public UCP agent profile URL required by Shopify's current `https://catalog.shopify.com/api/ucp/mcp` Global Catalog contract.

Missing marketplace credentials produce a structured setup error only when that marketplace is requested.

## Web Bot Auth

Shopify product URLs resolve through the public Ajax product endpoint; durable variant refs resolve through Storefront GraphQL. Unsigned Shopify HTTP is the default and normal public-tool behavior. `settings.web_bot_auth` opts into Ed25519 HTTP Message Signatures. The signer validates the key type and JWK thumbprint, creates fresh nonce and expiry material for every request, refuses pre-signed requests, and signs redirects only after the Shopify adapter verifies the same HTTPS authority and API path. A configured but missing or unreadable key is an `api_error` naming its path; the tool never silently falls back to unsigned traffic.
