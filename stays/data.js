/* ============================================================================
   Lúmen Stays — listing data
   Source of truth: the host's five real Airbnb listings (Las Colinas & Richardson).
   Photography: the host's own edited property photo sets (Google Drive),
   stored under assets/img/<set>/ and referenced per listing below.
   All copy has been cleaned and rewritten from the listing details.
   ========================================================================== */

const CAPTIONS = {
  s296: {
    "01": "Chef-inspired kitchen with stainless appliances",
    "02": "Spa-style bath with a deep soaking tub",
    "03": "Sunlit dining nook by the window",
    "04": "Designer living room with plush seating",
    "05": "Open-concept living and kitchen",
    "06": "Restful bedroom with a tufted headboard",
    "07": "Bath storage stocked with fresh towels",
    "08": "Bedroom anchored by a 65-inch Smart TV",
    "09": "Bedroom with a wall-mounted Smart TV",
    "10": "Plush queen bedroom",
    "11": "Accent bedroom with layered linens",
    "12": "Vanity with a full mirror",
    "13": "Entry framed by a round mirror",
    "14": "Light-filled hallway",
    "15": "Curated kitchen styling",
    "16": "Bedroom with a mounted Smart TV",
    "17": "Organized walk-in closet",
    "18": "Wood-floored hallway",
    "19": "Living plant wall at the entry",
    "20": "In-suite putting green",
    "21": "Living room with a dedicated workspace",
    "22": "Living lounge with a Smart TV",
    "23": "Wine and charcuterie corner",
    "24": "Bright island kitchen",
    "25": "Dedicated workspace lounge",
    "26": "Dining table set for shared meals",
    "27": "Coffee bar beside a vanity mirror",
    "28": "Granite kitchen counter details",
    "29": "Cozy corner with warm mood lighting",
    "30": "Cozy bedroom retreat",
    "31": "Vanity mirror and granite bath",
    "32": "Granite bath with a vanity mirror"
  },
  s397: {
    "01": "Living room with an L-shaped sectional",
    "02": "Full bath with soaking tub and vanity",
    "03": "Kitchen and living with a mounted TV",
    "04": "Hallway with a round mirror",
    "05": "Kitchen with a mini basketball hoop",
    "06": "Candlelit bath details",
    "07": "Chef's kitchen with herringbone backsplash",
    "08": "Living lounge with a Smart TV"
  },
  s144: {
    "01": "Island kitchen with gold accents",
    "02": "Stocked coffee bar with layered mugs",
    "03": "Star-lit lounge with a linear fireplace",
    "04": "Board-game night by the fire",
    "05": "Dark-wood kitchen details"
  }
};

const HOUSE_RULES_BASE = [
  { icon: "key", text: "Self check-in — arrive on your schedule" },
  { icon: "clock", text: "Check-in after 3:00 PM · Check-out by 11:00 AM" },
  { icon: "paw", text: "Pets welcome — just let us know in advance" },
  { icon: "moon", text: "Quiet hours 10:00 PM – 8:00 AM" },
  { icon: "no-smoke", text: "No smoking anywhere on the property" },
  { icon: "calendar", text: "Long-term stays (28+ nights) available" }
];

