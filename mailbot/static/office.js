// GigaMail: уведомления, флажки, подписи, автоответ, правила, контакты и задачи.
// Подключается к странице сервером после основного скрипта и пользуется его функциями (el, api, toast, ICON…).
(function () {
  const css = `
.flagOn, .rowActs button.on { color: #d13438 !important; }
.flagDone { color: var(--ev-bar); }
.row .top .flagOn svg, .row .top .flagDone svg { width: 13px; height: 13px; }
.oofPill { background: #fff4ce; color: #6b4b00; border: 0; border-radius: 12px; padding: 4px 10px; font-size: 12.5px; cursor: pointer; display: inline-flex; gap: 6px; align-items: center; }
@media (prefers-color-scheme: dark) { .oofPill { background: #4a3b10; color: #ffe08a; } }
.sigMenu { position: fixed; z-index: 70; background: var(--panel); border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 10px 28px rgba(0,0,0,.18); padding: 4px; min-width: 220px; }
.sigMenu button { display: block; width: 100%; text-align: left; border: 0; background: none; padding: 7px 10px; border-radius: 7px; cursor: pointer; color: var(--text); font-size: 14px; }
.sigMenu button:hover { background: var(--bg); }
.sigMenu .hint { padding: 6px 10px; }
.offView { flex: 1; min-width: 0; display: flex; background: var(--panel); }
.offList { width: 360px; flex-shrink: 0; border-right: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0; }
.offHead { display: flex; gap: 8px; align-items: center; padding: 12px; border-bottom: 1px solid var(--border); }
.offHead h2 { margin: 0; font-size: 18px; flex: 1; }
.offSearch { margin: 10px 12px 4px; width: auto !important; }
.offItems { flex: 1; overflow-y: auto; padding: 4px 6px 12px; }
.offItem { display: flex; gap: 10px; align-items: center; padding: 8px 8px; border-radius: 9px; cursor: pointer; }
.offItem:hover { background: var(--bg); }
.offItem.on { background: var(--select); }
.offItem .nm { font-size: 14.5px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.offItem .sub { font-size: 12.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.offItem > div { min-width: 0; flex: 1; }
.offDetail { flex: 1; min-width: 0; overflow-y: auto; padding: 18px 24px 30px; }
.offDetail .wrap2 { max-width: 640px; }
.offDetail h2 { margin: 0 0 4px; font-size: 22px; }
.offGrid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 14px; }
.offRow { display: flex; gap: 10px; padding: 7px 0; font-size: 14.5px; align-items: center; }
.offRow .ico { color: var(--muted); }
.offGroup { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .03em; margin: 14px 8px 4px; }
.taskRow { display: flex; gap: 10px; align-items: center; padding: 8px 8px; border-radius: 9px; cursor: pointer; }
.taskRow:hover { background: var(--bg); }
.taskRow.on { background: var(--select); }
.taskRow .box { width: 20px; height: 20px; border-radius: 50%; border: 2px solid var(--muted); background: none; cursor: pointer; flex-shrink: 0; display: grid; place-items: center; color: #fff; padding: 0; }
.taskRow .box:hover { border-color: var(--accent); }
.taskRow.done .box { background: var(--ev-bar); border-color: var(--ev-bar); }
.taskRow.done .nm { text-decoration: line-through; color: var(--muted); }
.taskRow .nm { flex: 1; min-width: 0; font-size: 14.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.taskRow .due { font-size: 12.5px; color: var(--muted); white-space: nowrap; }
.taskRow .due.late { color: var(--danger); }
.taskRow .imp { color: #d13438; font-weight: 700; }
.taskAdd { display: flex; gap: 6px; padding: 10px 12px; border-bottom: 1px solid var(--border); }
.taskAdd input[type=text] { flex: 1; }
.taskAdd input[type=date] { width: 150px; }
.sigList, .ruleList { display: grid; gap: 6px; margin: 8px 0 12px; }
.sigItem, .ruleItem { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 10px; cursor: pointer; }
.sigItem:hover, .ruleItem:hover { border-color: var(--accent); }
.sigItem.on, .ruleItem.on { border-color: var(--accent); background: var(--select); }
.sigItem .nm, .ruleItem .nm { flex: 1; min-width: 0; }
.ruleItem .sub { font-size: 12.5px; color: var(--muted); }
.sigEditor { min-height: 110px; border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; background: var(--panel); outline: 0; }
.sigEditor:focus { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.offTabs { display: flex; gap: 6px; margin: 4px 0 12px; }
@media (max-width: 760px) { .offList { width: 100%; } .offView.detail .offList { display: none; } .offView:not(.detail) .offDetail { display: none; } .offGrid { grid-template-columns: 1fr; } }
`;
  document.head.appendChild(Object.assign(document.createElement("style"), { textContent: css }));
})();

// ---------- флажки и задачи из писем ----------
async function setFlag(id, flag) {
  try {
    await api("POST", "/api/emails/" + id + "/flag", { flag });
    toast(flag === 2 ? "Флажок поставлен" : flag === 1 ? "Отмечено как выполненное" : "Флажок снят");
  } catch (e) { alert(e.message); }
  listSig = ""; refresh(); if (typeof refreshDetail === "function") refreshDetail();
}
async function taskFromEmail(id) {
  try { await api("POST", "/api/emails/" + id + "/task"); toast("Задача создана — она в разделе «Задачи»"); office.tasks = null; }
  catch (e) { alert(e.message); }
}

