# Web Bot Auth key directory

This Cloudflare Worker publishes and signs the public Ed25519 key for your Web Bot Auth identity. It derives the directory's public JWK and thumbprint from the Worker secret, so the directory is fully determined by that secret and cannot drift from it.

The response follows [Cloudflare's key-directory enrollment profile](https://developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/#2-host-a-key-directory):

- `Signature-Input` covers the original request's `@authority` using `;req`.
- `created`, `expires`, `keyid`, `alg="ed25519"`, and `tag="http-message-signatures-directory"` describe the signature.
- `Content-Digest` is covered following the [HTTP Message Signatures Directory draft](https://datatracker.ietf.org/doc/draft-meunier-webbotauth-httpsig-directory/).
- `Cache-Control` keeps a response fresh for 60 seconds while its signature remains valid for 300 seconds.

`Signature-Agent` belongs on requests sent to storefronts. The directory response carries `Signature-Input` and `Signature` instead.

## Set up

1. Replace `example.com` in `wrangler.toml` with a domain in your Cloudflare account.
2. Generate a PKCS#8 Ed25519 key:

   ```sh
   openssl genpkey -algorithm ed25519 -out private.pem
   ```

3. Store the key as a Worker secret, then deploy:

   ```sh
   npx wrangler secret put DIRECTORY_PRIVATE_KEY_PEM < private.pem
   npx wrangler deploy
   ```

   Keep this order. `wrangler secret put` creates a Worker version, and the following deploy can satisfy `[secrets].required`. Keep `private.pem` outside version control and deployment bundles.

4. Verify the live directory:

   ```sh
   node verify-directory.mjs https://example.com/.well-known/http-message-signatures-directory
   ```

The verifier checks the content digest, JWK thumbprint, signature and cache lifetimes, and Ed25519 response signature. It prints only non-secret validation metadata.

Cloudflare verified-bot enrollment starts after the live verifier succeeds. Shopify documents its signed Web Bot Auth rate tier independently of Cloudflare enrollment; directory validation does not change Shopify's tier assignment.

## Test

The tests generate an ephemeral key and verify the response using only its returned public JWKS:

```sh
node --test worker.test.mjs
```