const LISTINGS = [
  /* ----------------------------------------------------------------- 1 */
  {
    id: "cozy-designer-suite",
    index: "01",
    name: "Cozy Designer Suite",
    subtitle: "Gym & Pool · DART Access",
    place: "Las Colinas · Irving, Texas",
    set: "s296",
    accent: "#c79a52",
    accentSoft: "#f3e6cd",
    priceFrom: 129,
    airbnb: "https://www.airbnb.com/rooms/1656911479326581139",
    occupancy: { guests: 3, bedrooms: 1, beds: 1, baths: 1 },
    tagline: "A modern, elegant Las Colinas getaway built for slow mornings and easy nights.",
    summary:
      "Ten-foot ceilings, hardwood-style floors and a chef-inspired kitchen set the tone the moment you walk in. Cook at the oversized prep island, sink into premium plush blankets in front of the 65-inch Smart TV, and keep the night going with board games, a coffee corner and an in-suite mini-hoop. Steps from Lake Carolyn trails, Toyota Music Factory, Water Street dining and the DART rail, it's resort-style living wrapped in cozy luxury.",
    highlights: [
      { icon: "ceiling", title: "10-foot ceilings", text: "Airy, light-filled rooms with hardwood-style flooring throughout." },
      { icon: "chef", title: "Chef-inspired kitchen", text: "A giant prep island, full cookware and a stocked coffee corner." },
      { icon: "bath", title: "Deep soaking tub", text: "A spa-style bath with plush towels and thoughtful toiletries." },
      { icon: "game", title: "Built for play", text: "Board games, an in-suite mini-hoop and a 65-inch Smart TV." },
      { icon: "train", title: "DART at the door", text: "Rail access to the Music Factory, Water Street and the canal walk." },
      { icon: "pool", title: "Resort amenities", text: "Resort-style pool, 24/7 fitness center, The Cave & putting green." }
    ],
    gallery: ["04","08","24","20","21","06","02","22","01","19","23","17","03","27","11","12","05","31","14","29"],
    amenities: {
      "Bath": ["Bathtub","Deep soaking tub","Hair dryer","Bidet","Hot water","Shower gel","Body soap","Conditioner","Cleaning products","Plush towels","Soap","Toilet paper"],
      "Bedroom & laundry": ["Washer","Dryer","Drying rack for clothing","Iron & pressing","Bed linens","Extra pillows & blankets","Premium plush blankets","Room-darkening shades","Hangers","Clothing storage","Towels & bed sheets"],
      "Kitchen & dining": ["Full kitchen","Refrigerator","Mini fridge","Freezer","Microwave","Stove & oven","Dishwasher","Trash compactor","Coffee maker","Toaster","Cooking basics","Pots & pans","Oil, salt & pepper","Dishes & silverware","Bowls, plates & cups","Chopsticks","Wine glasses","Baking sheet","Dining table","Coffee"],
      "Entertainment & play": ["65\" Smart TV","Ethernet connection","Board games","Pool table","Mini-hoop","Putting green access","Children's playroom","Theme room"],
      "Workspace & connectivity": ["Fast Wi-Fi","Dedicated workspace"],
      "Comfort & climate": ["Central air conditioning","Heating","Ceiling fan","Climate-controlled access"],
      "Outdoor & lake": ["Lake access","Patio or balcony","Outdoor furniture","Outdoor dining area","Shared BBQ grill","Sun loungers","Private entrance"],
      "Building & resort": ["Resort-style pool","24/7 fitness center","The Cave & putting green","Elevator","Indoor corridors","Secure multi-level parking","Single-level home — no stairs"],
      "Parking": ["Free covered parking on premises","Free street parking"],
      "Safety": ["Smoke alarm","Carbon monoxide alarm","Fire extinguisher","First aid kit"],
      "The stay": ["Self check-in with lockbox","Pets allowed","Long-term stays allowed","Cleaning available during stay","Stocked soaps, paper goods & supplies"]
    }
  },

  /* ----------------------------------------------------------------- 2 */
  {
    id: "designer-game-suite",
    index: "02",
    name: "Designer Game Suite",
    subtitle: "Resort Amenities · DART Rail",
    place: "Las Colinas · Irving, Texas",
    set: "s397",
    accent: "#9b6f8e",
    accentSoft: "#efe0ea",
    priceFrom: 134,
    airbnb: "https://www.airbnb.com/rooms/1691507762565991335",
    occupancy: { guests: 3, bedrooms: 1, beds: 1, baths: 1 },
    tagline: "Marble finishes, soft candlelight and a playful game-night energy.",
    summary:
      "A designer one-bedroom in Las Colinas with marble accents, mirrors throughout and a warm, candlelit ambience. Brew from the stocked coffee bar, cook in the chef-grade kitchen, then settle in with the 65-inch Smart TV, a second bedroom TV, board games, mini golf and a mini basketball hoop. Step onto the balcony, or walk to the DART rail, Toyota Music Factory, Lake Carolyn, Alamo Drafthouse and Water Street just outside.",
    highlights: [
      { icon: "candle", title: "Candlelit ambience", text: "Marble finishes, mirrors throughout and a warm, layered glow." },
      { icon: "coffee", title: "Stocked coffee bar", text: "Keurig machine, creamers, sweeteners, filters and mugs." },
      { icon: "game", title: "Game-night ready", text: "Mini golf, a mini hoop, board games and dual Smart TVs." },
      { icon: "balcony", title: "Private balcony", text: "Step out to chairs and skyline air, steps from the rail." },
      { icon: "shield", title: "Secure & gated", text: "Gated community, controlled access and EV charging." },
      { icon: "pool", title: "Resort living", text: "Resort-style pool, 24/7 gym, sky lounge and putting green." }
    ],
    gallery: ["01","05","04","06","03","07","02","08"],
    amenities: {
      "Bath": ["Bathtub","Hair dryer","Shampoo","Conditioner","Dove body soap","Hot water","Shower gel","Cleaning products","Fresh bath & hand towels","Soap","Toilet paper","Full first-aid kit"],
      "Bedroom & laundry": ["In-unit washer & dryer","Drying rack for clothing","Iron & pressing","Bed linens","Extra pillows, blankets & comforters","Layered blankets","Room-darkening shades","Hangers","Clothing storage","Laundry detergent & dryer sheets","Full-length & vanity mirrors"],
      "Kitchen & dining": ["Chef-grade kitchen","Refrigerator","Mini fridge","Freezer","Electric stove & oven","Dishwasher","Trash compactor","Keurig coffee machine","Multiple creamers & sweeteners","Filters & mugs","Toaster","Cooking basics","Pots & pans","Oil, salt & pepper","Dishes & silverware","Bowls, plates & cups","Chopsticks","Wine glasses","Baking sheet","Dining table","Coffee"],
      "Entertainment & play": ["65\" Smart TV","Bedroom TV","Ethernet connection","Mini golf","Mini hoop","Board games & storage","Pool table","Children's playroom","Theme room","Candles"],
      "Workspace & connectivity": ["Fast Wi-Fi for remote work","Dedicated workspace"],
      "Comfort & climate": ["Central air conditioning","Central heating","Ceiling fan"],
      "Outdoor & lake": ["Lake access","Private patio or balcony","Outdoor furniture","Outdoor dining area","BBQ grill","Sun loungers","Picnic area & grills","Private entrance"],
      "Building & resort": ["Resort-style pool","24/7 fitness center","Yoga studio","Sky lounge with skyline views","On-site putting green","Clubhouse & game room","Outdoor lounges","Elevator"],
      "Parking": ["Free covered garage parking","Free parking on premises","Free street parking","EV charging stations"],
      "Safety": ["Exterior security cameras in shared areas (none indoors)","Smoke alarm","Carbon monoxide alarm","Fire extinguisher","First aid kit","Gated, controlled access"],
      "The stay": ["Self check-in with lockbox","Pets allowed","Long-term stays allowed","Housekeeping available (extra cost)"]
    }
  },

  /* ----------------------------------------------------------------- 3 */
  {
    id: "luxury-lake-view-suite",
    index: "03",
    name: "Luxury Lake View Suite",
    subtitle: "Gym & Pool · Lake Carolyn",
    place: "Las Colinas · Irving, Texas",
    set: "s296",
    accent: "#3f8f9b",
    accentSoft: "#d6ecef",
    priceFrom: 149,
    airbnb: "https://www.airbnb.com/rooms/1579365691674889946",
    occupancy: { guests: 3, bedrooms: 1, beds: 1, baths: 1 },
    tagline: "An ultra-luxury, 600 sq ft lakeside escape with tranquil Lake Carolyn views.",
    summary:
      "Cozy yet refined, this 600-square-foot Las Colinas suite looks out over tranquil Lake Carolyn from a private balcony. Designer interiors frame a plush queen bed, a 65-inch Smart TV, a wine cooler, a game bin and a UFC-style punching bag. Work from the mounted 27-inch monitor on 500 Mbps fiber, unwind with candles and throws by the full-body vanity mirror, then take in the waterfront pool, two-story gym, game room and conference spaces just downstairs.",
    highlights: [
      { icon: "lake", title: "Tranquil lake views", text: "Wake to Lake Carolyn from your private balcony." },
      { icon: "wine", title: "Wine cooler & game bin", text: "Chill a bottle, break out the games, settle in." },
      { icon: "fitness", title: "UFC-style punching bag", text: "An in-suite workout, plus a 24/7 two-story gym below." },
      { icon: "monitor", title: "27\" monitor workspace", text: "500 Mbps fiber Wi-Fi for effortless remote work." },
      { icon: "mirror", title: "Full-body vanity mirror", text: "Candles, plush throws and hotel-grade linens." },
      { icon: "pool", title: "Waterfront resort", text: "Gorgeous waterfront pool, game room and conference spaces." }
    ],
    gallery: ["21","30","25","27","31","11","24","09","05","23","13","20","17","02","32","06","26","16","29","04"],
    amenities: {
      "Bath": ["Bathtub","Hair dryer","Conditioner","Body soap","Hot water","Shower gel","Cleaning products","Fully stocked bathroom","Essential toiletries","Extra towels","Soap","Toilet paper","First-aid kit"],
      "Bedroom & laundry": ["Full-size washer & dryer","Detergent","Iron & ironing board","Bed linens","Extra pillows, blankets & comforters","Plush hotel-grade linens","Throws","Room-darkening shades","Hangers","Huge walk-in closet","Full-body vanity mirror","Candles"],
      "Kitchen & dining": ["Full kitchen","Refrigerator","Mini fridge","Freezer","Stove & oven","Dishwasher","Trash compactor","Wine cooler","Keurig coffee station with complimentary pods","Coffee maker","Toaster","Cooking basics","Pots & pans","Oil, salt & pepper","Dishes & silverware","Bowls, plates & cups","Chopsticks","Baking sheet","Dining table","Coffee"],
      "Entertainment & play": ["65\" Smart TV","Game bin","Board games","Pool table","UFC-style punching bag","High chair","Children's playroom","Theme room"],
      "Workspace & connectivity": ["500 Mbps fiber internet","Dedicated workspace","Mounted 27\" monitor","Desk"],
      "Comfort & climate": ["Central air conditioning","Heating","Ceiling fan"],
      "Outdoor & lake": ["Lake view & scenic views","Waterfront","Lake access","Private balcony","Outdoor kitchen","BBQ grill & utensils","Charcoal & skewers","Sun loungers","Private entrance"],
      "Building & resort": ["Gorgeous waterfront pool","24/7 two-story gym","Game room","Conference spaces","Elevator","Single-level home — no stairs","Convenience store nearby"],
      "Parking": ["Secure garage parking","Free parking on premises","Free street parking"],
      "Safety": ["Smoke alarm","Carbon monoxide alarm","Fire extinguisher","First aid kit"],
      "The stay": ["Self check-in with lockbox","Pets allowed","Long-term stays allowed","24-hour housekeeping available (extra cost)"]
    }
  },

  /* ----------------------------------------------------------------- 4 */
  {
    id: "luxury-executive-living",
    index: "04",
    name: "Luxury Executive Living",
    subtitle: "DART Rail · Workspace",
    place: "Richardson, Texas",
    set: "s144",
    accent: "#3d7a5c",
    accentSoft: "#d8ecdf",
    priceFrom: 159,
    airbnb: "https://www.airbnb.com/rooms/1622247533469388804",
    occupancy: { guests: 5, bedrooms: 1, beds: 1, baths: 1 },
    tagline: "A polished executive suite with soaring ceilings and a fireplace-lit lounge.",
    summary:
      "Soaring high ceilings and floor-to-ceiling light open onto an oversized private patio. Inside, a 75-inch Smart TV and a dedicated desk with a 27-inch monitor sit beside a stocked coffee and tea station, plush towels and throw blankets — with an air mattress that hosts up to five. Below, a resort pool, 24/7 gym, sky lounge, arcade room, pool table, ping pong, fire pit and co-working spaces round out a stay built for work and play in equal measure. Galatyn Park DART is moments away.",
    highlights: [
      { icon: "ceiling", title: "Soaring ceilings", text: "Floor-to-ceiling light and an oversized private patio." },
      { icon: "tv", title: "75\" Smart TV", text: "A cinematic lounge with a fireplace-lit ambience." },
      { icon: "monitor", title: "Executive workspace", text: "A dedicated desk with a 27-inch monitor and fast Wi-Fi." },
      { icon: "guests", title: "Sleeps five", text: "An air mattress and layered bedding for up to 5 guests." },
      { icon: "arcade", title: "Arcade & billiards", text: "Arcade games, pool table, ping pong and a fire pit." },
      { icon: "fire", title: "Indoor fireplace", text: "Plus a courtyard fire pit and co-working lounges." }
    ],
    gallery: ["03","01","04","02","05"],
    amenities: {
      "Bath": ["Bathtub","Hair dryer","Shampoo","Conditioner","Body soap / body wash","Hot water","Shower gel","Cleaning products","Plush towels","Soap","Toilet paper","Full first aid kit"],
      "Bedroom & laundry": ["In-unit washer & dryer","Detergent","Iron & pressing","Bed linens","Extra pillows & blankets","Cozy throw blankets","Extra sheets & blankets","Air mattress for up to 5 guests","Room-darkening shades","Hangers","Clothing storage"],
      "Kitchen & dining": ["Full kitchen","Refrigerator","Freezer","Stove & oven","Dishwasher","Trash compactor","Hot water kettle","Coffee maker","Stocked coffee & tea station","Creamers & sweeteners","Toaster","Cooking basics","Pots & pans","Oil, salt & pepper","Dishes & silverware","Bowls, plates & cups","Chopsticks","Wine glasses","Baking sheet","Dining table","Coffee","Dish soap"],
      "Entertainment & play": ["75\" Smart TV","Indoor fireplace","Ping pong table","Pool table","Arcade games","Board games","High chair","Theme room"],
      "Workspace & connectivity": ["Fast Wi-Fi","Ethernet connection","Dedicated workspace","27\" monitor","Co-working spaces"],
      "Comfort & climate": ["Central air conditioning","Heating","Ceiling fan"],
      "Outdoor & patio": ["Oversized private patio / balcony","Fire pit","Outdoor furniture","Outdoor dining area","Shared BBQ grill & utensils","Charcoal & skewers","Sun loungers","Courtyard kitchen","Private pocket park & game lawn","Private entrance"],
      "Building & resort": ["Resort-style pool with tanning ledge & cabanas","24/7 fitness center","Spin room","Sky lounge with panoramic views","Arcade room","Billiards lounge","Multiple clubhouse lounges","Outdoor firepit","Elevator"],
      "Parking": ["Safe covered parking","Free parking on premises","Free street parking","EV charger (Level 1)"],
      "Safety": ["Smoke alarm","Carbon monoxide alarm","Fire extinguisher","First aid kit"],
      "The stay": ["Self check-in with smart lock","Pets allowed","Long-term stays allowed","Housekeeping available (extra cost)","All essential supplies stocked"]
    }
  },

  /* ----------------------------------------------------------------- 5 */
  {
    id: "stunning-lake-views",
    index: "05",
    name: "Stunning Lake Views",
    subtitle: "DART Train · Gym & Pool",
    place: "Las Colinas · Irving, Texas",
    set: "s397",
    accent: "#2f6db0",
    accentSoft: "#d6e4f4",
    priceFrom: 145,
    airbnb: "https://www.airbnb.com/rooms/862596563097784023",
    occupancy: { guests: 3, bedrooms: 1, beds: 1, baths: 1 },
    tagline: "Breathtaking Lake Carolyn views and a luxe living room built to linger in.",
    summary:
      "A modern designer one-bedroom with breathtaking Lake Carolyn views and a private balcony. The luxe living room centers on a massive L-shaped couch and a 65-inch Smart TV, with a 50-inch Smart TV in the bedroom above an ultra-plush queen bed dressed in premium linens. A game bin, full-body vanity mirror, desk, candles and throws make it home, while 1GB fiber Wi-Fi keeps you connected. DART Orange Line, secure garage parking, the waterfront pool, a two-story gym, a game room and conference rooms are all within reach.",
    highlights: [
      { icon: "lake", title: "Breathtaking lake views", text: "Lake Carolyn framed from your private balcony." },
      { icon: "sofa", title: "Massive L-shaped couch", text: "A luxe living room built to stretch out and linger." },
      { icon: "tv", title: "Dual Smart TVs", text: "65-inch in the living room, 50-inch in the bedroom." },
      { icon: "wifi", title: "1GB fiber Wi-Fi", text: "A dedicated workspace with an extra monitor setup." },
      { icon: "mirror", title: "Game bin & vanity mirror", text: "Candles, throws and a full-body stand-up mirror." },
      { icon: "pool", title: "Waterfront resort", text: "Waterfront pool, two-story gym, game room & business center." }
    ],
    gallery: ["08","01","03","07","05","02","04","06"],
    amenities: {
      "Bath": ["Bathtub","Hair dryer","Shampoo","Conditioner","Body soap","Hot water","Shower gel","Cleaning products","Plenty of bath & hand towels","Stocked bathroom","Essential toiletries","Soap","Toilet paper","First aid kit"],
      "Bedroom & laundry": ["Full-size in-unit washer & dryer","Detergent","Iron & ironing board","Drying rack for clothing","Bed linens","Premium linens","Extra pillows & blankets","Room-darkening shades","Hangers","Huge walk-in closet","Full-body vanity mirror","Candles","Throws"],
      "Kitchen & dining": ["Full kitchen","Refrigerator","Freezer","Stove & oven","Dishwasher","Trash compactor","Keurig coffee station with complimentary pods","Coffee maker","Toaster","Cooking basics","Pots & pans","Oil, salt & pepper","Dishes & silverware","Bowls, plates & cups","Chopsticks","Baking sheet","Dining table","Coffee"],
      "Entertainment & play": ["65\" living room Smart TV","50\" bedroom Smart TV","Game bin","Board games","Pool table","High chair"],
      "Workspace & connectivity": ["1GB fiber internet","Dedicated workspace","Extra monitor setup","Desk"],
      "Comfort & climate": ["Central air conditioning","Heating","Ceiling fan"],
      "Outdoor & lake": ["Lake view & scenic views","Waterfront","Lake access","Private balcony","Outdoor furniture","Outdoor dining area","Shared outdoor kitchen","BBQ grill & utensils","Charcoal & skewers"],
      "Building & resort": ["Waterfront pool","24/7 state-of-the-art two-story gym","Game room","Business center","Conference rooms","Elevator"],
      "Parking": ["Complimentary secure garage parking","Free parking on premises","Free street parking"],
      "Safety": ["Smoke alarm","Carbon monoxide alarm","Fire extinguisher","First aid kit"],
      "The stay": ["Self check-in with lockbox","Pets allowed","Long-term stays allowed","24-hour housekeeping available (extra cost)"]
    }
  }
];

