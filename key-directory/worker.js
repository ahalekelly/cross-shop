const DIRECTORY_PATH = "/.well-known/http-message-signatures-directory";
const MEDIA_TYPE = "application/http-message-signatures-directory+json";
const CACHE_SECONDS = 60;
const SIGNATURE_SECONDS = 300;
const encoder = new TextEncoder();
const materialByPem = new Map();

function base64(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)));
}

function base64url(bytes) {
  return base64(bytes).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function pkcs8Bytes(pem) {
  if (typeof pem !== "string") {
    throw new Error("DIRECTORY_PRIVATE_KEY_PEM Worker secret is required");
  }
  const label = ["PRIVATE", "KEY"].join(" ");
  const match = new RegExp(`^-----BEGIN ${label}-----\\s+([A-Za-z0-9+/=\\s]+)-----END ${label}-----$`).exec(pem.trim());
  if (match === null) {
    throw new Error("DIRECTORY_PRIVATE_KEY_PEM must contain a PKCS#8 key");
  }
  const encoded = match[1].replaceAll(/\s/g, "");
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) {
    throw new Error("DIRECTORY_PRIVATE_KEY_PEM contains invalid base64");
  }
  return Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
}

async function jwkThumbprint(jwk) {
  const canonical = JSON.stringify({ crv: jwk.crv, kty: jwk.kty, x: jwk.x });
  return base64url(await crypto.subtle.digest("SHA-256", encoder.encode(canonical)));
}

async function deriveMaterial(privateKeyPem) {
  const privateKey = await crypto.subtle.importKey(
    "pkcs8",
    pkcs8Bytes(privateKeyPem),
    { name: "Ed25519" },
    true,
    ["sign"],
  );
  const privateJwk = await crypto.subtle.exportKey("jwk", privateKey);
  if (privateJwk.kty !== "OKP" || privateJwk.crv !== "Ed25519" || typeof privateJwk.x !== "string") {
    throw new Error("DIRECTORY_PRIVATE_KEY_PEM must contain an Ed25519 key");
  }
  const publicKeyJwk = {
    kty: "OKP",
    crv: "Ed25519",
    x: privateJwk.x,
    kid: await jwkThumbprint(privateJwk),
    use: "sig",
    nbf: 0,
  };
  const publicKey = await crypto.subtle.importKey(
    "jwk",
    publicKeyJwk,
    { name: "Ed25519" },
    false,
    ["verify"],
  );
  return {
    body: JSON.stringify({ keys: [publicKeyJwk] }),
    privateKey,
    publicKey,
    publicKeyJwk,
  };
}

function material(privateKeyPem) {
  if (typeof privateKeyPem !== "string") {
    throw new Error("DIRECTORY_PRIVATE_KEY_PEM Worker secret is required");
  }
  let promise = materialByPem.get(privateKeyPem);
  if (promise === undefined) {
    promise = deriveMaterial(privateKeyPem);
    materialByPem.set(privateKeyPem, promise);
  }
  return promise;
}

async function signedResponse(request, privateKeyPem) {
  const { body, privateKey, publicKey, publicKeyJwk } = await material(privateKeyPem);
  const contentDigest = `sha-256=:${base64(
    await crypto.subtle.digest("SHA-256", encoder.encode(body)),
  )}:`;
  const created = Math.floor(Date.now() / 1_000);
  const parameters =
    `("@authority";req "content-digest");alg="ed25519"` +
    `;keyid="${publicKeyJwk.kid}";tag="http-message-signatures-directory"` +
    `;created=${created};expires=${created + SIGNATURE_SECONDS}`;
  const signatureBase =
    `"@authority";req: ${new URL(request.url).host}\n` +
    `"content-digest": ${contentDigest}\n` +
    `"@signature-params": ${parameters}`;
  const signature = await crypto.subtle.sign(
    "Ed25519",
    privateKey,
    encoder.encode(signatureBase),
  );
  if (!(await crypto.subtle.verify("Ed25519", publicKey, signature, encoder.encode(signatureBase)))) {
    throw new Error("Derived public key did not verify the directory signature");
  }
  return new Response(body, {
    headers: {
      "Cache-Control": `public, max-age=${CACHE_SECONDS}, must-revalidate, no-transform`,
      "Content-Digest": contentDigest,
      "Content-Type": MEDIA_TYPE,
      Signature: `sig1=:${base64(signature)}:`,
      "Signature-Input": `sig1=${parameters}`,
    },
  });
}

export function createWorker() {
  return {
    async fetch(request, env) {
      const url = new URL(request.url);
      if (request.method !== "GET" || url.pathname !== DIRECTORY_PATH) {
        return new Response("Not found", { status: 404 });
      }
      try {
        return await signedResponse(request, env?.DIRECTORY_PRIVATE_KEY_PEM);
      } catch (error) {
        return new Response(`Directory configuration error: ${error.message}`, { status: 500 });
      }
    },
  };
}

export default createWorker();
