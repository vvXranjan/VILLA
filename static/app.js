const $ = (s) => document.querySelector(s);
let stagedToken = null;
let scanStream = null;
let arrivedData = [];
let pendingData = [];

/* =========================== navigation =========================== */

function showTab(name) {
  document.querySelectorAll("[data-tab]").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name)
  );
  document.querySelectorAll(".tab").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + name)
  );
  if (name === "overview") loadActivity();
  if (name === "arrived") loadArrived();
  if (name === "pending") loadPending();
  if (name === "review") loadReview();
  if (name === "checkin") setTimeout(() => $("#searchInput").focus(), 50);
}

document.querySelectorAll("[data-tab]").forEach((b) =>
  b.addEventListener("click", () => showTab(b.dataset.tab))
);

const openGateBtn = $("#openGateBtn");
if (openGateBtn) openGateBtn.onclick = () => showTab("checkin");

/* ============================== stats ============================== */

async function loadStats() {
  try {
    const s = await (await fetch("/api/stats")).json();

    setText("#kpiTotal", s.total);
    setText("#kpiPaid", s.paid);
    setText("#kpiChecked", s.checked_in);
    setText("#kpiPending", s.pending_total);

    const total = s.total || 0;
    const pct = (n) => (total ? (100 * n) / total : 0);
    $("#barPaid").style.width = pct(s.paid) + "%";
    $("#barUnpaid").style.width = pct(s.unpaid) + "%";
    $("#barReview").style.width = pct(s.under_review) + "%";
    setText("#legPaid", s.paid);
    setText("#legUnpaid", s.unpaid);
    setText("#legReview", s.under_review);

    $("#statsBar").innerHTML =
      `${s.registrations} registrations · <b>${s.total}</b> named guests · ` +
      `capacity <b>${s.expected_capacity}</b> · ` +
      `<span class="s-paid">${s.paid} paid</span> · ` +
      `<span class="s-unpaid">${s.unpaid} unpaid</span> · ` +
      `<span class="s-review">${s.under_review} under review</span> · ` +
      `<b>${s.checked_in}</b> checked in · <b>${s.paid_remaining}</b> paid remaining` +
      (s.incomplete_groups
        ? ` · <span class="s-review">${s.incomplete_groups} incomplete groups</span>`
        : "");

    const live = $("#gateLive");
    live.classList.remove("offline");
    $("#gateLiveText").textContent = "Gate live";
  } catch (e) {
    const live = $("#gateLive");
    live.classList.add("offline");
    $("#gateLiveText").textContent = "Server offline";
  }
}

function setText(sel, val) {
  const el = $(sel);
  if (el) el.textContent = val;
}

setInterval(loadStats, 5000);

/* =============================== toast =============================== */

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

/* ===================== gate activity (overview) ===================== */

async function loadActivity() {
  const box = $("#activityList");
  if (!box) return;
  try {
    const res = await (await fetch("/api/list/arrived")).json();
    const recent = res.slice().reverse().slice(0, 8);
    if (!recent.length) {
      box.innerHTML = '<div class="empty">The gate is quiet — no check-ins yet.</div>';
      return;
    }
    box.innerHTML =
      `<div class="arow ahead"><span>Guest</span><span>Pass</span><span>Payment</span><span>Time</span><span>Status</span></div>` +
      recent
        .map(
          (g) => `
      <div class="arow">
        <span class="a-guest"><span class="a-name">${esc(g.name)}</span><br><span class="a-sub">${esc(g.reg_id)}</span></span>
        <span class="a-pass">${esc(g.entry_pass || "—")}</span>
        <span class="a-pay">${payBadge(g.payment_status)}</span>
        <span class="a-time">${esc((g.checked_in_at || "").slice(11, 16))} UTC</span>
        <span class="a-status">Checked in ✓</span>
      </div>`
        )
        .join("");
  } catch (e) {
    box.innerHTML = '<div class="empty">Activity unavailable.</div>';
  }
}

