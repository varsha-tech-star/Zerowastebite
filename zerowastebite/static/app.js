function tick() {
  const now = Date.now() / 1000;
  document.querySelectorAll(".timer").forEach(el => {
    const s = Math.floor(el.dataset.end - now);
    if (s <= 0) { el.textContent = "Closed"; return; }
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    el.textContent = h ? `${h}h ${m}m left` : `${m}m ${String(sec).padStart(2, "0")}s left`;
    el.classList.toggle("soon", s < 1800);
  });
}
tick(); setInterval(tick, 1000);

const modal = document.getElementById("modal");
if (modal) {
  document.querySelectorAll("[data-reserve]").forEach(b => b.addEventListener("click", () => {
    document.getElementById("reserve-form").action = "/reserve/" + b.dataset.reserve;
    document.getElementById("m-title").textContent = b.dataset.title;
    document.getElementById("m-sub").textContent = `${b.dataset.vendor} · ₹${b.dataset.price} · pay at pickup`;
    modal.showModal();
  }));
  modal.querySelector("[data-close]").addEventListener("click", () => modal.close());
  modal.addEventListener("click", e => { if (e.target === modal) modal.close(); });
}

// Saved bags (kept in this browser) + search + filter
const KEY = "zwb-saved";
const saved = new Set(JSON.parse(localStorage.getItem(KEY) || "[]"));
let filter = "all";
function apply() {
  const q = (document.getElementById("q")?.value || "").toLowerCase().trim();
  let shown = 0;
  document.querySelectorAll(".card").forEach(card => {
    const okCat = filter === "all" || (filter === "saved" ? saved.has(card.dataset.id) : card.dataset.cat === filter);
    const ok = okCat && card.dataset.text.includes(q);
    card.style.display = ok ? "" : "none"; shown += ok;
    card.querySelector(".heart").classList.toggle("on", saved.has(card.dataset.id));
  });
  const none = document.getElementById("none"); if (none) none.hidden = shown > 0 || !document.querySelector(".card");
}
document.querySelectorAll("[data-save]").forEach(h => h.addEventListener("click", () => {
  const id = h.dataset.save; saved.has(id) ? saved.delete(id) : saved.add(id);
  localStorage.setItem(KEY, JSON.stringify([...saved])); apply();
}));
document.querySelectorAll(".chip").forEach(chip => chip.addEventListener("click", () => {
  document.querySelectorAll(".chip").forEach(c => c.classList.toggle("on", c === chip));
  filter = chip.dataset.filter; apply();
}));
document.getElementById("q")?.addEventListener("input", apply);
apply();
