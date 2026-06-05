/* =================================================================
   Lúmen Stays — app.js  (vanilla, no dependencies)
   ================================================================= */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const money = (n) => "$" + Math.round(n).toLocaleString("en-US");
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const HERO_IMG = "assets/img/s144/03.jpg";
  const CLEANING = 75;
  const SERVICE_RATE = 0.08;

  /* ---------------- Icons (24x24, stroke=currentColor) ---------------- */
  const P = {
    arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    guests: '<path d="M9 11a3 3 0 100-6 3 3 0 000 6zM2 20a7 7 0 0114 0M17 11a3 3 0 10-1-5.83M22 20a7 7 0 00-5-6.7"/>',
    bed: '<path d="M3 7v11M3 12h18M21 12v6M3 12V9a2 2 0 012-2h6a2 2 0 012 2v3"/>',
    bath: '<path d="M4 12V6a2 2 0 012-2 2 2 0 012 2M3 12h18v2a5 5 0 01-5 5H8a5 5 0 01-5-5v-2zM7 19l-1 2M18 19l1 2"/>',
    door: '<path d="M14 3v18M6 3h11v18H6zM10 12h.01"/>',
    ceiling: '<path d="M3 4h18M5 4v16M19 4v16M9 4v16M15 4v16"/>',
    chef: '<path d="M6 12a4 4 0 01-1-7.87A4 4 0 0112 4a4 4 0 017 4 4 4 0 01-1 4M6 12h12v6a2 2 0 01-2 2H8a2 2 0 01-2-2z"/>',
    game: '<path d="M6 11h4M8 9v4M15 11h.01M18 12h.01M17 7H7a4 4 0 00-4 4l-1 5a2.5 2.5 0 004.6 1.8L8 16h8l1.4 1.8A2.5 2.5 0 0022 16l-1-5a4 4 0 00-4-4z"/>',
    train: '<path d="M8 3h8a3 3 0 013 3v8a3 3 0 01-3 3H8a3 3 0 01-3-3V6a3 3 0 013-3zM5 13h14M9 17l-2 4M15 17l2 4M9 7h6"/>',
    pool: '<path d="M2 16c1.5 0 1.5 1.3 3 1.3S8.5 16 10 16s1.5 1.3 3 1.3S16.5 16 18 16s1.5 1.3 3 1.3M2 20c1.5 0 1.5 1.3 3 1.3M7 14V5a2 2 0 014 0M7 9h4"/>',
    candle: '<path d="M10 7c0-2 2-2.5 2-5 .8 2.5 2 3 2 5a2 2 0 11-4 0zM8 11h8v9H8zM8 14h8"/>',
    coffee: '<path d="M4 8h13v5a4 4 0 01-4 4H8a4 4 0 01-4-4V8zM17 9h2a2 2 0 010 4h-2M6 4v1M9 4v1M12 4v1"/>',
    balcony: '<path d="M3 21h18M5 21v-7h14v7M4 14h16l-1-5H5l-1 5M9 21v-4M15 21v-4M12 4v5"/>',
    shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3zM9 12l2 2 4-4"/>',
    lake: '<path d="M3 18c2 0 2 1.2 4 1.2S11 18 13 18s2 1.2 4 1.2 2-1.2 4-1.2M5 14l3-9 3 6 2-3 4 6M3 22h18"/>',
    wine: '<path d="M8 3h8l-1 6a3 3 0 01-6 0L8 3zM12 12v6M9 21h6"/>',
    fitness: '<path d="M6.5 6.5l11 11M4 9l-1 1 2 2M20 15l1-1-2-2M7 4L4 7M17 20l3-3M9 15l-3 3M15 9l3-3"/>',
    monitor: '<path d="M3 4h18v12H3zM8 20h8M12 16v4"/>',
    mirror: '<path d="M12 2a6 8 0 000 16 6 8 0 000-16zM7 22h10M12 18v4"/>',
    tv: '<path d="M3 5h18v12H3zM8 21h8M7 9l3 3-3 3"/>',
    arcade: '<path d="M7 3h10a2 2 0 012 2v14a2 2 0 01-2 2H7a2 2 0 01-2-2V5a2 2 0 012-2zM8 6h8v4H8zM9 14h.01M15 14h.01M12 13v3"/>',
    fire: '<path d="M12 3c1 3 4 4 4 8a4 4 0 01-8 0c0-1 .5-2 1-2.5C9 11 12 10 12 3zM12 21v0"/>',
    sofa: '<path d="M4 11V8a2 2 0 012-2h12a2 2 0 012 2v3M2 13a2 2 0 012-2 2 2 0 012 2v3h12v-3a2 2 0 014 0v4a2 2 0 01-2 2H4a2 2 0 01-2-2v-4zM6 19v2M18 19v2"/>',
    wifi: '<path d="M2 8.8a16 16 0 0120 0M5 12a11 11 0 0114 0M8.5 15.2a6 6 0 017 0M12 19h.01"/>',
    key: '<path d="M15 7a4 4 0 11-3.5 6L9 15.5V18H6.5L4 20.5 2.5 19 11 10.5A4 4 0 0115 7zM16.5 8.5h.01"/>',
    clock: '<path d="M12 7v5l3 2M12 21a9 9 0 100-18 9 9 0 000 18z"/>',
    paw: '<path d="M5.5 11a1.5 2 0 100-4 1.5 2 0 000 4zM9.5 8a1.5 2 0 100-4 1.5 2 0 000 4zM14.5 8a1.5 2 0 100-4 1.5 2 0 000 4zM18.5 11a1.5 2 0 100-4 1.5 2 0 000 4zM12 11c-2.5 0-4 2-4 4 0 1.7 1.3 3 3 3h2c1.7 0 3-1.3 3-3 0-2-1.5-4-4-4z"/>',
    moon: '<path d="M21 13A9 9 0 1111 3a7 7 0 0010 10z"/>',
    "no-smoke": '<path d="M3 3l18 18M18 12h2v3M16 12v3H8M4 15v-3h7M19 8a2 2 0 000-4"/>',
    calendar: '<path d="M4 5h16v15H4zM4 9h16M8 3v4M16 3v4M9 14h6"/>',
    climate: '<path d="M4 8h16M4 12h16M4 16h10M18 15a2 2 0 11.01 3.99"/>',
    fan: '<path d="M12 12c0-3 1-6-1-6s-4 2-4 4 2 2 5 2zM12 12c3 0 6 1 6-1s-2-4-4-4-2 2-2 5zM12 12c0 3-1 6 1 6s4-2 4-4-2-2-5-2zM12 12c-3 0-6-1-6 1s2 4 4 4 2-2 2-5z"/>',
    iron: '<path d="M3 16h13a5 5 0 00-5-5H7l-4 3v2zM3 19h12M16 8l1-3h3"/>',
    towel: '<path d="M6 3h12a1 1 0 011 1v16a1 1 0 01-1 1H6zM6 3a2 2 0 00-2 2v3h2M9 7v10"/>',
    blanket: '<path d="M4 6h16v9a3 3 0 01-3 3H4zM4 18v3M8 6v12M16 9c0 2-2 2-2 4"/>',
    shade: '<path d="M4 4h16M5 4v9a7 7 0 0014 0V4M12 13v4M12 17a2 2 0 100 4 2 2 0 000-4z"/>',
    laundry: '<path d="M5 3h14v18H5zM8 6h.01M11 6h.01M12 17a4.5 4.5 0 100-9 4.5 4.5 0 000 9zM9.5 11c1.5 1.5 3.5 1.5 5 0"/>',
    kitchen: '<path d="M6 2v8a2 2 0 104 0V2M8 10v12M16 2c-1.5 0-2 3-2 5s.5 4 2 4 2-2 2-4-.5-5-2-5zM16 15v7"/>',
    desk: '<path d="M3 8h18M5 8v12M19 8v12M3 8l2-3h14l2 3M9 12h6"/>',
    lounge: '<path d="M3 18l2-9h14l2 9M5 18v3M19 18v3M9 9V6a3 3 0 016 0v3"/>',
    parking: '<path d="M5 3h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2zM9 17V7h4a3 3 0 010 6H9"/>',
    check: '<path d="M20 6L9 17l-5-5"/>',
    back: '<path d="M19 12H5M11 18l-6-6 6-6"/>',
    star: '<path d="M12 3l2.5 6 6.5.5-5 4.2 1.6 6.3L12 16.8 6.4 20l1.6-6.3-5-4.2 6.5-.5z"/>'
  };
  function icon(name, cls) {
    const d = P[name] || P.star;
    return `<svg class="${cls || ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  }
  function groupIcon(name) {
    const n = name.toLowerCase();
    if (n.includes("bath")) return "bath";
    if (n.includes("bedroom")) return "laundry";
    if (n.includes("kitchen")) return "kitchen";
    if (n.includes("entertain")) return "game";
    if (n.includes("work")) return "wifi";
    if (n.includes("comfort")) return "climate";
    if (n.includes("lake")) return "lake";
    if (n.includes("patio") || n.includes("outdoor")) return "balcony";
    if (n.includes("building") || n.includes("resort")) return "pool";
    if (n.includes("parking")) return "parking";
    if (n.includes("safety")) return "shield";
    if (n.includes("stay")) return "key";
    return "star";
  }

  /* ---------------- Boot ---------------- */
  document.addEventListener("DOMContentLoaded", init);

  function init() {
    // preloader
    window.addEventListener("load", () => setTimeout(() => $("#preloader").classList.add("done"), 600));
    setTimeout(() => $("#preloader").classList.add("done"), 2600); // safety

    $("#year").textContent = new Date().getFullYear();
    $("#heroMedia").style.backgroundImage = `url('${HERO_IMG}')`;

    buildMarquee();
    renderCards();
    renderStory();
    renderComforts();
    setupNav();
    setupReveal();
    setupSmoothScroll();
    setupCursor();
    setupLightbox();
    setupResvBase();
    setupRouting();

    // open deep-linked listing
    handleHash();
  }

  /* ---------------- Marquee ---------------- */
  function buildMarquee() {
    const items = ["Lake Carolyn views", "DART rail at the door", "Chef-grade kitchens", "Resort-style pools",
      "24/7 fitness", "Game rooms & mini golf", "Fireplaces & fire pits", "Self check-in", "Pet-friendly",
      "Fiber Wi-Fi", "Sky lounges", "Free covered parking"];
    const span = (t) => `<span>${esc(t)}</span>`;
    $("#marqueeTrack").innerHTML = (items.map(span).join("") ).repeat(2);
  }

  /* ---------------- Collection cards ---------------- */
  function renderCards() {
    const wrap = $("#cards");
    wrap.innerHTML = LISTINGS.map((l) => {
      const o = l.occupancy;
      const img = `assets/img/${l.set}/${l.gallery[0]}.jpg`;
      return `
      <article class="card" data-id="${l.id}" style="--c:${l.accent}">
        <div class="card__img" style="background-image:url('${img}')"></div>
        <div class="card__veil"></div><div class="card__accent"></div>
        <div class="card__top">
          <span class="card__idx">${l.index}</span>
          <span class="card__price">from <b>${money(l.priceFrom)}</b>/night</span>
        </div>
        <div class="card__body">
          <p class="card__place" style="color:${l.accent}">${esc(l.place)}</p>
          <h3 class="card__name">${esc(l.name)}</h3>
          <p class="card__sub">${esc(l.subtitle)}</p>
          <div class="card__meta">
            <span class="chip">${icon("guests")}${o.guests} guests</span>
            <span class="chip">${icon("bed")}${o.bedrooms} bed</span>
            <span class="chip">${icon("bath")}${o.baths} bath</span>
          </div>
          <span class="card__cta">Discover this stay ${icon("arrow")}</span>
        </div>
      </article>`;
    }).join("");
    $$(".card", wrap).forEach((c) => c.addEventListener("click", () => openListing(c.dataset.id)));
  }

  /* ---------------- Story ---------------- */
  function renderStory() {
    const rows = [
      { n: "01", img: "assets/img/s296/27.jpg", cap: "Island kitchen", h: "Cook like you mean it", p: "Every residence opens onto a chef-grade kitchen — full cookware, stocked basics, a coffee station and glassware ready for the evening. Spread out across a generous island and make the meal part of the stay.", tags: ["Chef kitchens", "Coffee & Keurig", "Wine glasses", "Dishwasher"] },
      { n: "02", img: "assets/img/s144/04.jpg", cap: "Board-game night", h: "Built for the nights you stay in", p: "Mini golf, mini-hoops, board games, pool tables, arcades and big Smart TVs turn an ordinary evening into a tournament. Play is never an afterthought here — it's part of the floor plan.", tags: ["Mini golf", "Board games", "Pool table", "65–75\" TVs"] },
      { n: "03", img: "assets/img/s296/25.jpg", cap: "Dedicated workspace", h: "Work, beautifully", p: "Dedicated desks, mounted monitors and fiber Wi-Fi up to 1GB mean a workday here feels effortless. Co-working lounges and business spaces sit just downstairs when you want a change of scene.", tags: ["Fiber Wi-Fi", "27\" monitors", "Co-working", "Business center"] },
      { n: "04", img: "assets/img/s144/03.jpg", cap: "Fireside lounge", h: "Unwind by firelight", p: "Sink into the sofa beneath a star-lit ceiling, linear fireplace aglow, throws within reach. When you're ready for more, resort pools, sky lounges and 24/7 gyms are an elevator ride away.", tags: ["Fireplaces", "Sky lounges", "Resort pools", "Plush throws"] }
    ];
    $("#story").innerHTML = rows.map((r, i) => `
      <div class="story__row reveal">
        <figure class="story__media"><img src="${r.img}" alt="${esc(r.cap)}" loading="lazy"><figcaption>${esc(r.cap)}</figcaption></figure>
        <div class="story__text">
          <p class="story__num">${r.n}</p>
          <h3 class="story__h">${esc(r.h)}</h3>
          <p class="story__p">${esc(r.p)}</p>
          <div class="story__tags">${r.tags.map((t) => `<span>${esc(t)}</span>`).join("")}</div>
        </div>
      </div>`).join("");
  }

  /* ---------------- Comforts ---------------- */
  function renderComforts() {
    $("#comfortGrid").innerHTML = COLLECTION_COMFORTS.map((c, i) =>
      `<div class="ci reveal" style="--d:${(i % 6) * 0.04}s">${icon(c.icon)}<span>${esc(c.label)}</span></div>`
    ).join("");
  }

  /* ---------------- Nav / scroll states ---------------- */
  function setupNav() {
    const nav = $("#nav");
    const onScroll = () => nav.classList.toggle("solid", window.scrollY > window.innerHeight * 0.7 - 60);
    onScroll(); window.addEventListener("scroll", onScroll, { passive: true });

    const burger = $("#burger"), menu = $("#mobilemenu");
    const toggle = (open) => {
      burger.classList.toggle("open", open); menu.classList.toggle("open", open);
      burger.setAttribute("aria-expanded", open); document.body.classList.toggle("lock", open);
    };
    burger.addEventListener("click", () => toggle(!menu.classList.contains("open")));
    $$("#mobilemenu a").forEach((a) => a.addEventListener("click", () => toggle(false)));
  }

  /* ---------------- Smooth scroll ---------------- */
  function setupSmoothScroll() {
    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-scroll]");
      if (!t) return;
      const sel = t.dataset.target || t.getAttribute("href");
      if (!sel || !sel.startsWith("#")) return;
      const el = $(sel);
      if (el) { e.preventDefault(); el.scrollIntoView({ behavior: "smooth", block: "start" }); }
    });
  }

  /* ---------------- Reveal on scroll ---------------- */
  let revealObs;
  function setupReveal() {
    revealObs = new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) { en.target.classList.add("in"); revealObs.unobserve(en.target); } });
    }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
    observeReveals(document);
    // count up
    $$("[data-count]").forEach((el) => {
      const target = +el.dataset.count; let cur = 0;
      const io = new IntersectionObserver((en) => {
        if (en[0].isIntersecting) {
          io.disconnect();
          const step = () => { cur += Math.ceil(target / 28); if (cur >= target) { el.textContent = target; } else { el.textContent = cur; requestAnimationFrame(step); } };
          step();
        }
      });
      io.observe(el);
    });
  }
  function observeReveals(root) { $$(".reveal", root).forEach((el) => revealObs.observe(el)); }

  /* ---------------- Cursor glow ---------------- */
  function setupCursor() {
    if (!matchMedia("(hover:hover) and (pointer:fine)").matches) return;
    const g = $("#cursorGlow");
    window.addEventListener("pointermove", (e) => { g.style.left = e.clientX + "px"; g.style.top = e.clientY + "px"; }, { passive: true });
  }

  /* =================================================================
     LISTING DETAIL
     ================================================================= */
  let scrollMem = 0, currentId = null;
  function openListing(id, fromPop) {
    const l = LISTINGS.find((x) => x.id === id);
    if (!l) return;
    const det = $("#detail");
    const wasOpen = det.classList.contains("open");
    det.innerHTML = detailHTML(l);
    det.style.setProperty("--accent", l.accent);
    det.style.setProperty("--accent-soft", l.accentSoft);
    if (!wasOpen) scrollMem = window.scrollY;
    currentId = id;
    document.body.classList.add("lock");
    det.classList.add("open");
    det.setAttribute("aria-hidden", "false");
    det.scrollTop = 0;
    wireDetail(l, det);
    if (!fromPop) history.pushState({ stay: id }, "", "#stay-" + id);
  }
  function closeListing(fromPop) {
    const det = $("#detail");
    currentId = null;
    det.classList.remove("open"); det.setAttribute("aria-hidden", "true");
    document.body.classList.remove("lock");
    window.scrollTo(0, scrollMem);
    setTimeout(() => { if (!det.classList.contains("open")) det.innerHTML = ""; }, 500);
    if (!fromPop && location.hash.startsWith("#stay")) history.pushState({}, "", location.pathname);
  }

  function detailHTML(l) {
    const o = l.occupancy;
    const galleryImgs = l.gallery.map((g) => ({ src: `assets/img/${l.set}/${g}.jpg`, cap: (CAPTIONS[l.set] && CAPTIONS[l.set][g]) || l.name }));
    const heroSrc = galleryImgs[0].src;
    const gal = galleryImgs.map((im, i) => {
      const cls = i === 0 ? "g-lg" : (i % 7 === 3 ? "g-wide" : "");
      return `<figure class="${cls}" data-i="${i}"><img src="${im.src}" alt="${esc(im.cap)}" loading="lazy"></figure>`;
    }).join("");

    const highs = l.highlights.map((h) => `
      <div class="high"><div class="high__ic">${icon(h.icon)}</div><h4>${esc(h.title)}</h4><p>${esc(h.text)}</p></div>`).join("");

    const amenGroups = Object.entries(l.amenities).map(([k, items]) => `
      <div class="amen__group">
        <h4>${icon(groupIcon(k))}${esc(k)}</h4>
        <ul>${items.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
      </div>`).join("");
    const groupCount = Object.keys(l.amenities).length;

    const rules = HOUSE_RULES_BASE.map((r) => {
      let text = r.text;
      if (r.icon === "key") text = "Self check-in — " + (l.id === "luxury-executive-living" ? "smart lock" : "lockbox");
      return `<div class="rule">${icon(r.icon)}<span>${esc(text)}</span></div>`;
    }).join("");

    const hood = l.id === "luxury-executive-living"
      ? ["DART Galatyn Park Station", "CityLine District", "UT Dallas", "Eisemann Center", "Restaurant Row", "Spring Creek Trails"]
      : ["DART Orange Line", "Toyota Music Factory", "Water Street dining", "Mandalay Canal Walk", "Lake Carolyn trails", "Alamo Drafthouse"];

    return `
    <div class="detail__bar">
      <button class="detail__back" id="detBack">${icon("back")} All stays</button>
      <span class="detail__barname">${esc(l.name)}</span>
      <div class="detail__barcta">
        <a class="btn btn--ghost" href="${l.airbnb}" target="_blank" rel="noopener">View on Airbnb</a>
        <button class="btn btn--accent" id="detReserveTop">Reserve</button>
      </div>
    </div>

    <header class="dhero">
      <div class="dhero__img" style="background-image:url('${heroSrc}')"></div>
      <div class="dhero__scrim"></div>
      <div class="dhero__inner">
        <p class="dhero__place">${esc(l.place)}</p>
        <h1 class="dhero__name">${esc(l.name)}</h1>
        <p class="dhero__sub">${esc(l.tagline)}</p>
        <div class="dhero__meta">
          <span class="chip">${icon("guests")}${o.guests} guests</span>
          <span class="chip">${icon("door")}${o.bedrooms} bedroom</span>
          <span class="chip">${icon("bed")}${o.beds} bed</span>
          <span class="chip">${icon("bath")}${o.baths} bath</span>
        </div>
      </div>
      <button class="btn btn--solid dhero__view" id="detViewGallery">${icon("game")} View ${galleryImgs.length} photos</button>
    </header>

    <div class="dwrap">
      <div class="dmain">
        <section class="dblock">
          <p class="dblock__k">The residence</p>
          <p class="dlede">${esc(l.tagline)}</p>
          <p class="dsummary">${esc(l.summary)}</p>
        </section>

        <section class="dblock">
          <p class="dblock__k">Gallery</p>
          <div class="gallery" id="detGallery">${gal}</div>
        </section>

        <section class="dblock">
          <p class="dblock__k">What makes it special</p>
          <div class="highs">${highs}</div>
        </section>

        <section class="dblock">
          <p class="dblock__k">Every amenity</p>
          <div class="amen ${groupCount > 4 ? "collapsed" : ""}" id="detAmen">${amenGroups}</div>
          ${groupCount > 4 ? `<button class="btn btn--line amen-toggle" id="amenToggle">Show all ${groupCount} categories</button>` : ""}
        </section>

        <section class="dblock">
          <p class="dblock__k">House rules</p>
          <div class="rules">${rules}</div>
        </section>

        <section class="dblock">
          <p class="dblock__k">The neighborhood</p>
          <div class="hood">${hood.map((h) => `<span>${esc(h)}</span>`).join("")}</div>
        </section>
      </div>

      <aside>
        <div class="book" id="bookRail">
          <div class="book__price"><b>${money(l.priceFrom)}</b><span>/ night from</span></div>
          <p class="book__rate">Self check-in · <b>Pets welcome</b> · Free cancellation window</p>
          <div class="field-row">
            <div class="field"><label>Check-in</label><input type="date" id="ci"></div>
            <div class="field"><label>Check-out</label><input type="date" id="co"></div>
          </div>
          <div class="field">
            <label>Guests</label>
            <div class="stepper"><b id="gLabel">1 guest</b>
              <div class="stepper__btns"><button type="button" id="gMinus" aria-label="Fewer guests">–</button><button type="button" id="gPlus" aria-label="More guests">+</button></div>
            </div>
          </div>
          <div class="toggle"><span>${icon("paw")} Bringing a pet?</span><div class="switch" id="petSwitch" role="switch" aria-checked="false" tabindex="0"></div></div>
          <p class="book__err" id="bookErr"></p>
          <div class="book__summary" id="bookSummary"></div>
          <button class="btn btn--accent btn--block" id="reserveBtn">Request to book</button>
          <a class="book__alt" href="${l.airbnb}" target="_blank" rel="noopener">or complete your booking on Airbnb →</a>
          <p class="book__note">You won't be charged yet. We confirm availability within hours, then send a secure payment link.</p>
        </div>
      </aside>
    </div>

    <div class="mobook">
      <div><b>${money(l.priceFrom)}</b><small>per night · ${o.guests} guests max</small></div>
      <button class="btn btn--accent" id="moReserve">Reserve</button>
    </div>`;
  }

  function wireDetail(l, det) {
    $("#detBack", det).addEventListener("click", () => closeListing());
    const bar = $(".detail__bar", det);
    const dhero = $(".dhero", det);
    // bar name fade when scrolled past hero
    det.addEventListener("scroll", () => {
      bar.classList.toggle("named", det.scrollTop > dhero.offsetHeight - 80);
    });

    // gallery -> lightbox
    const imgs = l.gallery.map((g) => ({ src: `assets/img/${l.set}/${g}.jpg`, cap: (CAPTIONS[l.set] && CAPTIONS[l.set][g]) || l.name }));
    $$("#detGallery figure", det).forEach((f) => f.addEventListener("click", () => openLightbox(imgs, +f.dataset.i)));
    $("#detViewGallery", det).addEventListener("click", () => openLightbox(imgs, 0));

    // amenities toggle
    const at = $("#amenToggle", det);
    if (at) at.addEventListener("click", () => {
      const a = $("#detAmen", det); const collapsed = a.classList.toggle("collapsed");
      at.textContent = collapsed ? `Show all ${Object.keys(l.amenities).length} categories` : "Show fewer";
    });

    // booking state
    const state = { guests: 1, pets: false, max: l.occupancy.guests };
    const ci = $("#ci", det), co = $("#co", det), gLabel = $("#gLabel", det);
    const today = new Date(); const iso = (d) => d.toISOString().slice(0, 10);
    ci.min = iso(today); co.min = iso(new Date(today.getTime() + 86400000));
    const gMinus = $("#gMinus", det), gPlus = $("#gPlus", det);
    const petSwitch = $("#petSwitch", det);
    const errEl = $("#bookErr", det), sumEl = $("#bookSummary", det);

    function setGuests(n) {
      state.guests = Math.max(1, Math.min(state.max, n));
      gLabel.textContent = state.guests + (state.guests === 1 ? " guest" : " guests");
      gMinus.disabled = state.guests <= 1; gPlus.disabled = state.guests >= state.max;
      recalc();
    }
    gMinus.addEventListener("click", () => setGuests(state.guests - 1));
    gPlus.addEventListener("click", () => setGuests(state.guests + 1));
    petSwitch.addEventListener("click", () => { state.pets = !state.pets; petSwitch.classList.toggle("on", state.pets); petSwitch.setAttribute("aria-checked", state.pets); });
    petSwitch.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); petSwitch.click(); } });
    ci.addEventListener("change", () => { co.min = ci.value ? iso(new Date(new Date(ci.value).getTime() + 86400000)) : co.min; if (co.value && co.value <= ci.value) co.value = ""; recalc(); });
    co.addEventListener("change", recalc);

    function nights() {
      if (!ci.value || !co.value) return 0;
      const d = (new Date(co.value) - new Date(ci.value)) / 86400000;
      return d > 0 ? Math.round(d) : 0;
    }
    function recalc() {
      const n = nights();
      errEl.classList.remove("show");
      if (n <= 0) { sumEl.classList.remove("show"); sumEl.innerHTML = ""; return; }
      const base = n * l.priceFrom; const service = base * SERVICE_RATE; const total = base + CLEANING + service;
      sumEl.innerHTML =
        `<div class="book__line"><span>${money(l.priceFrom)} × ${n} night${n > 1 ? "s" : ""}</span><span>${money(base)}</span></div>` +
        `<div class="book__line"><span>Cleaning fee</span><span>${money(CLEANING)}</span></div>` +
        `<div class="book__line"><span>Service</span><span>${money(service)}</span></div>` +
        `<div class="book__line total"><span>Total</span><span>${money(total)}</span></div>`;
      sumEl.classList.add("show");
    }

    function attemptReserve() {
      const n = nights();
      if (n <= 0) {
        errEl.textContent = "Please choose your check-in and check-out dates.";
        errEl.classList.add("show");
        (!ci.value ? ci : co).focus();
        return;
      }
      const base = n * l.priceFrom; const service = base * SERVICE_RATE; const total = base + CLEANING + service;
      openReservation(l, { ci: ci.value, co: co.value, nights: n, guests: state.guests, pets: state.pets, total });
    }
    $("#reserveBtn", det).addEventListener("click", attemptReserve);
    $("#moReserve", det).addEventListener("click", attemptReserve);
    $("#detReserveTop", det).addEventListener("click", () => { det.querySelector(".dwrap").scrollIntoView; attemptReserve(); });

    setGuests(1);
    observeReveals(det);
  }

  /* =================================================================
     LIGHTBOX
     ================================================================= */
  let lbImgs = [], lbIdx = 0;
  function setupLightbox() {
    $("#lbClose").addEventListener("click", closeLightbox);
    $("#lbPrev").addEventListener("click", () => stepLightbox(-1));
    $("#lbNext").addEventListener("click", () => stepLightbox(1));
    $("#lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox") closeLightbox(); });
    document.addEventListener("keydown", (e) => {
      if (!$("#lightbox").classList.contains("open")) return;
      if (e.key === "Escape") closeLightbox();
      if (e.key === "ArrowLeft") stepLightbox(-1);
      if (e.key === "ArrowRight") stepLightbox(1);
    });
  }
  function openLightbox(imgs, i) { lbImgs = imgs; lbIdx = i; paintLightbox(); $("#lightbox").classList.add("open"); $("#lightbox").setAttribute("aria-hidden", "false"); document.body.classList.add("lock"); }
  function closeLightbox() { $("#lightbox").classList.remove("open"); $("#lightbox").setAttribute("aria-hidden", "true"); if (!$("#detail").classList.contains("open")) document.body.classList.remove("lock"); }
  function stepLightbox(d) { lbIdx = (lbIdx + d + lbImgs.length) % lbImgs.length; paintLightbox(); }
  function paintLightbox() {
    const im = lbImgs[lbIdx];
    $("#lbImg").src = im.src; $("#lbImg").alt = im.cap;
    $("#lbCap").textContent = im.cap;
    $("#lbCount").textContent = (lbIdx + 1) + " / " + lbImgs.length;
  }

  /* =================================================================
     RESERVATION MODAL
     ================================================================= */
  function setupResvBase() {
    $("#resv").addEventListener("click", (e) => { if (e.target.id === "resv") closeReservation(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#resv").classList.contains("open")) closeReservation(); });
  }
  function fmtDate(s) { try { return new Date(s + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); } catch (e) { return s; } }

  function openReservation(l, d) {
    const resv = $("#resv");
    resv.style.setProperty("--accent", l.accent);
    resv.style.setProperty("--accent-soft", l.accentSoft);
    resv.innerHTML = `
      <div class="resv__card">
        <div class="resv__head">
          <button class="resv__x" id="resvX" aria-label="Close">&times;</button>
          <p class="dblock__k" style="margin:0 0 .2rem">Request to book</p>
          <h3>${esc(l.name)}</h3>
          <p>${esc(l.place)}</p>
        </div>
        <div class="resv__body" id="resvBody">
          <div class="resv__summary">
            <div class="book__line"><span>Dates</span><span>${fmtDate(d.ci)} → ${fmtDate(d.co)}</span></div>
            <div class="book__line"><span>Guests</span><span>${d.guests} · ${d.pets ? "with pet" : "no pets"}</span></div>
            <div class="book__line total"><span>${d.nights} night${d.nights > 1 ? "s" : ""} · est. total</span><span>${money(d.total)}</span></div>
          </div>
          <form id="resvForm" novalidate>
            <div class="resv__grid">
              <div class="field"><label>First name</label><input required name="first" autocomplete="given-name"></div>
              <div class="field"><label>Last name</label><input required name="last" autocomplete="family-name"></div>
              <div class="field resv__full"><label>Email</label><input required type="email" name="email" autocomplete="email"></div>
              <div class="field resv__full"><label>Anything we should know? (optional)</label><input name="msg" placeholder="Arrival time, occasion, questions…"></div>
            </div>
            <p class="book__err" id="resvErr"></p>
            <button class="btn btn--accent btn--block" type="submit" style="margin-top:.6rem">Send booking request</button>
            <a class="book__alt" href="${l.airbnb}" target="_blank" rel="noopener">Prefer Airbnb? Complete it there →</a>
          </form>
        </div>
      </div>`;
    resv.classList.add("open"); resv.setAttribute("aria-hidden", "false"); document.body.classList.add("lock");
    $("#resvX").addEventListener("click", closeReservation);
    $("#resvForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const f = e.target; const err = $("#resvErr");
      const first = f.first.value.trim(), last = f.last.value.trim(), email = f.email.value.trim();
      if (!first || !last) { err.textContent = "Please add your name."; err.classList.add("show"); return; }
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) { err.textContent = "Please enter a valid email."; err.classList.add("show"); return; }
      err.classList.remove("show");
      const subject = `Booking request · ${l.name}`;
      const body = `Hi Lúmen Stays,\n\nI'd like to request a booking:\n\nStay: ${l.name} (${l.place})\nDates: ${fmtDate(d.ci)} to ${fmtDate(d.co)} (${d.nights} nights)\nGuests: ${d.guests}${d.pets ? " + pet" : ""}\nEstimated total: ${money(d.total)}\n\nName: ${first} ${last}\nEmail: ${email}\n${f.msg.value.trim() ? "Notes: " + f.msg.value.trim() + "\n" : ""}\nListing: ${l.airbnb}\n\nThank you!`;
      const mailto = `mailto:stays@lumen.house?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
      $("#resvBody").innerHTML = `
        <div class="resv__success">
          <div class="resv__check">${icon("check")}</div>
          <h3>Request received</h3>
          <p>Thanks, ${esc(first)}. We've noted your dates for <b>${esc(l.name)}</b> and will confirm availability shortly with a secure payment link.</p>
          <div class="resv__summary" style="text-align:left">
            <div class="book__line"><span>Dates</span><span>${fmtDate(d.ci)} → ${fmtDate(d.co)}</span></div>
            <div class="book__line"><span>Guests</span><span>${d.guests}${d.pets ? " · with pet" : ""}</span></div>
            <div class="book__line total"><span>Estimated total</span><span>${money(d.total)}</span></div>
          </div>
          <a class="btn btn--accent btn--block" href="${mailto}" style="margin-top:1rem">Email my request to confirm</a>
          <a class="btn btn--line btn--block" href="${l.airbnb}" target="_blank" rel="noopener" style="margin-top:.6rem">Book instantly on Airbnb</a>
        </div>`;
      toast("Booking request prepared for " + l.name);
    });
  }
  function closeReservation() {
    const resv = $("#resv");
    resv.classList.remove("open"); resv.setAttribute("aria-hidden", "true");
    if (!$("#detail").classList.contains("open")) document.body.classList.remove("lock");
    setTimeout(() => { if (!resv.classList.contains("open")) resv.innerHTML = ""; }, 450);
  }

  /* ---------------- Toast ---------------- */
  let toastT;
  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 3600);
  }

  /* ---------------- Routing (back button) ---------------- */
  function setupRouting() {
    window.addEventListener("popstate", () => {
      if ($("#resv").classList.contains("open")) { closeReservation(); return; }
      if ($("#lightbox").classList.contains("open")) { closeLightbox(); return; }
      handleHash(true);
    });
    window.addEventListener("hashchange", () => handleHash(true));
  }
  function handleHash(fromPop) {
    const m = location.hash.match(/^#stay-(.+)$/);
    if (m) {
      const id = m[1];
      if (LISTINGS.find((x) => x.id === id)) { if (id !== currentId) openListing(id, true); return; }
    }
    if ($("#detail").classList.contains("open")) closeListing(true);
  }

})();
