"use strict";
/*
 * Client-side port of the local export tool's JSON/HTML/ORF/Paprika/Norish builders, plus a
 * small fetch layer that pulls manifest.json and the recipe files from alongside this page
 * (same folder on the same site -- no token, no API). Kept as one plain script (no build
 * step) so this whole thing is just static files GitHub Pages can serve as-is. Mirrors the
 * local Python tool's logic closely and deliberately.
 */

const CONCURRENCY = 6;

// --- id reference tables (mirrors the local Python tool's id reference module) --

const MENU_TYPES = {
  "-1": "All Menus", "1": "Classic", "2": "Vegetarian", "3": "Flexitarian",
  "4": "Pescatarian", "5": "Paleo", "6": "Vegan", "7": "Low Carb", "8": "Keto",
};
const RESTRICTIONS = {
  "1": "Gluten", "2": "Dairy", "3": "Fish", "4": "Shellfish", "5": "Peanut",
  "6": "Tree Nut", "9": "Soy", "10": "Nightshade", "11": "Egg", "12": "Sesame",
  "13": "Mustard", "14": "Sulfite",
};
function decodeTypes(ids) { return (ids || []).map(i => MENU_TYPES[String(i)] || `unknown(${i})`); }
function decodeRestrictions(ids) { return (ids || []).map(i => RESTRICTIONS[String(i)] || `unknown(${i})`); }

// "-1" (All Menus) isn't a per-recipe tag -- every recipe would match it, so it's
// meaningless as a filter checkbox and is left out here.
function renderCheckboxes(containerEl, table, groupName) {
  containerEl.innerHTML = Object.entries(table)
    .filter(([id]) => id !== "-1")
    .map(([id, label]) => `<label class="opt"><input type="checkbox" name="${groupName}" value="${id}"> ${escapeHtml(label)}</label>`)
    .join("");
}
renderCheckboxes(document.getElementById("restriction-checkboxes"), RESTRICTIONS, "restriction");
// "Menu" is a single-select radio group hardcoded in index.html (matching --menu's fixed choice
// list, Classic included), not rendered from MENU_TYPES -- see selectMenuWinners() for why.

// --- quantity parsing (mirrors the local Python tool's parse_quantity) ----------

const FRACTION_MAP = {
  "¼": 0.25, "½": 0.5, "¾": 0.75,
  "⅓": 1 / 3, "⅔": 2 / 3,
  "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8,
  "⅙": 1 / 6, "⅚": 5 / 6,
  "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
};
const FRACTION_ITEMS = Object.entries(FRACTION_MAP).sort((a, b) => b[1] - a[1]);

function parseQuantity(quantity) {
  const text = (quantity || "").trim();
  if (!text) return [1, "(to taste / as needed)"];
  for (const [ch, value] of FRACTION_ITEMS) {
    if (text.startsWith(ch)) return [value, text.slice(ch.length).trim()];
    const wholeMatch = text.match(new RegExp(`^(\\d+)\\s*${ch}(.*)$`, "s"));
    if (wholeMatch) return [parseInt(wholeMatch[1], 10) + value, wholeMatch[2].trim()];
  }
  const match = text.match(/^\s*(\d+(?:\.\d+)?)\s*(.*)$/s);
  if (match) return [parseFloat(match[1]), match[2].trim()];
  return [1, text];
}

function ingredientLine(ing) {
  const quantity = (ing.quantity || "").trim();
  return quantity ? `${quantity} ${ing.name}`.trim() : ing.name;
}

function stepTextAndCallouts(step) {
  const primary = (step.primary_message || "").trim();
  const secondary = (step.secondary_message || "").trim();
  const callouts = secondary.split("\n").map(l => l.trim()).filter(Boolean);
  return [primary, callouts];
}

// --- nutrition + amino acids (mirrors the local Python tool) ---------------------

const NUTRITION_DISPLAY = [
  ["energy", "Calories", "", 0], ["fat", "Fat", "g", 1], ["saturated", "Saturated Fat", "g", 1],
  ["carbs", "Carbohydrates", "g", 1], ["fiber", "Fiber", "g", 1], ["sugars", "Sugars", "g", 1],
  ["protein", "Protein", "g", 1], ["sodium", "Sodium", "mg", 1], ["cholesterol", "Cholesterol", "mg", 1],
];
// Python's f"{value:.{decimals}f}" rounds the EXACT binary value, ties (only possible when
// value*2^(decimals+1) is an odd integer, e.g. 46.25 at 1 decimal) to even. JS toFixed also rounds
// the exact binary value but sends ties up (46.25 -> "46.3" vs Python's "46.2"), so only ties need
// special handling to keep every format identical to the local Python tool's output.
function pyToFixed(value, decimals) {
  const doubled = value * 2 ** (decimals + 1);
  if (Number.isInteger(doubled) && Math.abs(doubled % 2) === 1) {
    const scaled = value * 10 ** decimals;
    const floor = Math.floor(scaled);
    return ((floor % 2 === 0 ? floor : floor + 1) / 10 ** decimals).toFixed(decimals);
  }
  return value.toFixed(decimals);
}

function nutritionLines(nutrition) {
  const lines = [];
  for (const [key, label, unit, decimals] of NUTRITION_DISPLAY) {
    const value = nutrition[key];
    if (value === undefined || value === null) continue;
    lines.push(`${label}: ${pyToFixed(value, decimals)}${unit ? " " + unit : ""}`);
  }
  return lines;
}