// ---------- уведомления о новых письмах ----------
const office = { last: 0, tasks: null, people: null, started: false, oof: null };
const notifyOn = (kind) => stored("gm.notify." + kind) !== "0";
async function pollMail() {
  if ($("app").hidden) return;
  let r;
  try { r = await api("GET", "/api/notify?after=" + office.last); } catch (e) { return; }
  document.title = (r.unread ? "(" + r.unread + ") " : "") + "GigaMail";
  const fresh = office.last > 0 ? r.emails : [];
  office.last = r.last;
  if (!fresh.length || !notifyOn("mail")) return;
  const one = fresh.length === 1 ? fresh[0] : null;
  const title = one ? (one.sender_name || one.sender) : "Новых писем: " + fresh.length;
  const body = one ? (one.subject || "(без темы)") + (one.summary ? "\n" + one.summary : "") : fresh.slice(0, 3).map((m) => (m.sender_name || m.sender) + ": " + (m.subject || "(без темы)")).join("\n");
  if (window.Notification && Notification.permission === "granted" && document.hidden) {
    try {
      const n = new Notification(title, { body, tag: "gm-mail-" + r.last });
      n.onclick = () => { window.focus(); showView("mail"); if (one) select(one.id); n.close(); };
    } catch (e) { /* браузер может не дать показать */ }
  } else if (!document.hidden) {
    toast(one ? "Новое письмо: " + title + " — " + (one.subject || "(без темы)") : title);
  }
}

// автоответ включён — показываем это в шапке, как Outlook
async function checkOof() {
  try { office.oof = await api("GET", "/api/oof"); } catch (e) { office.oof = null; }
  let pill = $("oofPill");
  const on = office.oof && office.oof.state !== "Disabled";
  if (!on) { if (pill) pill.remove(); return; }
  if (!pill) {
    pill = el("button", "oofPill"); pill.id = "oofPill";
    pill.onclick = () => openSettings().then(() => showSetSection("oof")).catch((e) => alert(e.message));
    $("settingsBtn").before(pill);
  }
  pill.textContent = office.oof.state === "Scheduled" ? "Автоответ по расписанию" : "Автоответ включён";
  pill.title = "Открыть настройки автоответа";
}

function startOffice() {
  if (office.started) return;
  office.started = true;
  pollMail(); setInterval(pollMail, 15000);
  checkOof(); setInterval(checkOof, 5 * 60000);
  if (location.hash === "#settings-oof") openSettings().then(() => showSetSection("oof"));
}

// ---------- подписи в письме ----------
function signatureMenu(btn, editor, changed) {
  document.querySelectorAll(".sigMenu").forEach((m) => m.remove());
  const sel = window.getSelection(), range = sel.rangeCount && editor.contains(sel.anchorNode) ? sel.getRangeAt(0).cloneRange() : null;
  const menu = el("div", "sigMenu"), r = btn.getBoundingClientRect();
  menu.style.left = Math.min(r.left, innerWidth - 240) + "px"; menu.style.top = r.bottom + 4 + "px";
  menu.appendChild(el("div", "hint", "Загружаю подписи…"));
  document.body.appendChild(menu);
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener("mousedown", close, true); } };
  document.addEventListener("mousedown", close, true);
  api("GET", "/api/signatures").then((d) => {
    menu.textContent = "";
    d.signatures.forEach((s) => {
      const b = el("button", "", s.name); b.onmousedown = (e) => e.preventDefault();
      b.onclick = () => {
        editor.querySelectorAll("[data-gm-sig]").forEach((x) => x.remove());  // одна подпись на письмо, как в Outlook
        const box = document.createElement("div"); box.dataset.gmSig = "1"; box.appendChild(safeFragment(s.body_html));
        if (range) { range.collapse(false); range.insertNode(box); } else editor.appendChild(box);
        menu.remove(); changed();
      };
      menu.appendChild(b);
    });
    const manage = el("button", "", d.signatures.length ? "Настроить подписи…" : "Создать подпись…");
    manage.onclick = () => { menu.remove(); openSettings().then(() => showSetSection("signatures")); };
    if (!d.signatures.length) menu.appendChild(el("div", "hint", "Подписей пока нет"));
    menu.appendChild(manage);
  }).catch((e) => { menu.textContent = ""; menu.appendChild(el("div", "err", e.message)); });
}

// ---------- разделы «Контакты» и «Задачи» ----------
function officeView(view) {
  ["people", "tasks"].forEach((v) => {
    let box = $(v + "View");
    if (view !== v) { if (box) box.hidden = true; return; }
    if (!box) { box = el("div", "offView"); box.id = v + "View"; $("calView").after(box); }
    box.hidden = false;
    if (v === "people") renderPeople(); else renderTasks();
  });
}

