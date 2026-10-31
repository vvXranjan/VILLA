const $ = (s) => document.querySelector(s);
let stagedToken = null;
let scanStream = null;

/* ---------- tabs ---------- */
document.querySelectorAll(".tab-btn").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".tab-btn").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("#tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "arrived") loadArrived();
    if (b.dataset.tab === "pending") loadPending();
    if (b.dataset.tab === "review") loadReview();
    if (b.dataset.tab === "checkin") $("#searchInput").focus();
  };
});

/* ---------- stats ---------- */
async function loadStats() {
  const s = await (await fetch("/api/stats")).json();
  $("#statsBar").innerHTML =
    `Guests <b>${s.total}</b> · Regs <b>${s.registrations}</b> · ` +
    `<span class="s-paid">Paid ${s.paid}</span> · <span class="s-unpaid">Unpaid ${s.unpaid}</span> · ` +
    `<span class="s-review">Review ${s.under_review}</span> · ` +
    `✅ <b>${s.checked_in}</b> in · Paid left <b>${s.paid_remaining}</b> · <b>${s.pct}%</b>`;
}
setInterval(loadStats, 5000);

/* ---------- toast ---------- */
function toast(msg, kind = "ok") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.hidden = true), 3000);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function payBadge(status) {
  if (status === "PAID") return '<span class="badge paid">PAID</span>';
  if (status === "UNDER REVIEW") return '<span class="badge review">UNDER REVIEW</span>';
  return '<span class="badge unpaid">UNPAID</span>';
}

/* ---------- search (grouped by registration) ---------- */
const searchInput = $("#searchInput");
let searchTimer;
searchInput.oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(doSearch, 200);
};

async function doSearch() {
  const q = searchInput.value.trim();
  const box = $("#results");
  box.innerHTML = "";
  if (!q) return;
  const regs = await (await fetch("/api/registrations?q=" + encodeURIComponent(q))).json();
  if (!regs.length) {
    box.innerHTML = '<div class="hint">No guests found.</div>';
    return;
  }
  regs.forEach((r) => box.appendChild(regCard(r)));
}

function regCard(r) {
  const card = document.createElement("div");
  card.className = "reg-card";
  const pass = r.entry_pass || "?";
  const inCount = r.members.filter((m) => m.checked_in_at).length;
  let head = `
    <div class="reg-head">
      <div>
        <div class="reg-title">${esc(r.reg_id)} · ${esc(pass)} <span class="badge pass">${inCount}/${r.members.length} in</span></div>
        <div class="meta">${esc(r.phone || "no phone")}${r.email ? " · " + esc(r.email) : ""}</div>
      </div>
      <div>${payBadge(r.payment_status)}</div>
    </div>`;
  if (r.needs_review) {
    head += `<div class="review-flag">⚠ ${esc(r.review_reason)}</div>`;
  }
  let members = "";
  r.members.forEach((m) => {
    const checked = !!m.checked_in_at;
    let btn;
    if (checked) {
      btn = `<div class="member-status in">✔ ${esc(m.checked_in_at.slice(11, 16))} UTC</div>`;
    } else if (r.payment_status === "PAID") {
      btn = `<button class="btn primary tiny" data-gid="${m.id}">CHECK IN</button>`;
    } else {
      btn = `<button class="btn danger tiny" disabled>Not paid</button>`;
    }
    members += `
      <div class="member${checked ? " checked" : ""}">
        <div class="member-name">${checked ? "✔ " : "● "}${esc(m.name)}</div>
        ${btn}
      </div>`;
  });
  card.innerHTML = head + `<div class="members">${members}</div>`;
  card.querySelectorAll("button[data-gid]").forEach((b) => {
    b.onclick = async () => {
      const res = await (await fetch(`/api/guests/${b.dataset.gid}/checkin`, { method: "POST" })).json();
      toast(res.message, res.ok ? "ok" : "bad");
      loadStats();
      refreshSearchOrList();
    };
  });
  return card;
}

function refreshSearchOrList() {
  if (searchInput.value.trim()) doSearch();
  else if ($("#tab-pending").classList.contains("active")) loadPending();
  else if ($("#tab-arrived").classList.contains("active")) loadArrived();
}

