// The library page: hand files to Alfred, watch them being read, answer his questions
// about large scans, delete them. Uploads go in 8 MB pieces: a dropped connection carries
// on from the last piece. Status comes from /library.json, so the page never reloads.
const tray = document.getElementById("tray");
const picker = document.getElementById("picker");
const folder = document.getElementById("folder");
const list = document.getElementById("files");
const uploads = document.getElementById("uploads");
const spaceLine = document.getElementById("space");
const LIMIT = Number(tray.dataset.limit);
const PIECE = 8 * 1024 * 1024;
const AT_ONCE = 2;
let known = []; // the files the library has, from the last refresh

const text = (tag, value, cls) => {
  const el = document.createElement(tag);
  el.textContent = value;
  if (cls) el.className = cls;
  return el;
};
const size = (bytes) =>
  bytes < 1048576
    ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : bytes < 1073741824
      ? `${(bytes / 1048576).toFixed(1)} MB`
      : `${(bytes / 1073741824).toFixed(1)} GB`;
const pause = (ms) => new Promise((done) => setTimeout(done, ms));
// Library actions carry this header: another site's page can't send it, so can't use them.
const post = (url, body) => fetch(url, { method: "POST", body, headers: { "X-Alfred": "1" } });
// A file's name with the folders it came in ("reports/q3.pdf"), as a zip's files are named.
const nameOf = (file) => file.alfredPath || file.webkitRelativePath || file.name;
const newId = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/[^a-z0-9-]/gi, "").toLowerCase();

function bar() {
  const outer = document.createElement("div");
  outer.className = "progress";
  outer.append(document.createElement("span"));
  return outer;
}

// One file, in pieces. Each piece is retried with growing pauses; the server says how much
// it already has, so a piece lost or sent twice never corrupts the file.
async function upload(file, replace) {
  const row = document.createElement("li");
  const meta = text("div", `Waiting to upload, ${size(file.size)}`, "meta");
  const progress = bar();
  row.append(text("div", nameOf(file), "name"), meta, progress);
  uploads.append(row);
  if (file.size > LIMIT && !file.name.toLowerCase().endsWith(".zip")) {
    meta.textContent = `Too large: the limit is ${size(LIMIT)}.`;
    progress.remove();
    return;
  }
  const id = newId();
  let have = 0;
  let tries = 0;
  while (true) {
    const piece = file.slice(have, Math.min(have + PIECE, file.size));
    const params = new URLSearchParams({ id, name: nameOf(file), size: file.size, offset: have, replace: replace ? "1" : "0" });
    let reply;
    try {
      const response = await post(`/library/upload?${params}`, piece);
      reply = await response.json();
      if (!response.ok && response.status !== 409) {
        meta.textContent = reply.error || "The upload was refused.";
        progress.remove();
        return;
      }
      tries = 0;
    } catch {
      tries += 1;
      if (tries > 8) {
        meta.textContent = "The upload stopped: check that Alfred is running, then try again.";
        return;
      }
      meta.textContent = `Connection lost, trying again (${tries})...`;
      await pause(Math.min(30000, 1000 * 2 ** tries));
      continue;
    }
    have = reply.have;
    progress.firstChild.style.width = `${(100 * have) / Math.max(file.size, 1)}%`;
    meta.textContent = `Uploading, ${size(have)} of ${size(file.size)}`;
    if (reply.done) {
      const said = { added: "Added", replaced: "Replaced the old version", "already there": "Already in the library", exists: "Not uploaded: a file with this name exists" };
      meta.textContent = said[reply.done] || reply.done;
      progress.remove();
      setTimeout(() => row.remove(), reply.done in said && reply.done !== "exists" ? 2500 : 12000);
      refresh();
      return;
    }
  }
}

async function send(files) {
  const queue = [...files].filter((f) => f.size > 0 || f.name);
  const names = new Set(known.map((d) => d.document.toLowerCase()));
  const clashes = queue.filter((f) => names.has(nameOf(f).toLowerCase()));
  let replace = false;
  if (clashes.length) {
    const shown = clashes.slice(0, 5).map(nameOf).join(", ") + (clashes.length > 5 ? ", ..." : "");
    replace = confirm(`Replace the library's ${shown} with the new version${clashes.length > 1 ? "s" : ""}? Identical files are skipped either way.`);
  }
  const workers = Array.from({ length: AT_ONCE }, async () => {
    while (queue.length) {
      const file = queue.shift();
      const clash = names.has(nameOf(file).toLowerCase());
      if (clash && !replace) continue;
      await upload(file, clash && replace);
    }
  });
  await Promise.all(workers);
}