// контакты
const pp = { q: "", sel: null };
async function renderPeople(reload) {
  const box = $("peopleView");
  if (!office.people || reload) {
    if (!box.childNodes.length) box.appendChild(el("div", "empty", "Загружаю контакты…"));
    try { office.people = (await api("GET", "/api/people")).contacts; }
    catch (e) { box.replaceChildren(el("div", "empty", e.message)); return; }
  }
  const prevScroll = box.querySelector(".offItems") ? box.querySelector(".offItems").scrollTop : 0;
  box.textContent = "";
  const list = el("div", "offList"), head = el("div", "offHead"), add = el("button", "btn small primary", "Новый контакт");
  head.append(el("h2", "", "Контакты"), add);
  add.onclick = () => editPerson(null);
  const q = el("input", "offSearch"); q.type = "search"; q.placeholder = "Поиск по имени, почте, компании"; q.value = pp.q;
  q.setAttribute("aria-label", "Поиск контактов");
  const items = el("div", "offItems");
  const draw = () => {
    items.textContent = "";
    const s = pp.q.toLowerCase();
    const found = office.people.filter((c) => !s || [c.display_name, c.company, c.job_title, ...c.emails].join(" ").toLowerCase().includes(s));
    if (!found.length) items.appendChild(el("div", "empty", office.people.length ? "Никого не нашли" : "Личных контактов пока нет"));
    let letter = "";
    found.forEach((c) => {
      const l = (c.display_name || "?")[0].toUpperCase();
      if (l !== letter) { letter = l; items.appendChild(el("div", "offGroup", l)); }
      const it = el("div", "offItem" + (pp.sel === c.id ? " on" : "")), txt = el("div");
      txt.append(el("div", "nm", c.display_name), el("div", "sub", [c.job_title, c.company].filter(Boolean).join(", ") || c.emails[0] || ""));
      it.append(avatar(c.display_name, c.emails[0] || c.display_name), txt);
      it.onclick = () => { pp.sel = c.id; showPerson(c.id); draw(); };
      items.appendChild(it);
    });
  };
  q.oninput = () => { pp.q = q.value; draw(); };
  list.append(head, q, items);
  const detail = el("div", "offDetail"); detail.id = "personDetail";
  box.append(list, detail);
  draw(); items.scrollTop = prevScroll;
  if (pp.sel) showPerson(pp.sel); else detail.appendChild(el("div", "empty", "Выберите контакт"));
}
async function showPerson(id) {
  const d = $("personDetail"); if (!d) return;
  $("peopleView").classList.add("detail");
  d.replaceChildren(el("div", "hint", "Загружаю…"));
  let c;
  try { c = await api("GET", "/api/people?id=" + encodeURIComponent(id)); } catch (e) { d.replaceChildren(el("div", "err", e.message)); return; }
  const w = el("div", "wrap2"), top = el("div", "offRow");
  const back = el("button", "btn small ghost", "← Список"); back.onclick = () => $("peopleView").classList.remove("detail");
  top.append(avatar(c.display_name, c.emails[0] || c.display_name, "big"));
  const nm = el("div"); nm.append(el("h2", "", c.display_name), el("div", "hint", [c.job_title, c.department, c.company].filter(Boolean).join(" · ")));
  top.appendChild(nm);
  w.append(top);
  const acts = el("div", "actions"), mail = el("button", "btn small primary", "Написать письмо"), edit = el("button", "btn small", "Изменить"), del = el("button", "btn small danger", "Удалить");
  mail.disabled = !c.emails.length;
  mail.onclick = () => mailTo([c.display_name + " <" + c.emails[0] + ">"]);
  edit.onclick = () => editPerson(c);
  del.onclick = async () => {
    if (!confirm("Удалить контакт «" + c.display_name + "»? Он попадёт в «Удалённые» Exchange.")) return;
    try { await api("POST", "/api/people/delete", { id: c.id }); pp.sel = null; toast("Контакт удалён"); renderPeople(true); } catch (e) { alert(e.message); }
  };
  acts.append(mail, edit, del);
  if (matchMedia("(max-width: 760px)").matches) acts.prepend(back);
  w.appendChild(acts);
  c.emails.forEach((e) => { const r = el("div", "offRow"); r.append(svgIcon("mail", "ico")); const a = el("a", "", e); a.href = "#"; a.onclick = (ev) => { ev.preventDefault(); mailTo([c.display_name + " <" + e + ">"]); }; r.appendChild(a); w.appendChild(r); });
  [["mobile", "мобильный"], ["work", "рабочий"], ["home", "домашний"]].forEach(([k, t]) => {
    if (!c.phones[k]) return;
    const r = el("div", "offRow"); r.append(svgIcon("user", "ico")); const a = el("a", "", c.phones[k]); a.href = "tel:" + c.phones[k].replace(/[^\d+]/g, "");
    r.append(a, el("span", "hint", t)); w.appendChild(r);
  });
  if (c.notes) w.appendChild(el("div", "epText", c.notes));
  d.replaceChildren(w);
}
async function mailTo(people) {
  try {
    const r = await api("POST", "/api/drafts", { kind: "new" });
    await api("PUT", "/api/drafts/" + r.draft.id, { to: people });
    showView("mail"); openDraft(r.draft.id);
  } catch (e) { alert(e.message); }
}
function editPerson(c) {
  const d = $("personDetail"); $("peopleView").classList.add("detail");
  c = c || { given_name: "", surname: "", display_name: "", company: "", job_title: "", department: "", emails: [], phones: {}, notes: "" };
  const w = el("div", "wrap2"), g = el("div", "offGrid");
  w.appendChild(el("h2", "", c.id ? "Изменить контакт" : "Новый контакт"));
  const f = {};
  const field = (key, label, value, box, type) => {
    const wrap = el("div"), l = el("label", "", label), i = el(type === "textarea" ? "textarea" : "input");
    i.id = "pc_" + key; l.htmlFor = i.id; i.value = value || ""; if (type && type !== "textarea") i.type = type;
    wrap.append(l, i); (box || g).appendChild(wrap); f[key] = i; return i;
  };
  field("given_name", "Имя", c.given_name); field("surname", "Фамилия", c.surname);
  field("company", "Компания", c.company); field("job_title", "Должность", c.job_title);
  field("department", "Отдел", c.department); field("display_name", "Как показывать", c.display_name);
  field("email1", "Почта", c.emails[0], null, "email"); field("email2", "Ещё почта", c.emails[1], null, "email");
  field("mobile", "Мобильный", c.phones.mobile, null, "tel"); field("work", "Рабочий телефон", c.phones.work, null, "tel");
  w.appendChild(g);
  field("notes", "Заметки", c.notes, w, "textarea").rows = 3;
  const err = el("div", "err"), acts = el("div", "actions"), save = el("button", "btn primary", "Сохранить"), cancel = el("button", "btn ghost", "Отмена");
  cancel.onclick = () => (c.id ? showPerson(c.id) : renderPeople());
  save.onclick = async () => {
    err.textContent = ""; save.disabled = true;
    const body = { id: c.id, given_name: f.given_name.value, surname: f.surname.value, company: f.company.value, job_title: f.job_title.value,
      department: f.department.value, display_name: f.display_name.value, emails: [f.email1.value, f.email2.value].filter((x) => x.trim()),
      phones: { mobile: f.mobile.value, work: f.work.value, home: (c.phones || {}).home || "" }, notes: f.notes.value };
    try { const r = await api("POST", "/api/people", body); pp.sel = r.id; toast("Контакт сохранён"); renderPeople(true); }
    catch (e) { err.textContent = e.message; save.disabled = false; }
  };
  acts.append(save, cancel); w.append(err, acts);
  d.replaceChildren(w); f.given_name.focus();
}

