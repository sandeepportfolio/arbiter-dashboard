import { describe, expect, it } from "vitest";
import { inferStaticApiBase, mixedContentApiWarning, normalizeApiBase, shouldInferSameHostApi } from "./api-base.js";

describe("api base inference", () => {
  it("normalizes trailing slashes", () => {
    expect(normalizeApiBase(" https://arbiter.example.com/// ")).toBe("https://arbiter.example.com");
  });

  it("prefers an explicit api query parameter", () => {
    const apiBase = inferStaticApiBase({
      searchParams: new URLSearchParams("api=https://api.example.com"),
      boot: { staticFrontend: true, defaultApiPort: "8090" },
      storageValue: "http://127.0.0.1:8090",
      locationHref: "https://sandeepportfolio.github.io/arbiter-dashboard/",
    });
    expect(apiBase).toBe("https://api.example.com");
  });

  it("reuses a stored api base before attempting host inference", () => {
    const apiBase = inferStaticApiBase({
      searchParams: new URLSearchParams(),
      boot: { staticFrontend: true, defaultApiPort: "8090" },
      storageValue: "http://10.0.0.5:8090",
      locationHref: "https://sandeepportfolio.github.io/arbiter-dashboard/",
    });
    expect(apiBase).toBe("http://10.0.0.5:8090");
  });

  it("infers the api on localhost and private network hosts", () => {
    expect(
      inferStaticApiBase({
        searchParams: new URLSearchParams(),
        boot: { staticFrontend: true, defaultApiPort: "8090" },
        storageValue: "",
        locationHref: "http://127.0.0.1:8092/",
      })
    ).toBe("http://127.0.0.1:8090");

    expect(
      inferStaticApiBase({
        searchParams: new URLSearchParams(),
        boot: { staticFrontend: true, defaultApiPort: "8090" },
        storageValue: "",
        locationHref: "http://10.10.112.124:8092/",
      })
    ).toBe("http://10.10.112.124:8090");
  });

  it("does not auto-infer an api for public static hosts", () => {
    const apiBase = inferStaticApiBase({
      searchParams: new URLSearchParams(),
      boot: { staticFrontend: true, defaultApiPort: "8090" },
      storageValue: "",
      locationHref: "https://sandeepportfolio.github.io/arbiter-dashboard/",
    });
    expect(apiBase).toBe("");
    expect(shouldInferSameHostApi("https://sandeepportfolio.github.io/arbiter-dashboard/")).toBe(false);
  });

  it("allows explicit opt-in for same-host inference on public domains", () => {
    const apiBase = inferStaticApiBase({
      searchParams: new URLSearchParams(),
      boot: {
        staticFrontend: true,
        defaultApiPort: "8090",
        allowSameHostApiInference: true,
      },
      storageValue: "",
      locationHref: "https://arbiter.example.com/",
    });
    expect(apiBase).toBe("https://arbiter.example.com:8090");
  });

  it("warns when an https dashboard tries to call an http api", () => {
    expect(mixedContentApiWarning("https://sandeepportfolio.github.io/arbiter-dashboard/", "http://10.10.112.124:8090")).toContain(
      "cannot call an HTTP API"
    );
    expect(mixedContentApiWarning("https://sandeepportfolio.github.io/arbiter-dashboard/", "https://api.example.com")).toBe("");
    expect(mixedContentApiWarning("http://10.10.112.124:8092/", "http://10.10.112.124:8090")).toBe("");
  });
});