/* Collection-wide comfort details surfaced on the landing experience */
const COLLECTION_COMFORTS = [
  { icon: "climate", label: "Central A/C & heating" },
  { icon: "fan", label: "Ceiling fans" },
  { icon: "iron", label: "Irons & boards" },
  { icon: "mirror", label: "Full-length & vanity mirrors" },
  { icon: "towel", label: "Fresh towels & extra linens" },
  { icon: "blanket", label: "Plush throws & extra blankets" },
  { icon: "shade", label: "Room-darkening shades" },
  { icon: "bath", label: "Full baths & hair dryers" },
  { icon: "laundry", label: "In-unit laundry" },
  { icon: "kitchen", label: "Stocked chef kitchens" },
  { icon: "coffee", label: "Coffee & Keurig stations" },
  { icon: "wine", label: "Glassware & wine glasses" },
  { icon: "wifi", label: "Fast fiber Wi-Fi" },
  { icon: "desk", label: "Dedicated workspaces" },
  { icon: "game", label: "Games, pool & mini golf" },
  { icon: "fire", label: "Fireplaces & fire pits" },
  { icon: "pool", label: "Resort pools & gyms" },
  { icon: "lounge", label: "Sky lounges & clubs" },
  { icon: "parking", label: "Free covered parking" },
  { icon: "paw", label: "Pet-friendly stays" }
];

if (typeof module !== "undefined") { module.exports = { LISTINGS, CAPTIONS, HOUSE_RULES_BASE, COLLECTION_COMFORTS }; }