// задачи
const tk = { sel: null, showDone: false, flagged: [] };
const TASK_STATUS = { NotStarted: "Не начата", InProgress: "Выполняется", Completed: "Завершена", WaitingOnOthers: "Ждёт других", Deferred: "Отложена" };
async function renderTasks(reload) {
  const box = $("tasksView");
  if (!office.tasks || reload) {
    if (!box.childNodes.length) box.appendChild(el("div", "empty", "Загружаю задачи…"));
    try {
      const [t, fl] = await Promise.all([api("GET", "/api/tasks"), api("GET", "/api/emails?flagged=1&limit=50")]);
      office.tasks = t.tasks; tk.flagged = fl.emails;
    } catch (e) { box.replaceChildren(el("div", "empty", e.message)); return; }
  }
  box.textContent = "";
  const list = el("div", "offList"), head = el("div", "offHead");
  const toggle = el("button", "btn small ghost", tk.showDone ? "Скрыть завершённые" : "Показать завершённые");
  toggle.onclick = () => { tk.showDone = !tk.showDone; renderTasks(); };
  head.append(el("h2", "", "Задачи"), toggle);
  const add = el("form", "taskAdd"), subj = el("input"), due = el("input"), go = el("button", "btn small primary", "Добавить");
  subj.type = "text"; subj.placeholder = "Новая задача"; subj.setAttribute("aria-label", "Новая задача");
  due.type = "date"; due.setAttribute("aria-label", "Срок");
  add.onsubmit = async (e) => {
    e.preventDefault();
    if (!subj.value.trim()) return;
    go.disabled = true;
    try { await api("POST", "/api/tasks", { subject: subj.value.trim(), due: due.value || null }); toast("Задача добавлена"); renderTasks(true); }
    catch (err) { alert(err.message); go.disabled = false; }
  };
  add.append(subj, due, go);
  const items = el("div", "offItems");
  const today = isoDate(new Date()), soon = isoDate(addDays(new Date(), 7));
  const groups = [["Просрочено", (t) => !t.done && t.due && t.due < today], ["Сегодня", (t) => !t.done && t.due === today],
    ["На неделе", (t) => !t.done && t.due > today && t.due <= soon], ["Позже", (t) => !t.done && t.due > soon],
    ["Без срока", (t) => !t.done && !t.due], ["Завершённые", (t) => t.done && tk.showDone]];
  const byDue = (a, b) => (a.due || "9") < (b.due || "9") ? -1 : (a.due || "9") > (b.due || "9") ? 1 : a.subject.localeCompare(b.subject);
  groups.forEach(([title, test]) => {
    const rows = office.tasks.filter(test).sort(byDue);
    if (!rows.length) return;
    items.appendChild(el("div", "offGroup", title + " · " + rows.length));
    rows.forEach((t) => items.appendChild(taskRow(t, today)));
  });
  if (tk.flagged.length) {
    items.appendChild(el("div", "offGroup", "Письма с флажком · " + tk.flagged.length));
    tk.flagged.forEach((m) => {
      const r = el("div", "taskRow"), b = el("button", "box"); b.title = "Отметить выполненным"; b.setAttribute("aria-label", "Выполнено");
      b.onclick = async (e) => { e.stopPropagation(); await setFlag(m.id, 1); renderTasks(true); };
      r.append(b, el("span", "nm", m.subject || "(без темы)"), el("span", "due", m.sender_name || m.sender));
      r.onclick = () => { showView("mail"); select(m.id); };
      items.appendChild(r);
    });
  }
  if (!items.childNodes.length) items.appendChild(el("div", "empty", "Задач нет. Добавьте первую или создайте задачу из письма."));
  list.append(head, add, items);
  const detail = el("div", "offDetail"); detail.id = "taskDetail";
  box.append(list, detail);
  if (tk.sel && office.tasks.some((t) => t.id === tk.sel)) showTask(tk.sel); else detail.appendChild(el("div", "empty", "Выберите задачу"));
}
function taskRow(t, today) {
  const r = el("div", "taskRow" + (t.done ? " done" : "") + (tk.sel === t.id ? " on" : "")), b = el("button", "box");
  b.innerHTML = t.done ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="m5 12 5 5 9-10"/></svg>' : "";
  b.title = t.done ? "Вернуть в работу" : "Завершить"; b.setAttribute("aria-label", b.title);
  b.onclick = async (e) => {
    e.stopPropagation();
    try { await api("POST", "/api/tasks", { id: t.id, status: t.done ? "NotStarted" : "Completed" }); renderTasks(true); } catch (err) { alert(err.message); }
  };
  r.append(b);
  if (t.importance === "High") { const i = el("span", "imp", "!"); i.title = "Важная"; r.appendChild(i); }
  r.appendChild(el("span", "nm", t.subject));
  if (t.due) r.appendChild(el("span", "due" + (!t.done && t.due < today ? " late" : ""), new Date(t.due + "T00:00").toLocaleDateString("ru-RU", { day: "numeric", month: "short" })));
  r.onclick = () => { tk.sel = t.id; renderTasks(); };
  return r;
}
async function showTask(id) {
  const d = $("taskDetail"); if (!d) return;
  $("tasksView").classList.add("detail");
  let t;
  try { t = await api("GET", "/api/tasks?id=" + encodeURIComponent(id)); } catch (e) { d.replaceChildren(el("div", "err", e.message)); return; }
  const w = el("div", "wrap2"), f = {};
  const field = (key, label, tag, value) => {
    const l = el("label", "", label), i = el(tag); i.id = "tk_" + key; l.htmlFor = i.id;
    if (value != null) i.value = value; w.append(l, i); f[key] = i; return i;
  };
  field("subject", "Задача", "input", t.subject);
  const g = el("div", "offGrid"), a = el("div"), b = el("div"), c = el("div");
  const mk = (box, key, label, tag) => { const l = el("label", "", label), i = el(tag); i.id = "tk_" + key; l.htmlFor = i.id; box.append(l, i); f[key] = i; return i; };
  mk(a, "due", "Срок", "input").type = "date"; f.due.value = t.due || "";
  const st = mk(b, "status", "Состояние", "select"); Object.entries(TASK_STATUS).forEach(([v, x]) => { const o = el("option", "", x); o.value = v; st.appendChild(o); }); st.value = t.status;
  const im = mk(c, "importance", "Важность", "select"); [["Low", "Низкая"], ["Normal", "Обычная"], ["High", "Высокая"]].forEach(([v, x]) => { const o = el("option", "", x); o.value = v; im.appendChild(o); }); im.value = t.importance;
  g.append(a, b, c); w.appendChild(g);
  field("body", "Заметки", "textarea", t.body || "").rows = 6;
  const err = el("div", "err"), acts = el("div", "actions"), save = el("button", "btn primary", "Сохранить"), del = el("button", "btn danger", "Удалить");
  save.onclick = async () => {
    err.textContent = ""; save.disabled = true;
    try {
      await api("POST", "/api/tasks", { id: t.id, subject: f.subject.value, due: f.due.value || null, status: f.status.value, importance: f.importance.value, body: f.body.value });
      toast("Задача сохранена"); renderTasks(true);
    } catch (e) { err.textContent = e.message; save.disabled = false; }
  };
  del.onclick = async () => {
    if (!confirm("Удалить задачу «" + t.subject + "»?")) return;
    try { await api("POST", "/api/tasks/delete", { id: t.id }); tk.sel = null; toast("Задача удалена"); renderTasks(true); } catch (e) { alert(e.message); }
  };
  acts.append(save, del);
  if (matchMedia("(max-width: 760px)").matches) { const back = el("button", "btn ghost", "← Список"); back.onclick = () => $("tasksView").classList.remove("detail"); acts.prepend(back); }
  w.append(err, acts);
  d.replaceChildren(w);
}