/* ---------- QR resolve shows the whole registration ---------- */
async function resolveToken(token) {
  try {
    const g = await (await fetch("/api/qr/" + encodeURIComponent(token))).json();
    const regs = await (await fetch("/api/registrations?q=" + encodeURIComponent(g.reg_id))).json();
    const box = $("#results");
    box.innerHTML = "";
    if (regs.length) box.appendChild(regCard(regs[0]));
    if (g.payment_status !== "PAID") toast("Payment not verified", "bad");
    else toast("QR valid — " + g.name, "ok");
  } catch {
    toast("INVALID QR TOKEN", "bad");
  }
}

/* ---------- lists ---------- */
async function loadArrived() {
  const res = await (await fetch("/api/list/arrived")).json();
  $("#arrivedList").innerHTML = res.length
    ? res
        .map(
          (g) => `
      <div class="row checked">
        <div>
          <div class="name">${esc(g.name)}</div>
          <div class="meta">${esc(g.reg_id)} · ${esc(g.entry_pass || "")} · ${esc(g.phone || "")}</div>
        </div>
        <div class="badge in">✔ ${esc((g.checked_in_at || "").slice(11, 16))} UTC</div>
      </div>`
        )
        .join("")
    : '<div class="hint">No one has checked in yet.</div>';
}

async function loadPending() {
  const res = await (await fetch("/api/list/pending")).json();
  $("#pendingList").innerHTML = res.length
    ? res
        .map(
          (g) => `
      <div class="row" onclick="goSearch('${esc(g.name).replace(/'/g, "\\'")}')">
        <div>
          <div class="name">${esc(g.name)}</div>
          <div class="meta">${esc(g.reg_id)} · ${esc(g.entry_pass || "")} · ${esc(g.phone || "")}</div>
        </div>
        ${payBadge(g.payment_status)}
      </div>`
        )
        .join("")
    : '<div class="hint">Everyone has arrived! 🎉</div>';
}

async function loadReview() {
  const res = await (await fetch("/api/list/manual_review")).json();
  $("#reviewList").innerHTML = res.length
    ? res
        .map(
          (r) => `
      <div class="row" onclick="goSearch('${esc(r.reg_id)}')">
        <div>
          <div class="name">${esc(r.reg_id)} · ${esc(r.entry_pass)}</div>
          <div class="meta">${esc(r.review_reason)} — ${esc(r.members.map((m) => m.name).join(", "))}</div>
        </div>
        ${payBadge(r.payment_status)}
      </div>`
        )
        .join("")
    : '<div class="hint">No registrations need manual review. ✅</div>';
}

window.goSearch = (q) => {
  document.querySelector('[data-tab="checkin"]').click();
  searchInput.value = q;
  doSearch();
};

