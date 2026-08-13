import assert from "node:assert/strict";
import { createHash, generateKeyPairSync } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createWorker } from "./worker.js";
import { verifyDirectoryResponse } from "./verify-directory.mjs";

const TARGET = "https://example.com/.well-known/http-message-signatures-directory";
const NOW = 1_800_000_000;

function fixture() {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  return {
    privateKeyPem: privateKey.export({ format: "pem", type: "pkcs8" }),
    publicJwk: publicKey.export({ format: "jwk" }),
  };
}

async function responseFrom(worker, privateKeyPem) {
  const originalNow = Date.now;
  Date.now = () => NOW * 1_000;
  try {
    return await worker.fetch(new Request(TARGET), {
      DIRECTORY_PRIVATE_KEY_PEM: privateKeyPem,
    });
  } finally {
    Date.now = originalNow;
  }
}

test("derives and signs a directory that an independent verifier accepts", async () => {
  const { privateKeyPem, publicJwk } = fixture();
  const response = await responseFrom(createWorker(), privateKeyPem);
  const copy = response.clone();
  const result = await verifyDirectoryResponse(response, NOW, TARGET);
  const directory = await copy.json();
  const [published] = directory.keys;

  assert.equal(published.x, publicJwk.x);
  assert.equal("d" in published, false);
  assert.equal(result.keyId, published.kid);
  assert.equal(result.created, NOW);
  assert.equal(result.expires, NOW + 300);
  assert.equal(result.maxAge, 60);
  assert.equal(
    published.kid,
    createHash("sha256")
      .update(JSON.stringify({ crv: published.crv, kty: published.kty, x: published.x }))
      .digest("base64url"),
  );
});

test("covers the directory body with Content-Digest", async () => {
  const { privateKeyPem } = fixture();
  const response = await responseFrom(createWorker(), privateKeyPem);
  const headers = new Headers(response.headers);
  const body = `${await response.text()} `;

  await assert.rejects(
    verifyDirectoryResponse(new Response(body, { headers }), NOW, TARGET),
    /Content-Digest does not match/,
  );
});

test("rejects a signature lifetime outside the production profile", async () => {
  const { privateKeyPem } = fixture();
  const response = await responseFrom(createWorker(), privateKeyPem);
  const headers = new Headers(response.headers);
  headers.set(
    "signature-input",
    headers.get("signature-input").replace(`expires=${NOW + 300}`, `expires=${NOW + 301}`),
  );

  await assert.rejects(
    verifyDirectoryResponse(
      new Response(await response.arrayBuffer(), { headers }),
      NOW,
      TARGET,
    ),
    /signature lifetime must be exactly 300 seconds/,
  );
});

test("rejects a cache lifetime outside the production profile", async () => {
  const { privateKeyPem } = fixture();
  const response = await responseFrom(createWorker(), privateKeyPem);
  const headers = new Headers(response.headers);
  headers.set("cache-control", headers.get("cache-control").replace("max-age=60", "max-age=61"));

  await assert.rejects(
    verifyDirectoryResponse(
      new Response(await response.arrayBuffer(), { headers }),
      NOW,
      TARGET,
    ),
    /cache max-age must be exactly 60 seconds/,
  );
});

test("fails loudly when the Worker secret is absent or invalid", async () => {
  const worker = createWorker();
  for (const secret of [undefined, "not a key"]) {
    const response = await worker.fetch(new Request(TARGET), {
      DIRECTORY_PRIVATE_KEY_PEM: secret,
    });
    assert.equal(response.status, 500);
    assert.match(await response.text(), /Directory configuration error/);
    assert.equal(response.headers.has("signature"), false);
    assert.equal(response.headers.has("signature-input"), false);
  }
});

test("keeps non-directory and non-GET responses unsigned", async () => {
  const worker = createWorker();
  const responses = await Promise.all([
    worker.fetch(new Request("https://example.com/not-the-directory"), {}),
    worker.fetch(new Request(TARGET, { method: "POST" }), {}),
  ]);

  for (const response of responses) {
    assert.equal(response.status, 404);
    assert.equal(response.headers.has("signature"), false);
    assert.equal(response.headers.has("signature-input"), false);
    assert.equal((await response.text()).includes("keys"), false);
  }
});

test("Wrangler requires the private-key secret without storing its value", async () => {
  const config = await readFile(new URL("./wrangler.toml", import.meta.url), "utf8");
  assert.match(config, /\[secrets\]\nrequired = \["DIRECTORY_PRIVATE_KEY_PEM"\]/);
  assert.doesNotMatch(config, /DIRECTORY_PRIVATE_KEY_PEM\s*=/);
});