// ---------- настройки: уведомления, подписи, автоответ, правила ----------
function addSettingsCard(key, title, build) {
  const card = el("div", "card"); card.dataset.set = key; card.dataset.title = title; card.hidden = true;
  $("settingsErr").parentElement.appendChild(card);
  const r = el("div", "ct-row"), name = el("div", "name");
  r.dataset.key = key; name.appendChild(el("div", "", title)); r.appendChild(name);
  r.onclick = () => showSetSection(key);
  $("setNav").appendChild(r);
  const show = showSetSection;
  showSetSection = (k) => { show(k); if (k === key) build(card); };
}

addSettingsCard("notify", "Уведомления", (card) => {
  card.textContent = "";
  card.appendChild(el("h3", "", "Уведомления в браузере"));
  const perm = window.Notification ? Notification.permission : "unsupported";
  card.appendChild(el("div", "hint", perm === "granted" ? "Браузер разрешил уведомления." : perm === "denied" ? "Браузер запретил уведомления для GigaMail. Разрешите их в настройках сайта в браузере." : perm === "unsupported" ? "Этот браузер не умеет показывать уведомления." : "Браузер ещё не спрашивал разрешения."));
  if (perm === "default") {
    const b = el("button", "btn primary", "Разрешить уведомления");
    b.onclick = () => Notification.requestPermission().then(() => showSetSection("notify"));
    card.appendChild(b);
  }
  [["mail", "О новых письмах во «Входящих»", "Когда вкладка GigaMail в фоне — уведомление браузера, когда открыта — подсказка внизу экрана."],
    ["meet", "О ближайших встречах", "По напоминанию встречи. Карточка в углу GigaMail показывается всегда."]].forEach(([k, t, h]) => {
    const l = el("label", "check"), i = el("input"); i.type = "checkbox"; i.checked = notifyOn(k);
    i.onchange = () => stored("gm.notify." + k, i.checked ? "1" : "0");
    l.append(i, t); card.append(l, el("div", "hint", h));
  });
  card.appendChild(el("div", "hint", "Уведомления приходят, пока GigaMail открыт хотя бы в одной вкладке. Настройка хранится в этом браузере."));
});