/* ---------- import: preview → confirm ---------- */
$("#previewBtn").onclick = async () => {
  const f = $("#csvFile").files[0];
  if (!f) return toast("Choose a CSV file first", "warn");
  const fd = new FormData();
  fd.append("file", f);
  const resp = await fetch("/api/import/preview", { method: "POST", body: fd });
  const p = await resp.json();
  if (!resp.ok) {
    $("#previewBox").hidden = false;
    $("#previewBox").innerHTML = `<div class="review-flag">Import failed: ${esc(p.detail || JSON.stringify(p))}</div>`;
    return;
  }
  stagedToken = p.token;
  const cases = p.manual_review_cases
    .map((c) => `<div class="review-flag">⚠ ${esc(c.reg_id)} (${esc(c.entry_pass)}) — ${esc(c.reason)} — names: ${esc(c.names.join(", ") || "(none)")}</div>`)
    .join("");
  $("#previewBox").hidden = false;
  $("#previewBox").innerHTML = `
    <h3>Preview: ${esc(p.file_name)}</h3>
    <table class="pv">
      <tr><td>Registration rows (non-empty)</td><td><b>${p.registration_rows}</b></td></tr>
      <tr><td>Blank rows skipped</td><td><b>${p.blank_rows_skipped}</b></td></tr>
      <tr><td>Individual named guests</td><td><b>${p.individual_guests}</b></td></tr>
      <tr><td>Solo / Couple / Duo / Trio</td><td><b>${p.solo} / ${p.couple} / ${p.duo} / ${p.trio}</b></td></tr>
      <tr><td>Other pass types</td><td><b>${p.other_pass}</b></td></tr>
      <tr><td>Paid registrations</td><td><b class="s-paid">${p.paid_regs}</b></td></tr>
      <tr><td>Unpaid registrations</td><td><b class="s-unpaid">${p.unpaid_regs}</b></td></tr>
      <tr><td>Payment review registrations</td><td><b class="s-review">${p.review_regs}</b></td></tr>
      <tr><td>Manual review (names ≠ pass count)</td><td><b>${p.manual_review_regs}</b></td></tr>
    </table>
    ${p.aadhaar_columns_ignored.length ? `<div class="hint">Aadhaar columns ignored: ${esc(p.aadhaar_columns_ignored.join(", "))} (never stored)</div>` : ""}
    ${p.duplicate_file_warning ? `<div class="review-flag">⚠ ${esc(p.duplicate_file_warning)}</div>` : ""}
    ${cases || '<div class="hint">No manual-review cases.</div>'}
    <h3>First registrations</h3>
    ${p.sample_guests.map((s) => `<div class="hint">${esc(s.reg_id)} · ${esc(s.entry_pass)} · ${esc(s.payment_status)} — ${esc(s.names.join(", "))}</div>`).join("")}
  `;
  $("#wipeRow").hidden = false;
  $("#confirmBtn").hidden = false;
};

$("#confirmBtn").onclick = async () => {
  if (!stagedToken) return;
  if (!confirm("Confirm import of this guest list?")) return;
  const wipe = $("#wipeCheck").checked;
  if (wipe && !confirm("You chose to REPLACE the entire existing guest list. A backup will be saved. Continue?")) return;
  const resp = await fetch(
    `/api/import/confirm?token=${stagedToken}&wipe_existing=${wipe}`,
    { method: "POST" }
  );
  const r = await resp.json();
  if (!resp.ok) {
    $("#importResult").textContent = r.detail || "Import failed";
    toast("Import failed", "bad");
    return;
  }
  $("#importResult").textContent = r.message;
  toast(`Imported ${r.guests} guests`, "ok");
  stagedToken = null;
  $("#confirmBtn").hidden = true;
  loadStats();
};

$("#resetAllBtn").onclick = async () => {
  if (!confirm("Reset ALL check-ins? This cannot be undone.")) return;
  const r = await (await fetch("/api/admin/reset_all", { method: "POST" })).json();
  toast(`Reset ${r.reset} check-ins`, "warn");
  loadStats();
};

/* ---------- QR scanning ---------- */
$("#scanBtn").onclick = async () => {
  const video = $("#scanVideo");
  const status = $("#scanStatus");
  if (scanStream) return stopScan();
  if (!("BarcodeDetector" in window)) {
    status.textContent = "QR scanning not supported on this browser — use manual search below.";
    return;
  }
  try {
    scanStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    video.srcObject = scanStream;
    video.hidden = false;
    await video.play();
    status.textContent = "Point camera at guest QR code…";
    $("#scanBtn").textContent = "Stop scanning";
    const detector = new BarcodeDetector({ formats: ["qr_code"] });
    const tick = async () => {
      if (!scanStream) return;
      try {
        const codes = await detector.detect(video);
        if (codes.length) {
          stopScan();
          resolveToken(codes[0].rawValue);
          return;
        }
      } catch (e) {}
      scanStream._t = requestAnimationFrame(tick);
    };
    tick();
  } catch (e) {
    status.textContent = "Camera unavailable (" + e.message + ") — use manual search.";
    stopScan();
  }
};

function stopScan() {
  if (!scanStream) return;
  scanStream.getTracks().forEach((t) => t.stop());
  cancelAnimationFrame(scanStream._t);
  scanStream = null;
  $("#scanVideo").hidden = true;
  $("#scanBtn").textContent = "📷 Scan QR";
  $("#scanStatus").textContent = "";
}

/* ---------- init ---------- */
loadStats();
searchInput.focus();
