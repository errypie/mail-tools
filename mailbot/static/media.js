// GigaMail: раздел «Вложения и ссылки» — фото, файлы, архивы и ссылки из всей почты, как медиа чата в Телеграме.
// Подключается к странице сервером после основного скрипта и пользуется его функциями (el, api, ICON, openViewer…).
(function () {
  const css = `
.mdView { flex-direction: column; min-height: 0; }
.mdTop { padding: 14px 20px 0; border-bottom: 1px solid var(--border); }
.mdTop .row1 { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.mdTop h2 { margin: 0; font-size: 19px; flex: 1; min-width: 180px; }
.mdTop input[type=search] { width: 300px; max-width: 100%; }
.mdScope { display: inline-flex; align-items: center; gap: 6px; background: var(--select); border-radius: 14px; padding: 3px 4px 3px 10px; font-size: 13px; max-width: 100%; }
.mdScope span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 420px; }
.mdScope button { border: 0; background: none; color: var(--muted); cursor: pointer; font-size: 15px; line-height: 1; padding: 2px 6px; border-radius: 10px; }
.mdScope button:hover { background: var(--panel); color: var(--text); }
.mdTabs { display: flex; gap: 2px; margin-top: 10px; overflow-x: auto; }
.mdTabs button { border: 0; background: none; color: var(--muted); padding: 9px 14px; font-size: 14px; cursor: pointer; border-bottom: 2px solid transparent; white-space: nowrap; }
.mdTabs button.on { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
.mdTabs button b { font-weight: 500; font-size: 12px; color: var(--muted); margin-left: 4px; }
.mdTabs .sp { flex: 1; }
.mdBody { flex: 1; overflow-y: auto; padding: 4px 20px 30px; }
.mdBody > * { max-width: 1100px; }
.mdMonth { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .03em; margin: 18px 2px 8px; }
.mdGrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 6px; }
.mdTile { position: relative; aspect-ratio: 1; border-radius: 8px; overflow: hidden; background: var(--bg); cursor: pointer; border: 0; padding: 0; }
.mdTile img { width: 100%; height: 100%; object-fit: cover; display: block; }
.mdTile .cap { position: absolute; left: 0; right: 0; bottom: 0; padding: 14px 8px 6px; font-size: 11.5px; color: #fff; background: linear-gradient(transparent, rgba(0,0,0,.65)); text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; opacity: 0; transition: opacity .15s; }
.mdTile:hover .cap, .mdTile:focus-visible .cap { opacity: 1; }
.mdTile .ph { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); font-size: 12px; }
.mdRow { display: flex; gap: 12px; align-items: flex-start; padding: 10px 8px; border-radius: 10px; }
.mdRow:hover { background: var(--bg); }
.mdRow .attIcon { margin-top: 2px; cursor: pointer; }
.mdRow .main { flex: 1; min-width: 0; }
.mdRow .nm { font-size: 14.5px; font-weight: 500; overflow-wrap: anywhere; cursor: pointer; color: var(--text); text-decoration: none; }
.mdRow a.nm:hover, .mdRow .nm:hover { color: var(--accent); text-decoration: underline; }
.mdRow .url { font-size: 12.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mdRow .ctx { font-size: 13.5px; margin-top: 3px; color: var(--text); opacity: .85; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.mdRow .meta { font-size: 12.5px; color: var(--muted); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mdRow .acts { display: flex; gap: 2px; flex-shrink: 0; }
.mdLinkIco { width: 34px; height: 34px; border-radius: 8px; display: grid; place-items: center; color: #fff; font-weight: 700; font-size: 15px; flex-shrink: 0; margin-top: 2px; text-transform: uppercase; }
.mdMore { margin: 16px auto 0; display: block; }
.mdRow .ai { font-size: 13.5px; margin-top: 3px; overflow-wrap: anywhere; }
.mdRow .ai::before { content: "✦ "; color: var(--accent); }
.mdProg { font-size: 12.5px; color: var(--muted); margin: 10px 0 0; }
.mdCopiesBtn { border: 0; background: var(--select); color: var(--text); border-radius: 10px; padding: 1px 8px; font-size: 12px; cursor: pointer; margin-left: 6px; }
.mdCopiesBtn:hover { color: var(--accent); }
.mdCopies { margin: 6px 0 2px; border-left: 2px solid var(--border); padding-left: 10px; display: grid; gap: 2px; }
.mdCopies .cp { display: flex; gap: 8px; align-items: baseline; font-size: 13px; padding: 3px 6px; border-radius: 6px; cursor: pointer; min-width: 0; }
.mdCopies .cp:hover { background: var(--panel); }
.mdCopies .cp .w { color: var(--muted); white-space: nowrap; }
.mdCopies .cp .s { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.mdCopies .cp .n { color: var(--muted); font-size: 12px; white-space: nowrap; }
.mdTile .cnt { position: absolute; top: 6px; right: 6px; background: rgba(0,0,0,.6); color: #fff; font-size: 11.5px; border-radius: 10px; padding: 1px 7px; }
@media (max-width: 760px) { .mdTop, .mdBody { padding-left: 12px; padding-right: 12px; } .mdGrid { grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); } .mdRow .acts .btn { display: none; } }
`;
  document.head.appendChild(Object.assign(document.createElement("style"), { textContent: css }));
})();