addSettingsCard("signatures", "Подписи", async (card) => {
  card.textContent = "";
  card.appendChild(el("h3", "", "Подписи"));
  card.appendChild(el("div", "hint", "Подпись по умолчанию вставляется в новое письмо или ответ сама. Другую можно выбрать кнопкой подписи в письме."));
  let sigs = [];
  try { sigs = (await api("GET", "/api/signatures")).signatures; } catch (e) { card.appendChild(el("div", "err", e.message)); return; }
  const list = el("div", "sigList"), form = el("div");
  card.append(list, form);
  const edit = (s) => {
    list.querySelectorAll(".sigItem").forEach((x) => x.classList.toggle("on", s && x.dataset.id === String(s.id)));
    s = s || { name: "", body_html: "", for_new: !sigs.length, for_reply: !sigs.length };
    form.textContent = "";
    const nl = el("label", "", "Название"), name = el("input"); name.id = "sig_name"; nl.htmlFor = name.id; name.value = s.name;
    const bl = el("label", "", "Текст подписи"), ed = el("div", "sigEditor"); ed.contentEditable = "true"; ed.id = "sig_body";
    ed.setAttribute("role", "textbox"); ed.setAttribute("aria-multiline", "true"); ed.setAttribute("aria-labelledby", "sig_body_l"); bl.id = "sig_body_l";
    ed.appendChild(safeFragment(s.body_html || ""));
    ed.onpaste = (e) => { e.preventDefault(); document.execCommand("insertText", false, (e.clipboardData || window.clipboardData).getData("text/plain")); };
    ed.onkeydown = (e) => { if ((e.ctrlKey || e.metaKey) && ["b", "i", "u"].includes(e.key.toLowerCase())) { e.preventDefault(); document.execCommand({ b: "bold", i: "italic", u: "underline" }[e.key.toLowerCase()]); } };
    const opt = (label, on) => { const l = el("label", "check"), i = el("input"); i.type = "checkbox"; i.checked = !!on; l.append(i, label); return [l, i]; };
    const [l1, forNew] = opt("По умолчанию в новых письмах", s.for_new), [l2, forReply] = opt("По умолчанию в ответах и пересылке", s.for_reply);
    const err = el("div", "err"), acts = el("div", "actions"), save = el("button", "btn primary", "Сохранить подпись"), del = el("button", "btn danger", "Удалить");
    save.onclick = async () => {
      err.textContent = "";
      try {
        const r = await api("POST", "/api/signatures", { id: s.id, name: name.value, body_html: ed.innerHTML, for_new: forNew.checked, for_reply: forReply.checked });
        sigs = r.signatures; draw(); edit(sigs.find((x) => x.id === r.id)); toast("Подпись сохранена");
      } catch (e) { err.textContent = e.message; }
    };
    del.onclick = async () => {
      if (!confirm("Удалить подпись «" + s.name + "»?")) return;
      sigs = (await api("DELETE", "/api/signatures/" + s.id)).signatures; draw(); edit(null);
    };
    acts.append(save); if (s.id) acts.appendChild(del);
    form.append(nl, name, bl, ed, el("div", "hint", "Жирный, курсив и подчёркнутый — Ctrl+B, Ctrl+I, Ctrl+U."), l1, l2, err, acts);
  };
  const draw = () => {
    list.textContent = "";
    sigs.forEach((s) => {
      const it = el("div", "sigItem"); it.dataset.id = s.id;
      it.append(svgIcon("sig", "ico"), el("span", "nm", s.name));
      if (s.for_new) it.appendChild(el("span", "tag", "новые"));
      if (s.for_reply) it.appendChild(el("span", "tag", "ответы"));
      it.onclick = () => edit(s);
      list.appendChild(it);
    });
    const add = el("button", "btn small", "+ Новая подпись"); add.onclick = () => edit(null);
    list.appendChild(add);
  };
  draw(); edit(sigs[0] || null);
});

addSettingsCard("oof", "Автоответ", async (card) => {
  card.textContent = "";
  card.appendChild(el("h3", "", "Автоответ «нет на месте»"));
  card.appendChild(el("div", "hint", "Настройка хранится в Exchange: автоответ работает, даже когда GigaMail и Outlook закрыты."));
  let o;
  try { o = await api("GET", "/api/oof"); } catch (e) { card.appendChild(el("div", "err", e.message)); return; }
  const f = {};
  const sel = (key, label, opts, value) => {
    const l = el("label", "", label), s = el("select"); s.id = "oof_" + key; l.htmlFor = s.id;
    opts.forEach(([v, t]) => { const x = el("option", "", t); x.value = v; s.appendChild(x); }); s.value = value;
    card.append(l, s); f[key] = s; return s;
  };
  const local = (iso) => (iso ? localInput(new Date(iso)) : "");
  sel("state", "Автоответ", [["Disabled", "Выключен"], ["Enabled", "Включён"], ["Scheduled", "Только в эти даты"]], o.state);
  const when = el("div", "grid"), a = el("div"), b = el("div");
  [[a, "start", "С"], [b, "end", "По"]].forEach(([box, k, t]) => { const l = el("label", "", t), i = el("input"); i.type = "datetime-local"; i.id = "oof_" + k; l.htmlFor = i.id; i.value = local(o[k]); box.append(l, i); f[k] = i; });
  when.append(a, b); card.appendChild(when);
  const ta = (key, label, value) => { const l = el("label", "", label), t = el("textarea"); t.id = "oof_" + key; l.htmlFor = t.id; t.rows = 4; t.value = value || ""; card.append(l, t); f[key] = t; return t; };
  ta("internal_reply", "Ответ коллегам из организации", o.internal_reply);
  sel("external_audience", "Отвечать людям вне организации", [["None", "Нет"], ["Known", "Только моим контактам"], ["All", "Всем"]], o.external_audience);
  ta("external_reply", "Ответ внешним, пусто — тот же текст", o.external_reply);
  const sync = () => { when.hidden = f.state.value !== "Scheduled"; };
  f.state.onchange = sync; sync();
  if (!f.start.value) { const s = new Date(); s.setMinutes(0, 0, 0); f.start.value = localInput(s); f.end.value = localInput(addDays(s, 7)); }
  const err = el("div", "err"), acts = el("div", "actions"), save = el("button", "btn primary", "Сохранить автоответ");
  save.onclick = async () => {
    err.textContent = ""; save.disabled = true;
    try {
      await api("PUT", "/api/oof", { state: f.state.value, external_audience: f.external_audience.value, internal_reply: f.internal_reply.value,
        external_reply: f.external_reply.value, start: f.start.value ? new Date(f.start.value).toISOString() : null, end: f.end.value ? new Date(f.end.value).toISOString() : null });
      toast(f.state.value === "Disabled" ? "Автоответ выключен" : "Автоответ сохранён"); checkOof();
    } catch (e) { err.textContent = e.message; }
    save.disabled = false;
  };
  acts.appendChild(save); card.append(err, acts);
});