const AMINO_ACID_FIELDS = [
  ["alanine", "Alanine"], ["arginine", "Arginine"], ["aspartic_acid", "Aspartic Acid"],
  ["cystine", "Cystine"], ["glutamic_acid", "Glutamic Acid"], ["glycine", "Glycine"],
  ["histidine", "Histidine"], ["isoleucine", "Isoleucine"], ["leucine", "Leucine"],
  ["lysine", "Lysine"], ["methionine", "Methionine"], ["phenylalanine", "Phenylalanine"],
  ["proline", "Proline"], ["serine", "Serine"], ["threonine", "Threonine"],
  ["tryptophan", "Tryptophan"], ["tyrosine", "Tyrosine"], ["valine", "Valine"],
];
function aminoAcidLines(nutrition) {
  const lines = [];
  for (const [key, label] of AMINO_ACID_FIELDS) {
    const value = nutrition[key];
    if (value === undefined || value === null) continue;
    lines.push(`${label}: ${pyToFixed(value, 2)} g`);
  }
  return lines;
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// --- schema.org -------------------------------------------------------------

function buildSchemaOrg(record, imageSrc) {
  const ingredients = record.ingredients.map(ingredientLine);
  const steps = record.instructions.map(step => {
    const [primary, callouts] = stepTextAndCallouts(step);
    const text = callouts.length ? `${primary} (${callouts.join("; ")})` : primary;
    return { "@type": "HowToStep", text };
  });

  const nutrition = record.nutrition || {};
  const nutritionInfo = { "@type": "NutritionInformation" };
  if (nutrition.energy != null) nutritionInfo.calories = `${nutrition.energy.toFixed(0)} calories`;
  if (nutrition.fat != null) nutritionInfo.fatContent = `${nutrition.fat.toFixed(1)} g`;
  if (nutrition.saturated != null) nutritionInfo.saturatedFatContent = `${nutrition.saturated.toFixed(1)} g`;
  if (nutrition.carbs != null) nutritionInfo.carbohydrateContent = `${nutrition.carbs.toFixed(1)} g`;
  if (nutrition.fiber != null) nutritionInfo.fiberContent = `${nutrition.fiber.toFixed(1)} g`;
  if (nutrition.sugars != null) nutritionInfo.sugarContent = `${nutrition.sugars.toFixed(1)} g`;
  if (nutrition.protein != null) nutritionInfo.proteinContent = `${nutrition.protein.toFixed(1)} g`;
  if (nutrition.sodium != null) nutritionInfo.sodiumContent = `${nutrition.sodium.toFixed(1)} mg`;
  if (nutrition.cholesterol != null) nutritionInfo.cholesterolContent = `${nutrition.cholesterol.toFixed(1)} mg`;

  const aminoProps = AMINO_ACID_FIELDS
    .filter(([key]) => nutrition[key] != null)
    .map(([key, label]) => ({ "@type": "PropertyValue", name: label, value: `${nutrition[key].toFixed(2)} g` }));
  if (aminoProps.length) nutritionInfo.additionalProperty = aminoProps;

  const schema = {
    "@context": "https://schema.org/",
    "@type": "Recipe",
    name: record.name,
    recipeYield: `${record.serving_count} servings`,
    recipeIngredient: ingredients,
    recipeInstructions: steps,
  };
  if (record.cooking_minutes) schema.totalTime = `PT${Math.trunc(record.cooking_minutes)}M`;
  if (imageSrc) schema.image = [imageSrc];
  const meta = record.schema_metadata || {};
  if (meta.category) schema.recipeCategory = meta.category;
  if (meta.cuisine) schema.recipeCuisine = meta.cuisine;
  if (meta.keywords) schema.keywords = Array.isArray(meta.keywords) ? meta.keywords.join(", ") : meta.keywords;
  if (Object.keys(nutritionInfo).length > 1) schema.nutrition = nutritionInfo;
  return schema;
}

// --- HTML rendering (mirrors render_html; no PDF path/show_amino_acids toggle --
// this tool never emits PDF, so amino acids are always shown) --------------------

function renderHtml(record, imageSrc) {
  const schema = buildSchemaOrg(record, imageSrc);
  const types = decodeTypes(record.allowed_type_ids);
  const restrictions = decodeRestrictions(record.violated_restriction_ids);

  const ingredientsHtml = record.ingredients.map(ing => `    <li>${escapeHtml(ingredientLine(ing))}</li>`).join("\n");

  const stepsHtml = record.instructions.map(step => {
    const [primary, callouts] = stepTextAndCallouts(step);
    let item = `    <li>${escapeHtml(primary)}`;
    if (callouts.length) {
      item += `<div class="callout">${callouts.map(escapeHtml).join("<br>")}</div>`;
    }
    return item + "</li>";
  }).join("\n");

  let equipmentHtml = "";
  if (record.cookwares && record.cookwares.length) {
    const items = record.cookwares.map(c => `    <li>${escapeHtml(c.name)}</li>`).join("\n");
    equipmentHtml = `<h2>Equipment</h2>\n<ul>\n${items}\n</ul>\n`;
  }

  let nutritionHtml = "";
  const lines = nutritionLines(record.nutrition || {});
  if (lines.length) {
    const rows = lines.map(l => {
      const [label, val] = [l.slice(0, l.indexOf(":")), l.slice(l.indexOf(":") + 1).trim()];
      return `    <tr><td>${escapeHtml(label)}</td><td>${escapeHtml(val)}</td></tr>`;
    }).join("\n");
    nutritionHtml = `<h2>Nutrition (per serving)</h2>\n<table>\n${rows}\n</table>\n`;
  }
  const aminoFound = aminoAcidLines(record.nutrition || {});
  if (aminoFound.length) {
    const rows = aminoFound.map(l => {
      const [label, val] = [l.slice(0, l.indexOf(":")), l.slice(l.indexOf(":") + 1).trim()];
      return `    <tr><td>${escapeHtml(label)}</td><td>${escapeHtml(val)}</td></tr>`;
    }).join("\n");
    nutritionHtml += `<h2>Amino Acids (per serving)</h2>\n<table>\n${rows}\n</table>\n`;
  }

  const badges = types.map(t => `<span class="badge">${escapeHtml(t)}</span>`).join("");
  const warnings = restrictions.map(r => `<span class="badge warn">${escapeHtml(r)}</span>`).join("");
  const imageHtml = imageSrc ? `<img src="${escapeHtml(imageSrc)}" alt="${escapeHtml(record.name)}">` : "";

  const ingredientsBlock = `<h2>Ingredients</h2>\n<ul>\n${ingredientsHtml}\n</ul>`;
  const ingredientsSection = nutritionHtml
    ? `<div class="two-col">\n<div>\n${ingredientsBlock}\n</div>\n<div>\n${nutritionHtml}\n</div>\n</div>`
    : ingredientsBlock;

  const schemaJson = JSON.stringify(schema, null, 2);

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${escapeHtml(record.name)}</title>
<script type="application/ld+json">
${schemaJson}
</` + `script>
<style>
body { font-family: Georgia, serif; max-width: 720px; margin: 2em auto; padding: 0 1em; color: #222; }
h1 { font-size: 1.8em; }
img { max-width: 100%; border-radius: 8px; }
.badge { display: inline-block; background: #e6f4ea; color: #1a5e2a; border-radius: 12px; padding: 2px 10px; margin: 2px 4px 2px 0; font-size: 0.85em; }
.badge.warn { background: #fce8e6; color: #8a1c13; }
.meta { color: #555; margin: 0.5em 0 1em; }
.callout { color: #666; font-size: 0.9em; font-style: italic; margin-top: 2px; }
table { border-collapse: collapse; }
td { padding: 2px 12px 2px 0; }
.two-col { display: flex; gap: 2.5em; align-items: flex-start; }
.two-col > div { flex: 1; min-width: 0; }
.page-break { page-break-before: always; }
</style>
</head>
<body>
<h1>${escapeHtml(record.name)}</h1>
${imageHtml}
<p class="meta">${record.serving_count} servings &middot; ${record.cooking_minutes ?? "?"} min &middot; ${escapeHtml(record.units || "")} units</p>
<div>${badges}${warnings}</div>
${ingredientsSection}
<div class="page-break">
<h2>Instructions</h2>
<ol>
${stepsHtml}
</ol>
${equipmentHtml}
</div>
</body>
</html>
`;
}

// --- ORF ----------------------------------------------------------------------

function buildOrf(record) {
  const ingredientsOrf = record.ingredients.map(ing => {
    const [amount, unit] = parseQuantity(ing.quantity);
    return { [ing.name]: { amounts: [{ amount, unit }] } };
  });

  const stepsOrf = record.instructions.map(step => {
    const [primary, callouts] = stepTextAndCallouts(step);
    const entry = { step: primary };
    if (callouts.length) entry.notes = callouts;
    return entry;
  });

  const orf = {
    recipe_uuid: `recipe-archive:${record.id}`,
    recipe_name: record.name,
    yields: [{ amount: record.serving_count, unit: "servings" }],
    ingredients: ingredientsOrf,
    steps: stepsOrf,
  };

  const notes = [];
  if (record.cooking_minutes) notes.push(`Total cook time: ${record.cooking_minutes} minutes`);
  const types = decodeTypes(record.allowed_type_ids);
  if (types.length) notes.push("Suitable menus: " + types.join(", "));
  const restrictions = decodeRestrictions(record.violated_restriction_ids);
  if (restrictions.length) notes.push("Contains/violates: " + restrictions.join(", "));
  if (record.cookwares && record.cookwares.length) notes.push("Equipment: " + record.cookwares.map(c => c.name).join(", "));
  const nLines = nutritionLines(record.nutrition || {});
  if (nLines.length) notes.push("Nutrition (per serving): " + nLines.join("; "));
  if (notes.length) orf.notes = notes;

  return orf;
}

// --- Paprika --------------------------------------------------------------------

// Port of recipe_export.py's build_paprika(): same field set (reverse-engineered from a real
// .paprikarecipe export) -- Paprika rejects records that lack the uid/created/hash/photo fields it
// writes itself. Keep the two in step.

const PAPRIKA_UUID_NAMESPACE_HEX = "d38f3b0a6e0a4b0d9c8f9e6f1e6e6c9a";  // same as the Python tool
const SHA1_INIT = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
const SHA256_INIT = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
const SHA256_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

// Pads a message the way SHA-1 and SHA-256 both require (0x80, zeros, 64-bit big-endian bit length).
function shaPad(bytes) {
  const padded = new Uint8Array(((bytes.length + 9 + 63) >> 6) << 6);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  const bitLength = bytes.length * 8;
  view.setUint32(padded.length - 8, Math.floor(bitLength / 0x100000000));
  view.setUint32(padded.length - 4, bitLength >>> 0);
  return view;
}

// Synchronous SHA-1/SHA-256 (crypto.subtle is async and only exists in secure contexts). Used only
// to derive ids and content fingerprints, nothing security-sensitive.
function sha1Bytes(bytes) {
  const view = shaPad(bytes);
  const h = [...SHA1_INIT];
  const w = new Uint32Array(80);
  for (let off = 0; off < view.byteLength; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4);
    for (let i = 16; i < 80; i++) {
      const x = w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16];
      w[i] = (x << 1) | (x >>> 31);
    }
    let [a, b, c, d, e] = h;
    for (let i = 0; i < 80; i++) {
      let f, k;
      if (i < 20) { f = (b & c) | (~b & d); k = 0x5A827999; }
      else if (i < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1; }
      else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDC; }
      else { f = b ^ c ^ d; k = 0xCA62C1D6; }
      const t = (((a << 5) | (a >>> 27)) + f + e + k + w[i]) >>> 0;
      e = d; d = c; c = ((b << 30) | (b >>> 2)) >>> 0; b = a; a = t;
    }
    h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0; h[2] = (h[2] + c) >>> 0;
    h[3] = (h[3] + d) >>> 0; h[4] = (h[4] + e) >>> 0;
  }
  const out = new Uint8Array(20);
  const outView = new DataView(out.buffer);
  h.forEach((v, i) => outView.setUint32(i * 4, v));
  return out;
}

function sha256Bytes(bytes) {
  const view = shaPad(bytes);
  const h = [...SHA256_INIT];
  const w = new Uint32Array(64);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));
  for (let off = 0; off < view.byteLength; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i++) {
      const t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + SHA256_K[i] + w[i]) >>> 0;
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0; h[2] = (h[2] + c) >>> 0; h[3] = (h[3] + d) >>> 0;
    h[4] = (h[4] + e) >>> 0; h[5] = (h[5] + f) >>> 0; h[6] = (h[6] + g) >>> 0; h[7] = (h[7] + hh) >>> 0;
  }
  const out = new Uint8Array(32);
  const outView = new DataView(out.buffer);
  h.forEach((v, i) => outView.setUint32(i * 4, v));
  return out;
}

function bytesToHex(bytes) { return [...bytes].map(b => b.toString(16).padStart(2, "0")).join(""); }
function hexToUuid(hex) {
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// uuid5(PAPRIKA_UUID_NAMESPACE, String(recordId)), uppercased -- identical to the Python tool's uid,
// so re-importing an updated export overwrites rather than duplicates in apps that dedupe on it.
function paprikaUid(recordId) {
  const nsBytes = Uint8Array.from(PAPRIKA_UUID_NAMESPACE_HEX.match(/../g).map(b => parseInt(b, 16)));
  const nameBytes = new TextEncoder().encode(String(recordId));
  const input = new Uint8Array(nsBytes.length + nameBytes.length);
  input.set(nsBytes);
  input.set(nameBytes, nsBytes.length);
  const bytes = sha1Bytes(input).slice(0, 16);
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  return hexToUuid(bytesToHex(bytes)).toUpperCase();
}

function randomUuid4() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  return hexToUuid(bytesToHex(bytes));
}

function bytesToBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

// JSON with object keys sorted at every level, like Python's json.dumps(sort_keys=True).
function sortedJson(value) {
  return JSON.stringify(value, (key, v) => (v && typeof v === "object" && !Array.isArray(v))
    ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
    : v);
}

function localTimestamp(date) {
  const p = n => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())} ` +
         `${p(date.getHours())}:${p(date.getMinutes())}:${p(date.getSeconds())}`;
}

function buildPaprika(record, imageBytes = null, imageExt = ".jpg") {
  const ingredientsText = record.ingredients.map(ingredientLine).join("\n");

  const directionsParts = record.instructions.map(step => {
    const [primary, callouts] = stepTextAndCallouts(step);
    let text = primary;
    if (callouts.length) text += "\n(" + callouts.join("; ") + ")";
    return text;
  });
  const directionsText = directionsParts.join("\n\n");

  const notesParts = [];
  const restrictions = decodeRestrictions(record.violated_restriction_ids);
  if (restrictions.length) notesParts.push("Contains/violates: " + restrictions.join(", "));
  if (record.cookwares && record.cookwares.length) notesParts.push("Equipment: " + record.cookwares.map(c => c.name).join(", "));

  const fields = {
    uid: paprikaUid(record.id),
    created: localTimestamp(new Date()),
    name: record.name,
    description: "",
    ingredients: ingredientsText,
    directions: directionsText,
    notes: notesParts.join("\n"),
    nutritional_info: [...nutritionLines(record.nutrition || {}), ...aminoAcidLines(record.nutrition || {})].join("\n"),
    prep_time: "",
    cook_time: record.cooking_minutes ? `${record.cooking_minutes} min` : "",
    total_time: "",
    difficulty: "",
    servings: `${record.serving_count} servings`,
    rating: 0,
    source: "",
    source_url: record.source_url || "",
    photo: null,
    photo_large: null,
    photo_hash: null,
    image_url: null,
    categories: exportTagNames(record),
    photos: [],
  };

  // Real Paprika exports store the image twice: a top-level photo/photo_data/photo_hash trio plus a
  // "photos" array entry whose "filename" matches photo_large and whose own "hash" is the SHA-256 of
  // ITS data. There's only one source image, so it's reused for both slots (confirmed against a real
  // export: photo_hash == sha256(photo_data's decoded bytes), uppercase).
  if (imageBytes) {
    const photoFilename = `${randomUuid4().toUpperCase()}${imageExt}`;
    const photoLargeFilename = `${randomUuid4().toUpperCase()}${imageExt}`;
    const photoB64 = bytesToBase64(imageBytes);
    const photoHash = bytesToHex(sha256Bytes(imageBytes)).toUpperCase();
    fields.photo = photoFilename;
    fields.photo_data = photoB64;
    fields.photo_hash = photoHash;
    fields.photo_large = photoLargeFilename;
    fields.photos = [{ name: "1", filename: photoLargeFilename, hash: photoHash, data: photoB64 }];
  }

  // No known-correct formula for the top-level hash from a single example, so (like the Python tool)
  // it's a content fingerprint: SHA-256 of the other fields, canonically serialized.
  fields.hash = bytesToHex(sha256Bytes(new TextEncoder().encode(sortedJson(fields)))).toUpperCase();
  return fields;
}

// --- Norish -----------------------------------------------------------------------
//
// Port of recipe_export.py's Norish archive builder (build_norish_recipe and helpers),
// which was itself reverse-engineered from Norish's own archive writer and Zod schemas.
// Keep the two in step: a change to the tag rules, rescaling or field mapping there
// belongs here too.

const VARIETY_TAGS = {
  "1": "Pasta & pizza", "2": "Soups, stews, & chilis", "4": "Burgers & sandwiches",
  "6": "Curries", "7": "Frittatas", "8": "Fritters & cakes", "11": "Stir fries",
  "12": "Tacos & quesadillas", "14": "Steaks", "15": "Chops", "18": "Fried rice & noodles",
  "20": "Main-course salads", "22": "BBQ & Grilling", "24": "Baked", "25": "Bowls",
  "26": "Stuffed vegetables", "27": "Wraps", "29": "Low-carb \"pastas\"", "30": "Wings & tenders",
  "32": "Skillets & sautés", "33": "Pan-fried",
  // ids 35-43 are ours, not Mealime's: Albertsons cookbooks with no Mealime counterpart, turned into
  // variety tags by albertsons_cookbook_slugs_sync.py (see ALBERTSONS_VARIETY_TAGS in the local tool).
  "35": "Dessert", "36": "Breakfast", "37": "Snacks", "38": "Super simple",
  "39": "Mediterranean", "40": "Set it & forget it", "41": "Under 15 minutes",
  "42": "Sports fan favorites", "43": "Kid friendly",
};
// Menu types worth tagging: Classic is every recipe and Flexitarian nearly so, so both are left out.
const MENU_TYPE_TAG_IDS = [2, 4, 5, 6, 7, 8];  // Vegetarian, Pescatarian, Paleo, Vegan, Low Carb, Keto

// "<Restriction>-Free" tag names for every restriction id NOT in violated_restriction_ids -- e.g. a
// recipe with violated_restriction_ids [1, 11] gets "Dairy-Free", "Fish-Free", ... for the other ten
// restrictions, but never "Gluten-Free" or "Egg-Free". null (unknown -- restrictions never evaluated
// for this recipe) yields NO tags at all: asserting "-Free" on a recipe whose restrictions were never
// checked would be a false claim, not an absence of the allergen.
function restrictionFreeTagNames(record) {
  const violated = record.violated_restriction_ids;
  if (violated == null) return [];
  const violatedIds = new Set(violated);
  return Object.entries(RESTRICTIONS).filter(([id]) => !violatedIds.has(Number(id))).map(([, name]) => `${name}-Free`);
}

// The tag/category set shared by the Norish (tags) and Paprika (categories) builders, so both formats
// offer the end user the same filtering: variety_tag_ids (VARIETY_TAGS), the menu types worth tagging
// (MENU_TYPE_TAG_IDS), and restrictionFreeTagNames(). Mirrors the local tool's export_tag_names()
// exactly. Deduplicated, order preserved.
function exportTagNames(record) {
  const names = (record.variety_tag_ids || []).filter(v => VARIETY_TAGS[String(v)]).map(v => VARIETY_TAGS[String(v)]);
  for (const t of MENU_TYPE_TAG_IDS) {
    if ((record.allowed_type_ids || []).includes(t)) names.push(MENU_TYPES[String(t)]);
  }
  names.push(...restrictionFreeTagNames(record));
  return [...new Set(names)];
}
const NORISH_SYSTEM_USED = { "US": "us", "Metric": "metric" };
const NORISH_ARCHIVE_FORMAT = "norish-recipes";
const NORISH_ARCHIVE_FORMAT_VERSION = 1;
const NORISH_EXPORTER_ORIGIN = "https://recipe-export.invalid";
// tbsp counts that convert to a cup fraction instead of staying in tbsp.
const TBSP_TO_CUP_FRACTION = { 4: 1 / 4, 5: 1 / 3, 6: 1 / 3, 8: 1 / 2 };
const NUMERIC_6_2_LIMIT = 9999.99;  // numeric(6,2)'s largest magnitude: fat/carbs/protein overflow beyond it

// Python-style round(): exact halves go to the even neighbour (774.5 -> 774), unlike Math.round.
function pyRound(value, digits = 0) {
  const factor = 10 ** digits;
  const scaled = value * factor;
  const floor = Math.floor(scaled);
  const rounded = (scaled - floor === 0.5) ? (floor % 2 === 0 ? floor : floor + 1) : Math.round(scaled);
  return rounded / factor;
}

// Strict core of parseQuantity: null when there's no recognizable leading number, instead of
// parseQuantity's display-only fallback (amount 1). Anything doing arithmetic must use this.
function parseLeadingNumber(text) {
  text = (text || "").trim();
  if (!text) return null;
  for (const [ch, value] of FRACTION_ITEMS) {
    if (text.startsWith(ch)) return [value, text.slice(ch.length).trim()];
    const wholeMatch = text.match(new RegExp(`^(\\d+)\\s*${ch}(.*)$`, "s"));
    if (wholeMatch) return [parseInt(wholeMatch[1], 10) + value, wholeMatch[2].trim()];
  }
  const match = text.match(/^\s*(\d+(?:\.\d+)?)\s*(.*)$/s);
  if (match) return [parseFloat(match[1]), match[2].trim()];
  return null;
}

// Name forms worth trying as a suffix match against a step's callout line: the full name, the
// part before the first comma, the name without a parenthetical, and a singular/plural toggle.
function ingredientNameVariants(name) {
  const variants = new Set([name.trim()]);
  if (name.includes(",")) variants.add(name.split(",", 2)[0].trim());
  for (const variant of [...variants]) {
    const stripped = variant.replace(/\s*\([^)]*\)\s*/g, " ").trim();
    if (stripped) variants.add(stripped);
  }
  for (const variant of [...variants]) {
    variants.add(variant.endsWith("s") && variant.length > 1 ? variant.slice(0, -1) : variant + "s");
  }
  return [...variants].filter(Boolean);
}

// Index of the ingredient whose name is a case-insensitive suffix of this callout line,
// preferring the longest (most specific) match; -1 if none.
function matchCalloutIngredient(line, ingredients) {
  const lineNorm = line.trim().replace(/\.+$/, "").toLowerCase();
  let best = -1, bestLen = -1;
  ingredients.forEach((ing, index) => {
    for (const variant of ingredientNameVariants(ing.name)) {
      const variantLower = variant.toLowerCase();
      if (lineNorm.endsWith(variantLower) && variantLower.length > bestLen) { best = index; bestLen = variantLower.length; }
    }
  });
  return best;
}

// One list of {ingredientOrder, share, order} chips per instruction step, from that step's
// callout lines ("<quantity> <ingredient name>"). share is the fraction of the ingredient's
// total quantity used in this step (1.0 unless split across steps, or unknown).
function buildNorishStepIngredients(record) {
  const ingredients = record.ingredients || [];
  return (record.instructions || []).map(step => {
    const secondary = (step.secondary_message || "").trim();
    const chips = [];
    for (const line of secondary.split("\n").map(l => l.trim()).filter(Boolean)) {
      const ingredientOrder = matchCalloutIngredient(line, ingredients);
      if (ingredientOrder < 0) continue;
      let share = 1.0;
      const stepParsed = parseLeadingNumber(line);
      const totalParsed = parseLeadingNumber(ingredients[ingredientOrder].quantity || "");
      if (stepParsed && totalParsed && totalParsed[0]) {
        const ratio = pyRound(Math.min(stepParsed[0] / totalParsed[0], 1.0), 4);
        if (ratio > 0) share = ratio;
      }
      chips.push({ ingredientOrder, share, order: chips.length });
    }
    return chips;
  });
}

// Friendlier display units, since Norish has no rescaling of its own: kg -> g below 1 kg, L -> mL
// below 1 L, tbsp -> tsp below 1 tbsp and to a cup fraction at the counts in TBSP_TO_CUP_FRACTION.
function rescaleNorishAmount(amount, unit) {
  if (unit === null) return [amount, unit];
  const unitLower = unit.trim().toLowerCase();
  if (unitLower === "kg") return amount < 1 ? [pyRound(amount * 1000, 3), "g"] : [pyRound(amount, 2), "kg"];
  if (["l", "liter", "liters", "litre", "litres"].includes(unitLower)) {
    return amount < 1 ? [pyRound(amount * 1000, 3), "mL"] : [pyRound(amount, 2), "L"];
  }
  if (unitLower === "tbsp") {
    if (amount < 1) return [pyRound(amount * 3, 3), "tsp"];
    const nearestWhole = pyRound(amount);
    if (Math.abs(amount - nearestWhole) < 0.001 && nearestWhole in TBSP_TO_CUP_FRACTION) {
      return [pyRound(TBSP_TO_CUP_FRACTION[nearestWhole], 3), "cup"];
    }
    return [amount, "tbsp"];
  }
  return [amount, unit];
}

// A 2-decimal string for one of Norish's numeric(6,2) columns, or null when there's no value or it
// would overflow the column (corrupt source data makes Norish reject the WHOLE recipe otherwise).
function formatNumeric62(value) {
  if (value === undefined || value === null) return null;
  if (Math.abs(value) > NUMERIC_6_2_LIMIT) return null;
  return value.toFixed(2);
}

function buildNorishRecipe(record) {
  const systemUsed = NORISH_SYSTEM_USED[record.units] || "metric";
  const nutrition = record.nutrition || {};
  const stepChips = buildNorishStepIngredients(record);

  const recipeIngredients = (record.ingredients || []).map((ing, i) => {
    const [parsedAmount, unitText] = parseQuantity(ing.quantity);
    const [amount, unit] = rescaleNorishAmount(parsedAmount, unitText.trim() || null);
    return {
      ingredientId: null,
      ingredientName: ing.name,
      amount: pyRound(amount, 3),
      unit,
      systemUsed,
      order: i,
    };
  });

  const steps = (record.instructions || []).map((step, i) => ({
    step: step.primary_message,
    order: i,
    systemUsed,
    images: [],
    stepIngredients: stepChips[i],
  }));

  const tags = exportTagNames(record).map(name => ({ name }));

  const recipe = {
    name: record.name,
    description: null,
    url: null,
    notes: null,
    servings: record.serving_count,
    systemUsed,
    prepMinutes: null,
    cookMinutes: null,
    totalMinutes: record.cooking_minutes === undefined ? null : record.cooking_minutes,
    calories: (nutrition.energy !== undefined && nutrition.energy !== null) ? pyRound(nutrition.energy) : null,
    fat: formatNumeric62(nutrition.fat),
    carbs: formatNumeric62(nutrition.carbs),
    protein: formatNumeric62(nutrition.protein),
    originCountry: null,
    originCountryName: null,
    originRegion: null,
    provenanceNote: null,
    categories: [],
    tags,
    cuisines: [],
    recipeIngredients,
    steps,
    image: null,
    images: [],
    videos: [],
    authorName: null,
  };
  // Norish's own spelling: "favorite" (our field is "favourite"). Only present when a definite
  // true/false has been backfilled; omitted, never defaulted to false.
  if (typeof record.favourite === "boolean") recipe.favorite = record.favourite;
  return recipe;
}

// --menu name -> allowed_type_ids id, for selectMenuWinners(). Mirrors the local tool's
// MENU_NAME_TO_TYPE_ID exactly -- Classic requires type id 1 like every other choice; there's no
// "no filter" state any more (Classic is now the mandatory floor, not an "Any" option). Flexitarian
// isn't offered as a menu choice (nearly universal, discriminates nothing).
const MENU_NAME_TO_TYPE_ID = {
  classic: 1, vegetarian: 2, pescatarian: 4, paleo: 5, vegan: 6, "low-carb": 7, keto: 8,
};

// Case-insensitive keyword match between a recipe's own group_label and the selected menu name --
// selectMenuWinners()'s step 3 tie-break. Mirrors the local tool's label_matches_menu() exactly,
// including the "low-carb" spelling-variant handling ("Low-Carb/Paleo" vs "Low Carb / Paleo").
function labelMatchesMenu(groupLabel, menu) {
  if (!groupLabel) return false;
  const low = groupLabel.toLowerCase();
  if (menu === "low-carb") return low.includes("low-carb") || low.includes("low carb");
  return low.includes(menu);
}

// Steps 3-5 of selectMenuWinners()'s hierarchy: `entries` must already be restriction- and
// menu-type-filtered (steps 1-2). Prefers a group_label match, then lowest position, then lowest
// id (always unique, final tiebreak). Mirrors the local tool's _pick_winner() exactly. Each entry
// must carry its own numeric `id` (selectMenuWinners() adds it from the manifest key).
function pickWinner(entries, menu) {
  const matches = entries.filter(e => labelMatchesMenu(e.group_label, menu));
  const pool = matches.length ? matches : entries;
  let best = pool[0];
  let bestRank = [best.position ?? Infinity, best.id];
  for (const e of pool.slice(1)) {
    const rank = [e.position ?? Infinity, e.id];
    if (rank[0] < bestRank[0] || (rank[0] === bestRank[0] && rank[1] < bestRank[1])) { best = e; bestRank = rank; }
  }
  return best;
}

// Mirrors the local tool's select_menu_winners(): decides, for every (recipe_id, units,
// serving_count) slot, which single recipe wins under the given menu/avoid/servings -- so a family
// with several diet-swap siblings of the same recipe (Classic, Vegan, a gluten-free version, ...)
// never exports more than one, and a family where Mealime re-issued the same dish under a whole
// new id set never exports both copies. See the local tool's module docstring for the full 5-step
// SELECTION rationale (1. restrictions, 2. menu type, 3. label match, 4. position, 5. id).
// Deliberately does NOT group by (recipe_id, group_label, name) first -- that grouping is exactly
// what let two genuinely different recipes sharing an unreliable label get silently merged, with
// no chance to prefer whichever copy was actually, explicitly labeled for the requested menu;
// group_label is only ever consulted as pickWinner()'s tie-break preference.
//
// --servings fallback (when `servings` isn't null) is scoped to (recipe_id, units) as a WHOLE --
// every serving count that units system offers, together -- rather than per exact dish name/label:
// an exact match ANYWHERE in that group wins outright, and only when the family truly has NOTHING
// at the requested count does the whole group compete for one fallback answer regardless of its
// serving count. Confirmed necessary by family 100: two real "Classic" recipes, one with kale
// locked to 4 servings and one without locked to 6 -- scoping the fallback any narrower would let
// the without-kale one get rescued even when a servings=4 request already had a perfectly good
// answer (the kale one). Applies to every export format now, not just Norish -- silently dropping
// a single-serving-count recipe is just as wrong for html/json/orf/paprika as for Norish.
function selectMenuWinners(manifest, menu, avoidIds, servings) {
  const menuTypeId = MENU_NAME_TO_TYPE_ID[menu];
  const allFamilies = new Set();
  const grouped = new Map();  // "recipeId|units" -> [entry, ...] (entry carries its own numeric id)
  for (const [idStr, e] of Object.entries(manifest.entries)) {
    allFamilies.add(e.recipe_id);
    const violated = e.violated_restriction_ids;
    if (avoidIds.size && (violated == null || violated.some(r => avoidIds.has(r)))) continue;
    if (!(e.allowed_type_ids || []).includes(menuTypeId)) continue;
    const key = `${e.recipe_id}|${e.units}`;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push({ ...e, id: Number(idStr) });
  }

  const winners = new Map();  // "recipeId|units|servingCount" -> winning id
  for (const [key, entries] of grouped) {
    const [recipeIdStr, units] = key.split("|");
    const recipeId = Number(recipeIdStr);
    if (servings != null) {
      const exact = entries.filter(e => e.serving_count === servings);
      if (exact.length) {
        winners.set(`${recipeId}|${units}|${servings}`, pickWinner(exact, menu).id);
      } else {
        const winner = pickWinner(entries, menu);  // fallback: any serving count, one overall winner
        winners.set(`${recipeId}|${units}|${winner.serving_count}`, winner.id);
      }
    } else {
      const byServing = new Map();
      for (const e of entries) {
        if (!byServing.has(e.serving_count)) byServing.set(e.serving_count, []);
        byServing.get(e.serving_count).push(e);
      }
      for (const [servingCount, recs] of byServing) {
        winners.set(`${recipeId}|${units}|${servingCount}`, pickWinner(recs, menu).id);
      }
    }
  }

  const familiesWithWinner = new Set([...winners.keys()].map(k => Number(k.split("|")[0])));
  return { winners, totalFamilies: allFamilies.size, familiesWithWinner: familiesWithWinner.size };
}

function sanitizeFilename(name) {
  const cleaned = String(name).replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").trim().replace(/\s+/g, " ");
  return (cleaned || "untitled").slice(0, 150);
}

// --- Fetch layer -----------------------------------------------------------------

// Paths are relative to this page, so it works wherever the folder is hosted.
async function fetchRepoFile(path, asBinary) {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`${resp.status} fetching ${path}`);
  return asBinary ? resp.arrayBuffer() : resp.text();
}

async function mapWithConcurrency(items, limit, fn) {
  const results = new Array(items.length);
  let index = 0;
  async function worker() {
    while (index < items.length) {
      const i = index++;
      try {
        results[i] = { ok: true, value: await fn(items[i], i) };
      } catch (err) {
        results[i] = { ok: false, error: err };
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

// --- Main flow -------------------------------------------------------------------

const statusEl = document.getElementById("status");
function log(msg) {
  statusEl.textContent += msg + "\n";
  statusEl.scrollTop = statusEl.scrollHeight;
}

document.getElementById("export-btn").addEventListener("click", async () => {
  const btn = document.getElementById("export-btn");
  statusEl.textContent = "";

  const selectedUnits = [...document.querySelectorAll('input[name="units"]:checked')].map(el => el.value);
  const selectedServings = [...document.querySelectorAll('input[name="servings"]:checked')].map(el => Number(el.value));
  const selectedMenu = document.querySelector('input[name="menu"]:checked').value;
  const avoidIds = new Set([...document.querySelectorAll('input[name="restriction"]:checked')].map(el => Number(el.value)));
  const selectedFormats = [...document.querySelectorAll('input[name="formats"]:checked')].map(el => el.value);
  if (!selectedUnits.length || !selectedServings.length) { log("Select at least one unit system and serving size."); return; }
  if (!selectedFormats.length) { log("Select at least one export format."); return; }

  btn.disabled = true;
  try {
    log("Fetching manifest.json...");
    const manifestText = await fetchRepoFile("manifest.json", false);
    const manifest = JSON.parse(manifestText);
    log(`Manifest loaded: ${Object.keys(manifest.entries).length} recipes total.`);

    const wantNorish = selectedFormats.includes("norish");

    // Menu selection always collapses each family to one recipe per (units, servings) slot --
    // "classic" is the mandatory floor, not an "Any"/no-filter state -- see selectMenuWinners()'s
    // comment for the full rationale, including its own --servings fallback (applies to every
    // format now, not just Norish).
    const sampleEntry = Object.values(manifest.entries)[0];
    if (sampleEntry && sampleEntry.position === undefined) {
      log("Note: manifest.json has no position data, so menu selection ranks ties by variant id "+
          "alone (less precise than with position). Regenerate the manifest to include it.");
    }
    if (sampleEntry && sampleEntry.group_label === undefined) {
      log("Note: manifest.json has no group_label, so menu selection can't prefer an explicitly "+
          "labeled recipe when several tie on type (falls back to position/id alone). Regenerate "+
          "the manifest to include it.");
    }
    const requestedServings = selectedServings.length === 1 ? selectedServings[0] : null;
    const { winners: menuWinners, totalFamilies, familiesWithWinner } =
        selectMenuWinners(manifest, selectedMenu, avoidIds, requestedServings);
    log(`${familiesWithWinner} of ${totalFamilies} families have a recipe satisfying menu=${selectedMenu} `+
        `avoid=[${[...avoidIds].join(",") || "none"}]; ${totalFamilies - familiesWithWinner} excluded entirely.`);

    const matches = Object.entries(manifest.entries).filter(([idStr, e]) => {
      if (!selectedUnits.includes(e.units)) return false;
      const slot = `${e.recipe_id}|${e.units}|${e.serving_count}`;
      return menuWinners.get(slot) === Number(idStr);
    });
    log(`${matches.length} recipe(s) match your filters.`);
    if (!matches.length) { log("Nothing to export."); return; }

    const familyIds = [...new Set(matches.map(([, e]) => e.recipe_id))];
    log(`Spanning ${familyIds.length} recipe families. Fetching recipe files (concurrency ${CONCURRENCY})...`);

    const zip = new JSZip();
    const paprikaBundleEntries = [];
    const paprikaPending = [];
    const norishEntries = [];
    let done = 0, failed = 0;

    await mapWithConcurrency(matches, CONCURRENCY, async ([id, entry]) => {
      const text = await fetchRepoFile(entry.path, false);
      const record = JSON.parse(text);
      const familyFolder = zip.folder("families").folder(String(entry.recipe_id));
      const baseName = `${record.id}_${record.slug}`;

      if (selectedFormats.includes("json")) {
        familyFolder.file(`${baseName}.json`, text);
      }
      if (wantNorish) {
        const norishRecipe = buildNorishRecipe(record);
        familyFolder.file(`${baseName}.norish.json`, JSON.stringify(norishRecipe, null, 2));
        norishEntries.push({ id: String(record.id), recipeId: entry.recipe_id, recipe: norishRecipe });
      }
      if (selectedFormats.includes("html")) {
        const thumbName = manifest.family_thumbnails[String(entry.recipe_id)];
        familyFolder.file(`${baseName}.html`, renderHtml(record, thumbName || null));
      }
      if (selectedFormats.includes("orf")) {
        familyFolder.file(`${baseName}.orf.yaml`, jsyaml.dump(buildOrf(record), { sortKeys: false }));
      }
      if (selectedFormats.includes("paprika")) {
        // Built after the thumbnails are fetched (below): Paprika embeds the photo in each record.
        paprikaPending.push({ record, recipeId: entry.recipe_id, familyFolder, baseName });
      }

      done++;
      if (done % 200 === 0) log(`  ...${done}/${matches.length} recipes processed`);
    }).then(results => {
      failed = results.filter(r => !r.ok).length;
      if (failed) {
        log(`${failed} file(s) failed to fetch (see console for details).`);
        results.forEach(r => { if (!r.ok) console.error(r.error); });
      }
    });

    const thumbBytes = new Map();  // family id -> thumbnail bytes, for the Norish archive and Paprika photos
    if (selectedFormats.includes("html") || wantNorish || paprikaPending.length) {
      log("Fetching thumbnails...");
      const thumbFamilies = familyIds.filter(fid => manifest.family_thumbnails[String(fid)]);
      await mapWithConcurrency(thumbFamilies, CONCURRENCY, async fid => {
        const thumbName = manifest.family_thumbnails[String(fid)];
        const bytes = await fetchRepoFile(`families/${fid}/${thumbName}`, true);
        thumbBytes.set(String(fid), bytes);
        if (selectedFormats.includes("html")) zip.folder("families").folder(String(fid)).file(thumbName, bytes);
      });
    }

    if (paprikaPending.length) {
      log("Building Paprika recipes...");
      for (const { record, recipeId, familyFolder, baseName } of paprikaPending) {
        const thumbName = manifest.family_thumbnails[String(recipeId)];
        const thumbData = thumbBytes.get(String(recipeId));
        const imageExt = thumbName ? thumbName.slice(thumbName.lastIndexOf(".")) : ".jpg";
        const paprikaJson = JSON.stringify(buildPaprika(record, thumbData ? new Uint8Array(thumbData) : null, imageExt));
        const gz = pako.gzip(paprikaJson);
        familyFolder.file(`${baseName}.paprikarecipe`, gz);
        paprikaBundleEntries.push([`${sanitizeFilename(record.name)}_${record.id}.paprikarecipe`, gz]);
      }
    }

    if (wantNorish && norishEntries.length) {
      log("Building Norish archive...");
      const archive = new JSZip();
      archive.file("manifest.json", JSON.stringify({
        format: NORISH_ARCHIVE_FORMAT,
        formatVersion: NORISH_ARCHIVE_FORMAT_VERSION,
        exportedAt: new Date().toISOString(),
        exporter: { name: null, origin: NORISH_EXPORTER_ORIGIN },
        recipeCount: norishEntries.length,
      }, null, 2));
      norishEntries.sort((a, b) => Number(a.id) - Number(b.id));
      for (const { id, recipeId, recipe } of norishEntries) {
        const thumbName = manifest.family_thumbnails[String(recipeId)];
        const thumbData = thumbBytes.get(String(recipeId));
        let toWrite = recipe;
        if (thumbName && thumbData) {
          const imageRel = `images/${thumbName}`;
          toWrite = { ...recipe, image: imageRel, images: [{ image: imageRel, order: 0 }] };
          archive.file(`${id}/${imageRel}`, thumbData);
        }
        archive.file(`${id}/recipe.json`, JSON.stringify(toWrite, null, 2));
      }
      const archiveBytes = await archive.generateAsync({ type: "uint8array", compression: "DEFLATE" });
      zip.file(`norish-recipes-${new Date().toISOString().slice(0, 10)}.norishrecipes`, archiveBytes);
    }

    if (selectedFormats.includes("paprika") && paprikaBundleEntries.length) {
      log("Building combined library.paprikarecipes bundle...");
      const bundleZip = new JSZip();
      for (const [name, bytes] of paprikaBundleEntries) bundleZip.file(name, bytes, { compression: "STORE" });
      const bundleBytes = await bundleZip.generateAsync({ type: "uint8array", compression: "STORE" });
      zip.file("library.paprikarecipes", bundleBytes);
    }

    log(`Done: ${done} recipe(s) exported (${failed} failed). Building zip...`);
    const zipBlob = await zip.generateAsync({ type: "blob" });
    const url = URL.createObjectURL(zipBlob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `recipe_export_${Date.now()}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    log("Zip downloaded.");
  } catch (err) {
    log(`Error: ${err.message}`);
    console.error(err);
  } finally {
    btn.disabled = false;
  }
});