/* ====================== search (grouped by reg) ====================== */

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
    box.innerHTML = '<div class="reg-card"><div class="empty">No guests found for “' + esc(q) + '”.</div></div>';
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
        <div class="reg-title">
          ${esc(r.reg_id)}
          <span class="pass-chip">${esc(pass)}</span>
          <span class="member-count">${inCount}/${r.members.length} entered</span>
        </div>
        <div class="reg-meta">${esc(r.phone || "no phone")}${r.email ? " · " + esc(r.email) : ""}</div>
      </div>
      <div>${payBadge(r.payment_status)}</div>
    </div>`;
  if (r.needs_review) {
    const expected = r.expected_capacity || r.members.length;
    const named = r.members.length;
    const missing = Math.max(0, expected - named);
    head += `
      <div class="review-flag">
        ⚠ <strong>INCOMPLETE GROUP DETAILS</strong> — ${esc(pass)} expects ${expected}, ${named} named
        ${missing ? `· ${missing} companion name(s) not supplied` : ""}
        <span class="review-sub">Named guests can still check in if PAID. Companions were never invented.</span>
        <button class="btn secondary tiny add-companion" data-reg="${esc(r.reg_id)}">+ Add companion name (admin)</button>
      </div>`;
  }

  let members = "";
  r.members.forEach((m) => {
    const checked = !!m.checked_in_at;
    let action;
    if (checked) {
      action = `<div class="member-done">✓ ${esc(m.checked_in_at.slice(11, 16))} UTC</div>`;
    } else if (r.payment_status === "PAID") {
      action = `<button class="btn-checkin" data-gid="${m.id}">CHECK IN</button>`;
    } else {
      action = `<button class="btn-blocked" disabled>${r.payment_status === "UNPAID" ? "Not paid — blocked" : "Unverified — blocked"}</button>`;
    }
    const sub = checked
      ? "Entry recorded"
      : r.payment_status === "PAID"
        ? "Ready to enter"
        : r.payment_status === "UNPAID"
          ? "Payment pending — entry blocked"
          : "Payment unverified — entry blocked";
    members += `
      <div class="member${checked ? " checked" : ""}">
        <div class="member-info">
          <div class="member-name">${esc(m.name)}</div>
          <div class="member-sub">${sub}</div>
        </div>
        ${action}
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
  card.querySelectorAll("button.add-companion").forEach((b) => {
    b.onclick = async () => {
      const n = prompt(`Add companion name for ${b.dataset.reg}\n(leave blank to cancel — companions are never invented):`);
      if (!n || !n.trim()) return;
      try {
        const resp = await fetch(
          `/api/admin/add_companion?reg_id=${encodeURIComponent(b.dataset.reg)}&name=${encodeURIComponent(n.trim())}`,
          { method: "POST" }
        );
        const res = await resp.json();
        if (resp.ok && res.ok) {
          toast(`Added: ${res.added.join(", ")} (admin-entered)`, "ok");
          loadStats();
          refreshSearchOrList();
        } else {
          toast(res.detail || "Could not add name", "bad");
        }
      } catch (e) {
        toast("Could not add companion", "bad");
      }
    };
  });
  return card;
}

function refreshSearchOrList() {
  if (searchInput.value.trim()) doSearch();
  else if ($("#tab-pending").classList.contains("active")) loadPending();
  else if ($("#tab-arrived").classList.contains("active")) loadArrived();
  if ($("#tab-overview").classList.contains("active")) loadActivity();
}

/* ====================== QR resolve → reg card ====================== */

async function resolveToken(token) {
  try {
    const g = await (await fetch("/api/qr/" + encodeURIComponent(token))).json();
    const regs = await (await fetch("/api/registrations?q=" + encodeURIComponent(g.reg_id))).json();
    const box = $("#results");
    box.innerHTML = "";
    if (regs.length) box.appendChild(regCard(regs[0]));
    if (g.payment_status !== "PAID") toast("Payment not verified — entry blocked", "bad");
    else toast("QR valid — " + g.name, "ok");
  } catch {
    toast("INVALID QR TOKEN", "bad");
  }
}

/* ============================ list screens ============================ */

async function loadArrived() {
  arrivedData = await (await fetch("/api/list/arrived")).json();
  renderArrived();
}