addSettingsCard("rules", "Правила", async (card) => {
  card.textContent = "";
  card.appendChild(el("h3", "", "Правила для входящих"));
  card.appendChild(el("div", "hint", "Правила выполняет сам Exchange, когда письмо приходит, — те же, что в Outlook. Порядок — как в Outlook."));
  let rules;
  try { rules = (await api("GET", "/api/rules")).rules; if (!mb.folders.length) await loadFolders(); } catch (e) { card.appendChild(el("div", "err", e.message)); return; }
  const list = el("div", "ruleList"), form = el("div"); card.append(list, form);
  const folderTitle = (id) => (mb.folders.find((x) => x.id === id) || {}).name || "папка";
  const summary = (r) => {
    const c = r.conditions, a = r.actions, w = [], d = [];
    if (c.from.length) w.push("от " + c.from.join(", ")); if (c.to.length) w.push("кому " + c.to.join(", "));
    if (c.subject.length) w.push("в теме «" + c.subject.join("», «") + "»"); if (c.body.length) w.push("в тексте «" + c.body.join("», «") + "»");
    if (c.sent_to_me) w.push("мне лично"); if (c.has_attachments) w.push("с вложениями");
    if (a.move_to) d.push("в «" + folderTitle(a.move_to) + "»"); if (a.mark_read) d.push("прочитано"); if (a.delete) d.push("удалить");
    if (a.forward_to.length) d.push("переслать " + a.forward_to.join(", ")); if (a.stop) d.push("дальше не проверять");
    return (w.join(", ") || "любое письмо") + " → " + (d.join(", ") || "—");
  };
  const reload = async (keep) => { rules = (await api("GET", "/api/rules")).rules; draw(); if (keep !== undefined) edit(rules.find((x) => x.id === keep) || null); };
  const draw = () => {
    list.textContent = "";
    if (!rules.length) list.appendChild(el("div", "hint", "Правил пока нет."));
    rules.forEach((r) => {
      const it = el("div", "ruleItem"), sw = el("label", "check"), on = el("input"), txt = el("div", "nm");
      on.type = "checkbox"; on.checked = r.enabled; on.title = r.enabled ? "Выключить" : "Включить"; on.setAttribute("aria-label", "Правило включено");
      sw.style.margin = "0"; sw.appendChild(on); sw.onclick = (e) => e.stopPropagation();
      on.onchange = async () => { try { await api("POST", "/api/rules/update", { id: r.id, enabled: on.checked }); r.enabled = on.checked; toast(on.checked ? "Правило включено" : "Правило выключено"); } catch (e) { alert(e.message); on.checked = !on.checked; } };
      txt.append(el("div", "", r.name + (r.error ? " · ошибка в Exchange" : "")), el("div", "sub", r.unsupported ? "Настроено в Outlook, меняется там" : summary(r)));
      it.append(sw, txt); it.onclick = () => edit(r);
      list.appendChild(it);
    });
    const add = el("button", "btn small", "+ Новое правило"); add.onclick = () => edit(null); list.appendChild(add);
  };
  const edit = (r) => {
    form.textContent = "";
    list.querySelectorAll(".ruleItem").forEach((x, i) => x.classList.toggle("on", r && rules[i] && rules[i].id === r.id));
    if (r && r.unsupported) {
      form.appendChild(el("div", "hint", "В правиле «" + r.name + "» есть условия или действия, которых нет в GigaMail. Его можно включить, выключить или удалить, а менять — в Outlook."));
      const del = el("button", "btn danger", "Удалить правило");
      del.onclick = async () => { if (confirm("Удалить правило «" + r.name + "»?")) { await api("POST", "/api/rules/delete", { id: r.id }); reload(null); } };
      form.appendChild(del); return;
    }
    r = r || { name: "", enabled: true, conditions: { from: [], to: [], subject: [], body: [], sent_to_me: false, has_attachments: false }, actions: { move_to: "", mark_read: false, delete: false, forward_to: [], stop: true } };
    const f = {};
    const input = (key, label, value, hint) => {
      const l = el("label", "", label), i = el("input"); i.id = "rl_" + key; l.htmlFor = i.id; i.value = value || "";
      if (hint) i.placeholder = hint; form.append(l, i); f[key] = i; return i;
    };
    const check = (key, label, on) => { const l = el("label", "check"), i = el("input"); i.type = "checkbox"; i.checked = !!on; l.append(i, label); form.appendChild(l); f[key] = i; };
    input("name", "Название", r.name);
    form.appendChild(el("h4", "", "Если письмо"));
    input("from", "От (адрес, домен или имя — через запятую)", r.conditions.from.join(", "), "noreply@, @jira.example.ru");
    input("to", "Кому или копия", r.conditions.to.join(", "));
    input("subject", "Тема содержит", r.conditions.subject.join(", "), "[JIRA], релиз");
    input("body", "Текст содержит", r.conditions.body.join(", "));
    check("sent_to_me", "Отправлено мне лично (я в поле «Кому»)", r.conditions.sent_to_me);
    check("has_attachments", "Есть вложения", r.conditions.has_attachments);
    form.appendChild(el("h4", "", "То"));
    const ml = el("label", "", "Переместить в папку"), move = el("select"); move.id = "rl_move"; ml.htmlFor = move.id;
    const none = el("option", "", "— не перемещать —"); none.value = ""; move.appendChild(none);
    folderTree().forEach(([fd, depth]) => { if (fd.kind === "drafts" || fd.kind === "outbox") return; const o = el("option", "", "  ".repeat(depth) + fd.name); o.value = fd.id; move.appendChild(o); });
    move.value = r.actions.move_to || ""; form.append(ml, move); f.move = move;
    check("mark_read", "Отметить как прочитанное", r.actions.mark_read);
    check("delete", "Удалить (в «Удалённые»)", r.actions.delete);
    input("forward_to", "Переслать на адреса", r.actions.forward_to.join(", "));
    check("stop", "Не применять остальные правила", r.actions.stop);
    const en = el("label", "check"), enabled = el("input"); enabled.type = "checkbox"; enabled.checked = r.enabled; en.append(enabled, "Правило включено"); form.appendChild(en);
    const err = el("div", "err"), acts = el("div", "actions"), save = el("button", "btn primary", r.id ? "Сохранить правило" : "Создать правило"), del = el("button", "btn danger", "Удалить");
    const split = (i) => i.value.split(/[,;\n]+/).map((x) => x.trim()).filter(Boolean);
    save.onclick = async () => {
      err.textContent = "";
      const body = { id: r.id, name: f.name.value, enabled: enabled.checked,
        conditions: { from: split(f.from), to: split(f.to), subject: split(f.subject), body: split(f.body), sent_to_me: f.sent_to_me.checked, has_attachments: f.has_attachments.checked },
        actions: { move_to: f.move.value || null, mark_read: f.mark_read.checked, delete: f.delete.checked, forward_to: split(f.forward_to), stop: f.stop.checked } };
      if (body.actions.forward_to.length && !confirm("Письма по этому правилу будут пересылаться на " + body.actions.forward_to.join(", ") + ". Сохранить?")) return;
      save.disabled = true;
      try {
        const res = r.id ? await api("POST", "/api/rules/update", body) : await api("POST", "/api/rules", body);
        toast("Правило сохранено"); await reload(r.id || res.id);
      } catch (e) { err.textContent = e.message; save.disabled = false; }
    };
    del.onclick = async () => { if (!confirm("Удалить правило «" + r.name + "»?")) return; try { await api("POST", "/api/rules/delete", { id: r.id }); toast("Правило удалено"); reload(null); } catch (e) { alert(e.message); } };
    acts.appendChild(save); if (r.id) acts.appendChild(del);
    form.append(err, acts);
  };
  draw(); edit(null);
});

