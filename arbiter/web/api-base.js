export function normalizeApiBase(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

function isIpv4Hostname(hostname) {
  const octets = String(hostname || "").split(".");
  if (octets.length !== 4) return false;
  return octets.every((part) => /^\d+$/.test(part) && Number(part) >= 0 && Number(part) <= 255);
}

function isPrivateIpv4(hostname) {
  if (!isIpv4Hostname(hostname)) return false;
  const [first, second] = hostname.split(".").map(Number);
  return (
    first === 10 ||
    first === 127 ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168) ||
    (first === 169 && second === 254)
  );
}

export function shouldInferSameHostApi(currentUrl, options = {}) {
  const { allowSameHostApiInference = false } = options;

  try {
    const current = currentUrl instanceof URL ? currentUrl : new URL(currentUrl);
    if (!/^https?:$/.test(current.protocol)) return false;

    const hostname = String(current.hostname || "").trim().toLowerCase();
    if (!hostname) return false;
    if (allowSameHostApiInference) return true;
    if (hostname === "localhost" || hostname === "::1" || hostname.endsWith(".local")) return true;
    return isPrivateIpv4(hostname);
  } catch {
    return false;
  }
}

export function inferStaticApiBase({ searchParams, boot = {}, storageValue = "", locationHref = "" }) {
  const explicit = normalizeApiBase(searchParams?.get("api") || boot.defaultApiBase || storageValue);
  if (explicit) return explicit;
  if (!Boolean(boot.staticFrontend)) return "";

  const hintedPort = String(searchParams?.get("apiPort") || boot.defaultApiPort || "").trim();
  if (!hintedPort) return "";

  try {
    const current = new URL(locationHref || window.location.href);
    if (!shouldInferSameHostApi(current, boot)) return "";
    return normalizeApiBase(`${current.protocol}//${current.hostname}:${hintedPort}`);
  } catch {
    return "";
  }
}