const MD_KINDS = [["photo", "Фото"], ["file", "Файлы"], ["archive", "Архивы"], ["link", "Ссылки"]];
const md = { kind: null, q: "", scope: null, items: [], total: 0, counts: {}, excluded: [], seq: 0, timer: null };
const MD_PAGE = 60;
const MD_COLORS = ["#2563eb", "#7c3aed", "#0891b2", "#16a34a", "#ea580c", "#db2777", "#4f46e5", "#0d9488"];

// вызывается из showView основного скрипта
function mediaView(view) {
  let box = $("mediaView");
  if (view !== "media") { if (box) box.hidden = true; return; }
  if (!box) { box = el("div", "offView mdView"); box.id = "mediaView"; $("calView").after(box); buildMedia(box); }
  box.hidden = false;
  loadMedia();
}
// открыть раздел сразу с отбором: {conv, sender, label}
function openMedia(scope) {
  md.scope = scope || null; md.kind = null; md.q = "";
  if ($("mdSearch")) $("mdSearch").value = "";
  showView("media");
}

function buildMedia(box) {
  const top = el("div", "mdTop"), row = el("div", "row1"), search = el("input");
  search.type = "search"; search.id = "mdSearch"; search.placeholder = "Имя файла, ссылка, отправитель, слова из письма";
  search.setAttribute("aria-label", "Поиск по вложениям и ссылкам");
  search.oninput = () => { clearTimeout(md.timer); md.timer = setTimeout(() => { md.q = search.value.trim(); loadMedia(); }, 250); };
  const scope = el("div", "mdScope"); scope.id = "mdScope"; scope.hidden = true;
  row.append(el("h2", "", "Вложения и ссылки"), scope, search);
  const tabs = el("div", "mdTabs"); tabs.id = "mdTabs";
  top.append(row, tabs);
  const body = el("div", "mdBody"); body.id = "mdBody";
  box.append(top, body);
}

async function loadMedia(more) {
  const seq = ++md.seq, kind = md.kind || "photo";
  const params = new URLSearchParams({ kind, limit: MD_PAGE, offset: more ? md.items.length : 0 });
  if (md.q) params.set("q", md.q);
  if (md.scope && md.scope.conv) params.set("conv", md.scope.conv);
  if (md.scope && md.scope.sender) params.set("sender", md.scope.sender);
  let d;
  try { d = await api("GET", "/api/media?" + params); }
  catch (e) { if (seq === md.seq) $("mdBody").replaceChildren(el("div", "empty", e.message)); return; }
  if (seq !== md.seq) return;
  // при первом открытии — первая непустая вкладка, как в Телеграме
  if (!md.kind) {
    const first = MD_KINDS.find(([k]) => d.counts[k]);
    md.kind = first ? first[0] : "photo";
    if (md.kind !== kind) return loadMedia();
  }
  md.items = more ? md.items.concat(d.items) : d.items;
  md.total = d.total; md.counts = d.counts; md.excluded = d.excluded || []; md.backfill = d.backfill;
  renderMedia(d.pending);
}