function renderArrived() {
  const q = ($("#arrivedSearch").value || "").trim().toLowerCase();
  const rows = arrivedData.filter(
    (g) =>
      !q ||
      g.name.toLowerCase().includes(q) ||
      (g.reg_id || "").toLowerCase().includes(q) ||
      (g.phone || "").includes(q)
  );
  $("#arrivedList").innerHTML = rows.length
    ? `<div class="dtable">
        <div class="trow dthead"><span>Guest</span><span>Pass</span><span>Payment</span><span>Checked in</span></div>
        ${rows
          .map(
            (g) => `
        <div class="trow">
          <span class="c-guest">
            <span class="g-name">${esc(g.name)}</span>
            <span class="g-sub">${esc(g.reg_id)} · ${esc(g.phone || "no phone")}</span>
          </span>
          <span class="c-pass">${esc(g.entry_pass || "—")}</span>
          <span class="c-pay">${payBadge(g.payment_status)}</span>
          <span class="c-time">${esc((g.checked_in_at || "").slice(11, 16))} UTC</span>
        </div>`
          )
          .join("")}
      </div>`
    : '<div class="panel"><div class="empty">' + (arrivedData.length ? "No arrivals match that filter." : "No one has checked in yet.") + "</div></div>";
}

async function loadPending() {
  pendingData = await (await fetch("/api/list/pending")).json();
  renderPending();
}

function renderPending() {
  const q = ($("#pendingSearch").value || "").trim().toLowerCase();
  const rows = pendingData.filter(
    (g) =>
      !q ||
      g.name.toLowerCase().includes(q) ||
      (g.reg_id || "").toLowerCase().includes(q) ||
      (g.phone || "").includes(q)
  );
  $("#pendingList").innerHTML = rows.length
    ? `<div class="dtable">
        <div class="trow dthead"><span>Guest</span><span>Pass</span><span>Payment</span><span>Status</span></div>
        ${rows
          .map(
            (g) => `
        <div class="trow" data-search="${esc(g.name).replace(/"/g, "&quot;")}" style="cursor:pointer">
          <span class="c-guest">
            <span class="g-name">${esc(g.name)}</span>
            <span class="g-sub">${esc(g.reg_id)} · ${esc(g.phone || "no phone")}</span>
          </span>
          <span class="c-pass">${esc(g.entry_pass || "—")}</span>
          <span class="c-pay">${payBadge(g.payment_status)}</span>
          <span class="c-time">Not arrived</span>
        </div>`
          )
          .join("")}
      </div>`
    : '<div class="panel"><div class="empty">' + (pendingData.length ? "No pending guests match that filter." : "Everyone has arrived!") + "</div></div>";

  $("#pendingList").querySelectorAll("[data-search]").forEach((el) => {
    el.onclick = () => goSearch(el.dataset.search);
  });
}

$("#arrivedSearch").oninput = renderArrived;
$("#pendingSearch").oninput = renderPending;

/* ====================== payment review screen ====================== */

async function loadReview() {
  const box = $("#reviewList");
  try {
    const [manual, regs] = await Promise.all([
      fetch("/api/list/manual_review").then((r) => r.json()),
      fetch("/api/registrations?q=").then((r) => r.json()),
    ]);
    const unpaid = regs.filter((r) => r.payment_status === "UNPAID");
    const unknown = regs.filter((r) => r.payment_status === "UNDER REVIEW");
    const mismatch = manual;

    let html = "";
    const group = (title, list, cls, note, showAdd) => {
      if (!list.length) return "";
      let h = `<div class="group-head">${title} — ${list.length}</div>`;
      h += list
        .map(
          (r) => `
        <div class="row ${cls}" data-reg="${esc(r.reg_id)}">
          <div>
            <div class="name">${esc(r.reg_id)} · ${esc(r.entry_pass || "?")}${r.members.length > 1 ? ` · ${r.members.length} guests` : ""}</div>
            <div class="meta">${esc(note(r))}</div>
          </div>
          <div class="row-right">
            ${payBadge(r.payment_status)}
            ${
              showAdd && r.expected_capacity > r.named_count
                ? `<button class="link-add" data-add="${esc(r.reg_id)}">+ add companion</button>`
                : ""
            }
          </div>
        </div>`
        )
        .join("");
      return h;
    };

    html += group(
      "Unpaid — entry blocked",
      unpaid,
      "blocked",
      (r) => r.members.map((m) => m.name).join(", ")
    );
    html += group(
      "Unknown payment — entry blocked",
      unknown,
      "blocked",
      (r) => r.members.map((m) => m.name).join(", ")
    );
    html += group(
      "Incomplete group details — named guests can still enter if paid",
      mismatch,
      "",
      (r) => r.review_reason + " — " + r.members.map((m) => m.name).join(", "),
      true
    );

    box.innerHTML = html || '<div class="panel"><div class="empty">Nothing needs review — all registrations are paid and complete. ✓</div></div>';

    box.querySelectorAll("[data-reg]").forEach((el) => {
      el.onclick = () => goSearch(el.dataset.reg);
    });
    box.querySelectorAll("[data-add]").forEach((b) => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        const regId = b.dataset.add;
        const n = prompt(`Add companion name for ${regId} (never invented automatically):`);
        if (!n || !n.trim()) return;
        try {
          const resp = await fetch(
            `/api/admin/add_companion?reg_id=${encodeURIComponent(regId)}&name=${encodeURIComponent(n.trim())}`,
            { method: "POST" }
          );
          const res = await resp.json();
          if (resp.ok && res.ok) {
            toast(`Added: ${res.added.join(", ")} (admin-entered)`, "ok");
            loadStats();
            loadReview();
          } else {
            toast(res.detail || "Could not add name", "bad");
          }
        } catch (e) {
          toast("Could not add companion", "bad");
        }
      };
    });
  } catch (e) {
    box.innerHTML = '<div class="panel"><div class="empty">Review list unavailable.</div></div>';
  }
}