// A dropped folder: every file in it, however deep.
async function filesIn(entry) {
  if (entry.isFile) {
    const file = await new Promise((done, fail) => entry.file(done, fail));
    file.alfredPath = entry.fullPath.replace(/^\//, "");
    return [file];
  }
  const reader = entry.createReader();
  const found = [];
  while (true) {
    const batch = await new Promise((done, fail) => reader.readEntries(done, fail));
    if (!batch.length) break;
    for (const child of batch) if (!child.name.startsWith(".")) found.push(...(await filesIn(child)));
  }
  return found;
}

const statusWords = {
  waiting: (d) => (d.queue ? `In the queue, ${d.queue === 1 ? "next" : `number ${d.queue}`}` : "Waiting"),
  processing: (d) => (d.total ? `Reading ${d.done} of ${d.total}` : `Reading, ${d.done} parts so far`),
  asking: () => "Needs your yes",
  paused: (d) => `Paused at ${d.done} of ${d.total}`,
  ready: () => "Ready",
  failed: () => "Couldn't read it",
};

function render(docs) {
  list.replaceChildren();
  if (!docs.length) {
    list.append(text("li", "Nothing here yet. Hand Alfred a file above.", "empty"));
    return;
  }
  for (const doc of docs) {
    const row = document.createElement("li");
    const name = document.createElement("div");
    name.className = "name";
    name.append(text("span", doc.kind, "kind"));
    const link = text("a", doc.document);
    link.href = `/library/files/${doc.id}`;
    name.append(link);
    const meta = [size(doc.bytes), doc.parts, `added ${doc.added}`].filter(Boolean).join(", ");
    row.append(name, text("div", meta, "meta"));
    if ((doc.status === "processing" || doc.status === "paused") && doc.total) {
      const progress = bar();
      progress.firstChild.style.width = `${(100 * doc.done) / doc.total}%`;
      row.append(progress);
    }
    if (doc.note) row.append(text("div", doc.note, "note"));
    const side = document.createElement("div");
    side.className = "side";
    side.append(text("span", (statusWords[doc.status] || (() => doc.status))(doc), `status ${doc.status}`));
    if (doc.status === "asking") {
      const yes = text("button", "Read it", "primary");
      yes.onclick = async () => {
        await post(`/library/approve?id=${doc.id}`);
        refresh();
      };
      side.append(yes);
    }
    const remove = text("button", "Delete", "danger");
    remove.onclick = async () => {
      if (!confirm(`Delete ${doc.document}? Alfred will no longer answer from it.`)) return;
      await post(`/library/delete?id=${doc.id}`);
      refresh();
    };
    side.append(remove);
    row.append(side);
    list.append(row);
  }
}

function showSpace({ used, limit, free }, keepFree) {
  const room = Math.max(0, Math.min(limit - used, free - keepFree));
  spaceLine.textContent = `${size(used)} of ${size(limit)} used; room for ${size(room)} more.`;
}

let timer;
async function refresh() {
  clearTimeout(timer);
  try {
    const reply = await (await fetch("/library.json")).json();
    known = reply.documents;
    render(known);
    showSpace(reply.space, reply.keep_free);
    document.getElementById("clear").hidden = !known.length;
    const busy = known.some((d) => ["waiting", "processing"].includes(d.status));
    timer = setTimeout(refresh, busy ? 1500 : 15000);
  } catch {
    timer = setTimeout(refresh, 5000);
  }
}

document.getElementById("clear").onclick = async () => {
  if (!confirm(`Delete all ${known.length} files? Alfred will no longer answer from them.`)) return;
  await post("/library/clear");
  refresh();
};
tray.onclick = () => picker.click();
document.getElementById("pick-folder").onclick = () => folder.click();
picker.onchange = () => send(picker.files).then(() => (picker.value = ""));
folder.onchange = () => send(folder.files).then(() => (folder.value = ""));
tray.ondragover = (e) => {
  e.preventDefault();
  tray.classList.add("over");
};
tray.ondragleave = () => tray.classList.remove("over");
tray.ondrop = async (e) => {
  e.preventDefault();
  tray.classList.remove("over");
  const entries = [...e.dataTransfer.items].map((item) => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);
  const files = entries.length ? (await Promise.all(entries.map(filesIn))).flat() : [...e.dataTransfer.files];
  send(files);
};
refresh();