function renderMedia(pending) {
  const scope = $("mdScope");
  scope.hidden = !md.scope;
  if (md.scope) {
    const x = el("button", "", "×"); x.title = "Показать всю почту"; x.setAttribute("aria-label", "Убрать отбор");
    x.onclick = () => { md.scope = null; md.kind = null; loadMedia(); };
    scope.replaceChildren(el("span", "", md.scope.label), x);
  }
  const tabs = $("mdTabs"); tabs.textContent = "";
  MD_KINDS.forEach(([k, t]) => {
    const b = el("button", k === md.kind ? "on" : "", t);
    b.appendChild(el("b", "", String(md.counts[k] || 0)));
    b.onclick = () => { if (md.kind !== k) { md.kind = k; md.items = []; loadMedia(); } };
    tabs.appendChild(b);
  });
  tabs.appendChild(el("span", "sp"));
  const cfg = el("button", "", "Серый список");
  cfg.title = "Отправители, чьи вложения и ссылки не собираются, и скрытые домены ссылок — в настройках";
  cfg.onclick = () => openSettings().then(() => showSetSection("senders"));
  tabs.appendChild(cfg);
  const body = $("mdBody"), keep = body.scrollTop; body.textContent = "";
  if (pending) body.appendChild(el("div", "hint", "Разбираю письма: осталось " + pending + ". Список дополнится сам."));
  const bf = md.backfill;
  if (bf && bf.enabled && (bf.running || bf.error)) {
    body.appendChild(el("div", "mdProg", bf.error ? "Подгрузка старой почты остановилась: " + bf.error
      : "Подгружаю старую почту за " + bf.days + " дн.: папка «" + bf.folder + "», готово папок " + bf.done_folders + " из " + bf.folders
        + (bf.oldest ? ", дошёл до " + mdDate(bf.oldest) : "") + "."));
  }
  if (!md.items.length) {
    body.appendChild(el("div", "empty", md.q || md.scope ? "Ничего не нашлось" : {
      photo: "Фотографий в письмах пока нет", file: "Файлов в письмах пока нет",
      archive: "Архивов в письмах пока нет", link: "Ссылок в письмах пока нет" }[md.kind]));
  }
  let month = "", grid = null;
  md.items.forEach((it) => {
    const d = new Date(it.received_at), m = MONTHS[d.getMonth()] + " " + d.getFullYear();
    if (m !== month) {
      month = m; body.appendChild(el("div", "mdMonth", m));
      grid = md.kind === "photo" ? body.appendChild(el("div", "mdGrid")) : null;
    }
    if (md.kind === "photo") grid.appendChild(photoTile(it));
    else body.appendChild(md.kind === "link" ? linkRow(it) : fileRow(it));
  });
  if (md.items.length < md.total) {
    const b = el("button", "btn mdMore", "Показать ещё (" + (md.total - md.items.length) + ")");
    b.onclick = () => { b.disabled = true; loadMedia(true); };
    body.appendChild(b);
  }
  body.scrollTop = keep;
  if (pending || (bf && bf.running)) setTimeout(() => { if (!$("mediaView").hidden && md.items.length <= MD_PAGE) loadMedia(); }, pending ? 3000 : 15000);
}

const mdDate = (iso) => new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: "numeric" });
const mdMeta = (it, extra) => [it.sender_name || it.sender, mdDate(it.received_at), extra].filter(Boolean).join(" · ");
function openMail(it) { showView("mail"); select(it.email_id); }
function mailBtn(it) {
  const b = el("button", "btn small ghost", "К письму");
  b.title = it.subject || "(без темы)";
  b.onclick = (e) => { e.stopPropagation(); openMail(it); };
  return b;
}
const asAtt = (it) => ({ name: it.name, content_type: it.content_type, size: it.size });

function photoTile(it) {
  const t = el("button", "mdTile"), img = el("img");
  t.title = it.name + (it.description ? "\n" + it.description : it.context ? "\n" + it.context : "") + "\n" + mdMeta(it);
  img.loading = "lazy"; img.decoding = "async"; img.alt = it.name;
  img.src = attUrl(it.email_id, it.att_index, true);
  img.onerror = () => { img.remove(); t.prepend(el("span", "ph", it.name)); };
  t.append(img, el("span", "cap", it.sender_name || it.name));
  if (it.copies > 1) { const c = el("span", "cnt", "×" + it.copies); c.title = "Встречается в " + it.copies + " письмах"; t.appendChild(c); }
  t.onclick = () => openViewer({ id: it.email_id }, asAtt(it), it.att_index);
  t.oncontextmenu = (e) => { e.preventDefault(); openMail(it); };
  return t;
}