window.goSearch = (q) => {
  showTab("checkin");
  searchInput.value = q;
  doSearch();
};

/* ====================== import: preview → confirm ====================== */

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
    <h3>Preview — ${esc(p.file_name)}</h3>
    <table class="pv">
      <tr><td>Registration rows (non-empty)</td><td><b>${p.registration_rows}</b></td></tr>
      <tr><td>Blank rows skipped</td><td><b>${p.blank_rows_skipped}</b></td></tr>
      <tr><td>Individual named guests</td><td><b>${p.individual_guests}</b></td></tr>
      <tr><td>Expected pass capacity</td><td><b>${p.expected_capacity}</b></td></tr>
      <tr><td>Solo / Couple / Duo / Trio</td><td><b>${p.solo} / ${p.couple} / ${p.duo} / ${p.trio}</b></td></tr>
      <tr><td>Other pass types</td><td><b>${p.other_pass}</b></td></tr>
      <tr><td>Paid registrations</td><td><b class="s-paid">${p.paid_regs}</b></td></tr>
      <tr><td>Unpaid registrations</td><td><b class="s-unpaid">${p.unpaid_regs}</b></td></tr>
      <tr><td>Payment review registrations</td><td><b class="s-review">${p.review_regs}</b></td></tr>
      <tr><td>Incomplete group registrations</td><td><b>${p.incomplete_groups}</b></td></tr>
    </table>
    ${p.aadhaar_columns_ignored.length ? `<div class="hint">Aadhaar columns ignored: ${esc(p.aadhaar_columns_ignored.join(", "))} (never stored)</div>` : ""}
    ${p.duplicate_file_warning ? `<div class="review-flag">⚠ ${esc(p.duplicate_file_warning)}</div>` : ""}
    ${cases || '<div class="hint">No manual-review cases.</div>'}
    <h3>First registrations</h3>
    ${p.sample_guests.map((s) => `<div class="hint">${esc(s.reg_id)} · ${esc(s.entry_pass)} · ${esc(s.payment_status)} — ${esc(s.names.join(", "))}</div>`).join("")}
  `;
  $("#wipeRow").hidden = false;
  $("#confirmBtn").hidden = false;
  $("#previewBox").scrollIntoView({ behavior: "smooth", block: "start" });
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

/* ============================ QR scanning ============================ */

$("#scanBtn").onclick = async () => {
  const video = $("#scanVideo");
  const status = $("#scanStatus");
  if (scanStream) return stopScan();
  if (!("BarcodeDetector" in window)) {
    status.textContent = "QR scanning not supported on this browser — use manual search above.";
    return;
  }
  try {
    scanStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    video.srcObject = scanStream;
    video.hidden = false;
    await video.play();
    status.textContent = "Point camera at guest QR code…";
    $("#scanBtn").innerHTML = "Stop scanning";
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
  $("#scanBtn").innerHTML =
    '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M14 14h3v3h-3zM20 14h1M14 20h3M20 17v4"/></svg> Scan QR code';
  $("#scanStatus").textContent = "";
}

/* =============================== init =============================== */
loadStats();
searchInput.focus();