// ---------- цвета встреч ----------
addSettingsCard("colors", "Цвета встреч", async (card) => {
  card.textContent = "";
  card.appendChild(el("h3", "", "Цвета встреч в календаре"));
  card.appendChild(el("div", "hint", "Цвет выбирается так: сначала по категории, которую получило письмо-приглашение (её ставит модель или вы вручную), потом по категории Outlook с тем же названием, что у правила, потом по словам в теме, месте или у организатора. Остальные встречи — обычного зелёного цвета."));
  let rules = [], categories = [];
  try {
    const [r, c] = await Promise.all([api("GET", "/api/calendar/colors"), api("GET", "/api/categories")]);
    rules = r.rules; categories = c.categories;
  } catch (e) { card.appendChild(el("div", "err", e.message)); return; }
  const list = el("div", "clrList"), err = el("div", "err"), acts = el("div", "actions");
  const add = el("button", "btn", "Добавить цвет"), save = el("button", "btn primary", "Сохранить");
  const PALETTE = ["#2563eb", "#7c3aed", "#dc2626", "#ea580c", "#ca8a04", "#0d9488", "#db2777", "#475569"];
  const row = (r, i) => {
    const d = el("div", "clrRow"), c = el("input"), n = el("input"), w = el("input"), x = el("button", "iconBtn"), k = el("select");
    [["", "Любая категория"], ...categories.map((y) => [y.name, y.title])].forEach(([v, t]) => { const o = el("option", "", t); o.value = v; k.appendChild(o); });
    if (r.category && !categories.some((y) => y.name === r.category)) { const o = el("option", "", r.category + " (удалена)"); o.value = r.category; k.appendChild(o); }
    k.value = r.category || ""; k.setAttribute("aria-label", "Категория письма для правила " + (i + 1));
    c.type = "color"; c.value = r.color; c.title = "Цвет"; c.setAttribute("aria-label", "Цвет правила " + (i + 1));
    n.value = r.name; n.placeholder = "Название или категория Outlook"; n.setAttribute("aria-label", "Название правила " + (i + 1));
    w.value = (r.words || []).join(", "); w.placeholder = "Слова через запятую"; w.setAttribute("aria-label", "Слова правила " + (i + 1));
    x.innerHTML = ICON.close; x.title = "Убрать"; x.onclick = () => { collect(); rules.splice(i, 1); draw(); };
    d.append(c, n, k, w, x);
    return d;
  };
  const collect = () => {
    rules = [...list.querySelectorAll(".clrRow")].map((d) => {
      const [c, n, w] = d.querySelectorAll("input");
      return { color: c.value, name: n.value.trim(), category: d.querySelector("select").value, words: w.value.split(",").map((s) => s.trim()).filter(Boolean) };
    });
  };
  const draw = () => { list.textContent = ""; rules.forEach((r, i) => list.appendChild(row(r, i))); };
  add.onclick = () => { collect(); rules.push({ name: "", color: PALETTE[rules.length % PALETTE.length], words: [] }); draw(); list.lastChild.querySelectorAll("input")[1].focus(); };
  save.onclick = async () => {
    collect(); err.textContent = "";
    try {
      rules = (await api("PUT", "/api/calendar/colors", { rules })).rules;
      if (typeof cal !== "undefined") { cal.colors = rules; renderLegend(); if (!$("calView").hidden) renderCal(); }
      draw(); toast("Цвета сохранены");
    } catch (e) { err.textContent = e.message; }
  };
  acts.append(add, save);
  card.append(list, el("div", "hint", "Например: категория «Авто» — синий; «ПСИ» (категория ПСИ или слово ПСИ) — фиолетовый; слово «отдел» — красный. Если подходят несколько правил, сработает верхнее."), err, acts);
  draw();
});
(function () {
  const st = document.createElement("style");
  st.textContent = ".clrList{display:grid;gap:8px;margin:10px 0}.clrRow{display:grid;grid-template-columns:44px minmax(110px,1fr) minmax(150px,1.2fr) minmax(120px,1.4fr) auto;gap:8px;align-items:center}"
    + ".clrRow input[type=color]{width:44px;height:34px;padding:2px;border-radius:8px}@media (max-width:640px){.clrRow{grid-template-columns:44px 1fr auto}.clrRow select,.clrRow input:nth-child(4){grid-column:1/-1}}";
  document.head.appendChild(st);
  const b = $("calColorsBtn");
  if (b) b.onclick = () => openSettings().then(() => showSetSection("colors")).catch((e) => alert(e.message));
})();