function fileRow(it) {
  const r = el("div", "mdRow"), ext = (it.name.includes(".") ? it.name.split(".").pop() : "").slice(0, 4).toUpperCase();
  const icon = el("span", "attIcon " + attKind(asAtt(it)), ext || "FILE");
  const main = el("div", "main"), nm = el("div", "nm", it.name);
  const open = () => openViewer({ id: it.email_id }, asAtt(it), it.att_index);
  icon.onclick = open; nm.onclick = open; nm.title = "Открыть";
  main.appendChild(nm);
  if (it.description) main.appendChild(el("div", "ai", it.description));
  const ctx = el("div", "ctx", it.context || "Тема: " + (it.subject || "(без темы)"));
  if (!it.description || it.context) main.appendChild(ctx);
  main.appendChild(el("div", "meta", mdMeta(it, fmtSize(it.size))));
  const acts = el("div", "acts"), dl = el("a", "iconBtn");
  dl.href = attUrl(it.email_id, it.att_index, false); dl.setAttribute("download", it.name); dl.title = "Скачать"; dl.innerHTML = ICON.down;
  acts.append(dl, mailBtn(it));
  copiesUi(it, main);
  r.append(icon, main, acts);
  return r;
}

function linkRow(it) {
  const r = el("div", "mdRow"), dom = it.domain || "";
  const hash = [...dom].reduce((a, c) => (a * 31 + c.codePointAt(0)) >>> 0, 0);
  const ico = el("span", "mdLinkIco", [...dom.replace(/^(m|mail|web|app)\./, "")][0] || "↗");
  ico.style.background = MD_COLORS[hash % MD_COLORS.length];
  let path = it.url.replace(/^https?:\/\//i, "");
  if (path.length > 90) path = path.slice(0, 87) + "…";
  const main = el("div", "main"), a = el("a", "nm", it.name || path);
  a.href = it.url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.title = it.url;
  main.append(a);
  if (it.name) main.appendChild(el("div", "url", path));
  if (it.description) main.appendChild(el("div", "ai", it.description));
  if (it.context && it.context !== it.name) {
    const ctx = main.appendChild(el("div", "ctx", it.context));
    if (it.description) ctx.style.opacity = ".65";
  }
  main.appendChild(el("div", "meta", mdMeta(it, it.subject ? "«" + it.subject + "»" : "")));
  const acts = el("div", "acts"), copy = el("button", "iconBtn");
  copy.title = "Скопировать ссылку"; copy.innerHTML = ICON.link;
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(it.url); toast("Ссылка скопирована"); } catch (e) { prompt("Ссылка:", it.url); }
  };
  acts.append(copy, mailBtn(it));
  copiesUi(it, main);
  r.append(ico, main, acts);
  return r;
}

// тот же файл или ссылка в нескольких письмах: «в N письмах», по щелчку — список писем
function copiesUi(it, main) {
  if (!(it.copies > 1)) return;
  const meta = main.querySelector(".meta"), b = el("button", "mdCopiesBtn", "в " + it.copies + " " + plural(it.copies, "письме", "письмах", "письмах"));
  b.title = "Показать письма, где " + (it.kind === "link" ? "есть эта ссылка" : "приложен этот файл");
  let box = null;
  b.onclick = async () => {
    if (box) { box.remove(); box = null; return; }
    box = el("div", "mdCopies"); box.appendChild(el("div", "hint", "Загружаю…")); main.appendChild(box);
    try {
      const d = await api("GET", "/api/media/copies?id=" + it.id), list = box;
      list.textContent = "";
      d.copies.forEach((c) => {
        const row = el("div", "cp"); row.tabIndex = 0; row.setAttribute("role", "button");
        row.append(el("span", "w", mdDate(c.received_at)), el("span", "w", c.sender_name || c.sender),
          el("span", "s", c.subject || "(без темы)"));
        if (it.kind !== "link" && c.name !== it.name) row.appendChild(el("span", "n", c.name));
        row.title = "Открыть письмо";
        const open = () => openMail({ email_id: c.email_id });
        row.onclick = open; row.onkeydown = (e) => { if (e.key === "Enter") open(); };
        list.appendChild(row);
      });
    } catch (e) { box.textContent = e.message; }
  };
  meta.appendChild(b);
}
