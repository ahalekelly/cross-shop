import { createHash, createPublicKey, verify } from "node:crypto";
import { pathToFileURL } from "node:url";

const MEDIA_TYPE = "application/http-message-signatures-directory+json";
const CACHE_SECONDS = 60;
const SIGNATURE_SECONDS = 300;
const DIRECTORY_PATH = "/.well-known/http-message-signatures-directory";
const SIGNATURE_INPUT =
  /^sig1=(\("@authority";req "content-digest"\);alg="ed25519";keyid="([A-Za-z0-9_-]+)";tag="http-message-signatures-directory";created=(\d+);expires=(\d+))$/;
const SIGNATURE = /^sig1=:([A-Za-z0-9+/]+={0,2}):$/;
const CONTENT_DIGEST = /^sha-256=:([A-Za-z0-9+/]+={0,2}):$/;

function thumbprint(jwk) {
  const canonical = JSON.stringify({ crv: jwk.crv, kty: jwk.kty, x: jwk.x });
  return createHash("sha256").update(canonical).digest("base64url");
}

function requiredHeader(response, name) {
  const value = response.headers.get(name);
  if (value === null) throw new Error(`Directory response is missing ${name}`);
  return value;
}

function directoryUrl(value) {
  const url = new URL(value);
  if (
    url.protocol !== "https:" ||
    url.username !== "" ||
    url.password !== "" ||
    url.pathname !== DIRECTORY_PATH ||
    url.search !== "" ||
    url.hash !== ""
  ) {
    throw new Error(`Directory URL must be an HTTPS ${DIRECTORY_PATH} URL`);
  }
  return url;
}

export async function verifyDirectoryResponse(response, now, target) {
  const url = directoryUrl(target);
  if (response.status !== 200) throw new Error(`Directory returned HTTP ${response.status}`);
  const contentType = requiredHeader(response, "content-type").split(";", 1)[0].toLowerCase();
  if (contentType !== MEDIA_TYPE) throw new Error(`Unexpected directory media type ${contentType}`);

  const body = Buffer.from(await response.arrayBuffer());
  const directory = JSON.parse(body);
  if (!Array.isArray(directory.keys) || directory.keys.length !== 1) {
    throw new Error("Directory must contain exactly one key");
  }
  const [jwk] = directory.keys;
  if (jwk.kty !== "OKP" || jwk.crv !== "Ed25519" || typeof jwk.x !== "string" || "d" in jwk) {
    throw new Error("Directory did not return one public Ed25519 JWK");
  }

  const digestMatch = CONTENT_DIGEST.exec(requiredHeader(response, "content-digest"));
  if (digestMatch === null) throw new Error("Directory Content-Digest has an unexpected format");
  const expectedDigest = createHash("sha256").update(body).digest();
  if (!expectedDigest.equals(Buffer.from(digestMatch[1], "base64"))) {
    throw new Error("Directory Content-Digest does not match its body");
  }

  const inputMatch = SIGNATURE_INPUT.exec(requiredHeader(response, "signature-input"));
  if (inputMatch === null) throw new Error("Directory Signature-Input has an unexpected profile");
  const [, parameters, keyId, createdValue, expiresValue] = inputMatch;
  if (thumbprint(jwk) !== keyId || jwk.kid !== keyId) {
    throw new Error("Directory key ID is not the returned JWK thumbprint");
  }
  const created = Number(createdValue);
  const expires = Number(expiresValue);
  if (expires - created !== SIGNATURE_SECONDS) {
    throw new Error(`Directory signature lifetime must be exactly ${SIGNATURE_SECONDS} seconds`);
  }
  if (created > now + 5) throw new Error("Directory signature was created in the future");
  if (expires <= now) throw new Error("Directory signature has expired");

  const cacheControl = requiredHeader(response, "cache-control");
  const maxAgeMatch = /(?:^|,\s*)max-age=(\d+)(?:,|$)/.exec(cacheControl);
  if (maxAgeMatch === null || !cacheControl.includes("must-revalidate")) {
    throw new Error("Directory cache policy must have max-age and must-revalidate");
  }
  const maxAge = Number(maxAgeMatch[1]);
  if (maxAge !== CACHE_SECONDS) {
    throw new Error(`Directory cache max-age must be exactly ${CACHE_SECONDS} seconds`);
  }
  if (expires - now <= maxAge) {
    throw new Error("Directory signature can expire while a response is still fresh");
  }

  const signatureMatch = SIGNATURE.exec(requiredHeader(response, "signature"));
  if (signatureMatch === null) throw new Error("Directory Signature has an unexpected format");
  const signatureBase =
    `"@authority";req: ${url.host}\n` +
    `"content-digest": ${requiredHeader(response, "content-digest")}\n` +
    `"@signature-params": ${parameters}`;
  const publicKey = createPublicKey({ key: jwk, format: "jwk" });
  if (!verify(null, Buffer.from(signatureBase), publicKey, Buffer.from(signatureMatch[1], "base64"))) {
    throw new Error("Directory response signature failed verification");
  }
  return { created, expires, keyId, maxAge };
}

async function main() {
  if (process.argv.length !== 3) {
    throw new Error("Usage: node verify-directory.mjs <directory-url>");
  }
  const url = directoryUrl(process.argv[2]);
  const response = await fetch(url, {
    headers: { Accept: MEDIA_TYPE },
    redirect: "error",
  });
  const result = await verifyDirectoryResponse(response, Math.floor(Date.now() / 1_000), url);
  console.log(JSON.stringify({ ok: true, status: response.status, ...result }));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
