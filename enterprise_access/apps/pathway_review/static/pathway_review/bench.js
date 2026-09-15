/*
 * Pathway review bench.
 *
 * The server decides which pathway comes next and who may see it; this file renders one
 * item at a time and posts judgements back. It deliberately never learns another
 * reviewer's verdict -- the leaderboard returns families, never ratings.
 */
(function () {
  "use strict";

  var LEVELS = ["Introductory", "Intermediate", "Advanced"];
  var REASONS = [
    ["wrong_job", "Not this job"],
    ["wrong_level", "Wrong difficulty"],
    ["too_generic", "Too generic"],
    ["bad_order", "Wrong order"],
    ["thin_catalog", "Catalog has nothing better"]
  ];
  var POSITIVE = { good: 1, skip: 1 };

  var stage = document.getElementById("stage");
  var csrf = (document.querySelector("[name=csrfmiddlewaretoken]") || {}).value || "";
  var mode = "ladders";
  var current = null, draft = null, startedAt = 0;
  var progress = { reviewed: 0, goal: 20, remaining: 0, goal_set: true };
  var board = { rows: [], you: null };
  var openDesc = {}, openPanels = {}, openRater = null, careerDesc = {};
  var ready = false, askingGoal = false;

  /* ---------- plumbing ---------- */
  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (txt != null) { n.textContent = txt; }
    return n;
  }
  function api(path, options) {
    var opts = options || {};
    opts.headers = Object.assign(
      { "Content-Type": "application/json", "X-CSRFToken": csrf },
      opts.headers || {}
    );
    opts.credentials = "same-origin";
    return fetch("/pathway-review/api/" + path, opts).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) { throw Object.assign(new Error("request failed"), { body: body }); }
        return body;
      });
    });
  }
  function toast(msg, ms) {
    var t = el("div", "toast", msg);
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, ms || 1800);
  }

  /* ---------- celebration ---------- */
  function celebrate(msg) {
    toast(msg, 3400);
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) { return; }
    var cv = document.createElement("canvas");
    cv.className = "confetti";
    document.body.appendChild(cv);
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    var W = window.innerWidth, H = window.innerHeight;
    cv.width = W * dpr; cv.height = H * dpr;
    var ctx = cv.getContext("2d");
    if (!ctx) { cv.remove(); return; }
    ctx.scale(dpr, dpr);
    var colors = ["#bc4a2e", "#3c4e55", "#1668a6", "#1d7048", "#8a6206", "#93a7ad"];
    var parts = [];
    for (var i = 0; i < 90; i++) {
      parts.push({
        x: W / 2 + (Math.random() - 0.5) * 260, y: H * 0.3 + (Math.random() - 0.5) * 70,
        vx: (Math.random() - 0.5) * 7, vy: Math.random() * -9 - 3,
        w: 5 + Math.random() * 6, h: 3 + Math.random() * 5,
        rot: Math.random() * Math.PI, vr: (Math.random() - 0.5) * 0.3,
        c: colors[i % colors.length]
      });
    }
    var last = performance.now(), started = last;
    (function frame(now) {
      var dt = Math.min(34, now - last); last = now;
      ctx.clearRect(0, 0, W, H);
      var alive = false;
      for (var j = 0; j < parts.length; j++) {
        var p = parts[j];
        p.vy += 0.03 * dt; p.x += p.vx * dt / 16; p.y += p.vy * dt / 16; p.rot += p.vr;
        if (p.y < H + 50) { alive = true; }
        ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot);
        ctx.fillStyle = p.c; ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); ctx.restore();
      }
      if (alive && now - started < 6000) { requestAnimationFrame(frame); } else { cv.remove(); }
    })(last);
  }

  /* ---------- hover descriptions ---------- */
  var hc = null;
  function hoverInit() {
    hc = el("div"); hc.id = "hovercard"; document.body.appendChild(hc);
    function show(t) {
      var name = t.getAttribute("data-desc-name") || t.textContent;
      var d = careerDesc[name];
      if (!d) { return; }
      hc.innerHTML = "";
      hc.appendChild(el("b", "", name));
      hc.appendChild(el("i", "", d));
      hc.style.display = "block";
      hc.style.left = "0px"; hc.style.top = "0px";
      var r = t.getBoundingClientRect(), w = hc.offsetWidth, h = hc.offsetHeight;
      var left = r.left, top = r.bottom + 8;
      if (left + w > window.innerWidth - 12) { left = window.innerWidth - 12 - w; }
      if (left < 12) { left = 12; }
      if (top + h > window.innerHeight - 12) { top = r.top - 8 - h; }
      hc.style.left = left + "px"; hc.style.top = Math.max(12, top) + "px";
    }
    function hide() { if (hc) { hc.style.display = "none"; } }
    document.addEventListener("mouseover", function (e) {
      var t = e.target.closest && e.target.closest("[data-desc]"); if (t) { show(t); }
    });
    document.addEventListener("mouseout", function (e) {
      if (e.target.closest && e.target.closest("[data-desc]")) { hide(); }
    });
    document.addEventListener("focusin", function (e) {
      var t = e.target.closest && e.target.closest("[data-desc]"); if (t) { show(t); }
    });
    document.addEventListener("focusout", hide);
    window.addEventListener("scroll", hide, true);
  }
  function describable(node, name) {
    node.setAttribute("data-desc", "");
    node.setAttribute("data-desc-name", name);
    node.tabIndex = 0;
    return node;
  }

  /* ---------- course descriptions ---------- */
  function descBlock(text, limit, expandable, key) {
    if (!text) { return null; }
    var wrap = el("span", "cdesc");
    var isOpen = !!openDesc[key];
    var needsMore = text.length > limit;
    var shown = (isOpen || !needsMore)
      ? text
      : text.slice(0, limit).replace(/\s+\S*$/, "") + "…";
    wrap.appendChild(el("span", "cdesc-t", shown));
    if (needsMore && expandable) {
      var b = el("button", "morebtn", isOpen ? "Show less" : "Show more");
      b.type = "button";
      b.addEventListener("click", function (e) {
        e.preventDefault(); e.stopPropagation();
        openDesc[key] = !openDesc[key];
        render();
      });
      wrap.appendChild(b);
    }
    return wrap;
  }
  function rememberOpen(d, name) {
    d.open = !!openPanels[name];
    d.addEventListener("toggle", function () { openPanels[name] = d.open; });
    return d;
  }

  /* ---------- disclosures ---------- */
  function careersDetails(list) {
    if (!list || !list.length) { return null; }
    var d = rememberOpen(el("details", "disc"), "careers");
    d.appendChild(el("summary", "", "All " + list.length + " career"
      + (list.length === 1 ? "" : "s") + " this pathway is built for"));
    var body = el("div", "disc-body");
    var g = el("div", "cgrid");
    list.forEach(function (c) { g.appendChild(describable(el("span", "", c.name), c.name)); });
    body.appendChild(g);
    body.appendChild(el("p", "hint", "Hover or tab to a title to read what that career is."));
    d.appendChild(body);
    return d;
  }
  function altDetails(item) {
    var total = 0;
    LEVELS.forEach(function (lv) { total += ((item.alt && item.alt[lv]) || []).length; });
    if (!total) { return null; }
    var d = rememberOpen(el("details", "disc"), "alts");
    d.appendChild(el("summary", "", "See the other " + total + " course"
      + (total === 1 ? "" : "s") + " the search found"));
    var body = el("div", "disc-body");
    LEVELS.forEach(function (lv) {
      var arr = (item.alt && item.alt[lv]) || [];
      if (!arr.length) { return; }
      var g = el("div", "altgrp");
      var h = el("h5", "", lv + " · " + arr.length);
      h.style.color = lv === "Introductory" ? "var(--r1)"
        : lv === "Intermediate" ? "var(--r2)" : "var(--r3)";
      g.appendChild(h);
      var list = el("div", "altlist");
      arr.forEach(function (c) {
        var row = el("div");
        var head = el("div", "altrow-h");
        var t = el("span");
        var a = el("a", "", c.title);
        a.href = c.url; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.style.color = "inherit";
        t.appendChild(a);
        head.appendChild(t);
        head.appendChild(el("span", "am", c.provider));
        row.appendChild(head);
        var rd = descBlock(c.desc, 500, true, "alt:" + c.key);
        if (rd) { row.appendChild(rd); }
        list.appendChild(row);
      });
      g.appendChild(list);
      body.appendChild(g);
    });
    body.appendChild(el("p", "hint",
      "These were found for this career but not placed in the ladder. Optional — "
      + "open it if it helps you judge."));
    d.appendChild(body);
    return d;
  }

  /* ---------- the ladder ---------- */
  function renderLadder(item) {
    var grid = el("div", "grid");
    var card = el("div", "card");

    var head = el("div", "card-head");
    head.appendChild(el("h2", "", item.pathway));
    var hm = el("div", "head-meta");
    hm.appendChild(el("span", "", item.careers
      + (item.careers === 1 ? " career" : " careers") + " in this family"));
    hm.appendChild(el("span", "chip", item.mix));
    hm.appendChild(el("span", "chip", item.id));
    head.appendChild(hm);
    if (item.family_description) {
      head.appendChild(el("p", "fdesc", item.family_description));
    }
    card.appendChild(head);
    var cd = careersDetails(item.careers_list);
    if (cd) { card.appendChild(cd); }

    var lad = el("div", "ladder");
    item.courses.forEach(function (c) {
      var row = el("div", "rung lv-" + c.level);
      row.id = "rung-" + c.step;
      var mk = el("div", "mark");
      mk.appendChild(el("span", "dot", String(c.step)));
      row.appendChild(mk);

      var body = el("div");
      var t = el("div", "title");
      var a = el("a", "", c.title);
      a.href = c.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.style.color = "inherit";
      t.appendChild(a);
      body.appendChild(t);
      var sub = el("div", "sub");
      sub.appendChild(el("span", "lvtag", c.level));
      sub.appendChild(el("span", "", c.provider));
      sub.appendChild(el("span", "mono", c.key));
      body.appendChild(sub);
      var cdb = descBlock(c.desc, 500, true, "course:" + c.key);
      if (cdb) { body.appendChild(cdb); }
      row.appendChild(body);

      var kd = el("div", "kd");
      ["keep", "drop"].forEach(function (v) {
        var b = el("button", "", v === "keep" ? "Keep" : "Drop");
        b.dataset.v = v;
        b.setAttribute("aria-pressed", String((draft.drops[c.step] ? "drop" : "keep") === v));
        b.addEventListener("click", function () { setDrop(c, v === "drop"); });
        kd.appendChild(b);
      });
      row.appendChild(kd);

      if (draft.drops[c.step]) { row.appendChild(swapPicker(item, c)); }
      lad.appendChild(row);
    });
    card.appendChild(lad);
    var ad = altDetails(item);
    if (ad) { card.appendChild(ad); }
    card.appendChild(verdictBar(item));
    grid.appendChild(card);
    grid.appendChild(ladderSide(item));
    return grid;
  }

  function setDrop(c, isDrop) {
    if (isDrop) {
      draft.drops[c.step] = true;
    } else {
      delete draft.drops[c.step];
      delete draft.swaps[c.step];
    }
    render();
    var r = document.getElementById("rung-" + c.step);
    if (r && isDrop) { r.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
  }

  function swapPicker(item, c) {
    var box = el("div", "swap");
    var alts = (item.alt && item.alt[c.level]) || [];
    box.appendChild(el("h4", "",
      "What should have been in this " + c.level.toLowerCase() + " slot?"));
    if (!alts.length) {
      box.appendChild(el("p", "empty-rung", "The search found no other "
        + c.level.toLowerCase() + " course for this career. There was nothing else to pick."));
    }
    var list = el("div", "opts");
    alts.forEach(function (alt) {
      var b = el("button", "opt");
      b.type = "button";
      b.setAttribute("aria-pressed", String(draft.swaps[c.step] === alt.key));
      b.appendChild(el("span", "radio"));
      var d = el("span");
      d.appendChild(el("span", "t", alt.title));
      d.appendChild(el("span", "m", alt.provider + " · " + alt.key));
      var sd = descBlock(alt.desc, 500, false, "pick:" + alt.key);
      if (sd) { d.appendChild(sd); }
      b.appendChild(d);
      b.addEventListener("click", function () {
        draft.swaps[c.step] = draft.swaps[c.step] === alt.key ? undefined : alt.key;
        render();
      });
      list.appendChild(b);
    });
    var none = el("button", "opt none");
    none.type = "button";
    none.setAttribute("aria-pressed", String(draft.swaps[c.step] === "__none__"));
    none.appendChild(el("span", "radio"));
    var nd = el("span");
    nd.appendChild(el("span", "t", "Nothing here would work"));
    nd.appendChild(el("span", "m", "The catalog is missing content for this rung"));
    none.appendChild(nd);
    none.addEventListener("click", function () {
      draft.swaps[c.step] = draft.swaps[c.step] === "__none__" ? undefined : "__none__";
      render();
    });
    list.appendChild(none);
    box.appendChild(list);
    return box;
  }

  function ladderSide(item) {
    var side = el("div", "side");
    var p1 = el("div", "panel");
    p1.appendChild(el("h3", "", "Careers in this family"));
    var tt = el("div", "titles");
    var list = item.careers_list || [];
    list.slice(0, 10).forEach(function (c) {
      tt.appendChild(describable(el("span", "", c.name), c.name));
    });
    p1.appendChild(tt);
    p1.appendChild(el("p", "hint", list.length > 10
      ? "and " + (list.length - 10) + " more, listed above the ladder."
      : "Hover a title to read what that career is."));
    side.appendChild(p1);

    var p2 = el("div", "panel");
    p2.appendChild(el("h3", "", "Courses the search found"));
    var supply = item.supply || {};
    var max = Math.max(1, supply.Introductory || 0, supply.Intermediate || 0, supply.Advanced || 0);
    LEVELS.forEach(function (lv) {
      var n = supply[lv] || 0;
      var r = el("div", "supply-row");
      r.appendChild(el("span", "", lv));
      var bar = el("span", "sbar");
      var fill = el("i");
      fill.style.width = (100 * n / max) + "%";
      fill.style.background = lv === "Introductory" ? "var(--r1)"
        : lv === "Intermediate" ? "var(--r2)" : "var(--r3)";
      bar.appendChild(fill);
      r.appendChild(bar);
      r.appendChild(el("span", "mono", String(n)));
      p2.appendChild(r);
    });
    p2.appendChild(el("p", "hint",
      "Out of a maximum of 12 per level. A short bar means the model had little to choose from."));
    side.appendChild(p2);

    var p3 = el("div", "panel");
    p3.appendChild(el("h3", "", "Shortcuts"));
    [["1 – 5", "drop / undrop that course"], ["G", "good"], ["N", "needs work"],
      ["B", "bad"], ["↵", "submit"], ["S", "skip this one"]].forEach(function (k) {
      var r = el("div", "kv");
      var a = el("span", "mono", k[0]);
      var b = el("span", "v", k[1]);
      b.style.fontFamily = "var(--sans)";
      b.style.color = "var(--ink-soft)";
      r.appendChild(a); r.appendChild(b);
      p3.appendChild(r);
    });
    side.appendChild(p3);
    return side;
  }

  /* ---------- verdict ---------- */
  function needsNote(v) { return !!v && !POSITIVE[v]; }
  function noteMissing() {
    return needsNote(draft && draft.verdict) && !((draft.notes || "").trim());
  }

  function verdictBar(item) {
    var v = el("div", "verdict");
    var row = el("div", "vrow");
    row.appendChild(el("span", "vq", "Overall, is this a good pathway?"));
    [["good", "Good"], ["needs_work", "Needs work"], ["bad", "Bad"]].forEach(function (spec) {
      var b = el("button", "vbtn", spec[1]);
      b.dataset.v = spec[0];
      b.setAttribute("aria-pressed", String(draft.verdict === spec[0]));
      b.appendChild(el("span", "kbd", spec[1][0].toUpperCase()));
      b.addEventListener("click", function () {
        draft.verdict = spec[0];
        render();
        if (needsNote(spec[0])) {
          var n = document.getElementById("notes-" + item.id);
          if (n) { n.focus(); }
        }
      });
      row.appendChild(b);
    });
    v.appendChild(row);

    if (needsNote(draft.verdict)) {
      var rs = el("div", "reasons");
      REASONS.forEach(function (r) {
        var b = el("button", "rtag", r[1]);
        b.setAttribute("aria-pressed", String(!!draft.reasons[r[0]]));
        b.addEventListener("click", function () {
          if (draft.reasons[r[0]]) { delete draft.reasons[r[0]]; } else { draft.reasons[r[0]] = true; }
          render();
        });
        rs.appendChild(b);
      });
      v.appendChild(rs);
    }

    var ta = el("textarea", "notes");
    ta.id = "notes-" + item.id;
    ta.placeholder = needsNote(draft.verdict)
      ? "What was not good about this and/or how would you fix?"
      : "Anything worth saying in words (optional)";
    ta.value = draft.notes;
    ta.addEventListener("input", function () { draft.notes = ta.value; sync(); });
    v.appendChild(ta);

    var act = el("div", "actions");
    var sub = el("button", "btn", "Submit and next");
    sub.addEventListener("click", function () { submit(); });
    act.appendChild(sub);
    var sk = el("button", "btn btn-quiet", "Skip — can't judge this");
    sk.addEventListener("click", function () { draft.verdict = "skip"; submit(); });
    act.appendChild(sk);
    var st = el("span", "hint");
    act.appendChild(st);
    function sync() {
      sub.disabled = !draft.verdict || noteMissing();
      ta.classList.toggle("required", noteMissing());
      st.textContent = !draft.verdict ? "Pick a verdict above to continue."
        : noteMissing() ? "Say what was wrong, or how you would fix it, to continue."
        : "Saving as you go.";
    }
    sync();
    v.appendChild(act);
    return v;
  }

  function submit() {
    if (!current || !draft.verdict || noteMissing()) { return; }
    var drops = Object.keys(draft.drops).map(Number).sort(function (a, b) { return a - b; });
    var swaps = {};
    drops.forEach(function (s) { swaps[String(s)] = draft.swaps[s] || ""; });
    var payload = {
      item: current.id,
      verdict: draft.verdict,
      drops: drops,
      swaps: swaps,
      reasons: Object.keys(draft.reasons),
      notes: draft.notes || "",
      seconds: Math.round((Date.now() - startedAt) / 1000)
    };
    current = null;
    stage.innerHTML = "";
    stage.appendChild(el("div", "loading", "Saving…"));
    api("vote/", { method: "POST", body: JSON.stringify(payload) }).then(function (data) {
      var before = progress.reviewed;
      progress = Object.assign(progress, data.progress);
      if (progress.reviewed > before) {
        if (progress.reviewed === progress.goal) {
          celebrate("Goal reached — " + progress.reviewed + " reviews. Thank you.");
        } else if (progress.reviewed % 5 === 0) {
          celebrate(progress.reviewed + " reviews done. Keep going.");
        }
      }
      loadNext();
    }).catch(function (err) {
      toast((err.body && err.body.error) || "Could not save that rating.", 3200);
      loadNext();
    });
  }

  /* ---------- leaderboard ---------- */
  function renderLeaderboard() {
    var card = el("div", "card");
    var head = el("div", "card-head");
    head.appendChild(el("h2", "", "Who has reviewed the most"));
    var total = board.rows.reduce(function (n, r) { return n + r.total; }, 0);
    var hm = el("div", "head-meta");
    hm.appendChild(el("span", "", total + (total === 1 ? " rating" : " ratings") + " from "
      + board.rows.length + (board.rows.length === 1 ? " reviewer" : " reviewers")));
    head.appendChild(hm);
    card.appendChild(head);

    if (!board.rows.length) {
      var e = el("div", "disc-body");
      e.appendChild(el("p", "hint", "No ratings yet. Yours would be the first."));
      card.appendChild(e);
      return card;
    }

    var body = el("div", "disc-body");
    var tbl = el("table", "lb");
    var thead = el("thead"), htr = el("tr");
    [["", ""], ["Reviewer", ""], ["Total", "num"], ["Toward goal", "goalcell"]]
      .forEach(function (h) { htr.appendChild(el("th", h[1], h[0])); });
    thead.appendChild(htr); tbl.appendChild(thead);
    var tb = el("tbody");
    board.rows.forEach(function (r, i) {
      var tr = el("tr", r.user_id === board.you ? "me" : "");
      tr.appendChild(el("td", "rank", String(i + 1)));
      var nd = el("td", "nm");
      nd.appendChild(document.createTextNode(r.name));
      if (r.user_id === board.you) { nd.appendChild(el("span", "you", "you")); }
      tr.appendChild(nd);
      tr.appendChild(el("td", "num", String(r.total)));
      var gcell = el("td", "goalcell");
      gcell.appendChild(el("span", "mono", r.total + " / " + r.goal));
      var gb = el("span", "gbar"), gi = el("i");
      gi.style.width = Math.min(100, 100 * r.total / (r.goal || 1)) + "%";
      gb.appendChild(gi); gcell.appendChild(gb);
      tr.appendChild(gcell);
      tr.addEventListener("click", function () {
        openRater = openRater === r.user_id ? null : r.user_id;
        render();
      });
      tb.appendChild(tr);
      if (openRater === r.user_id) {
        var dtr = el("tr", "lb-detail"), dtd = el("td");
        dtd.colSpan = 4;
        dtd.appendChild(el("h4", "", r.user_id === board.you
          ? "What you have reviewed" : "What " + r.name + " has reviewed"));
        var grid = el("div", "cgrid");
        r.families.forEach(function (f) { grid.appendChild(el("span", "", f)); });
        dtd.appendChild(grid);
        dtd.appendChild(el("p", "hint",
          "Families only. Verdicts stay hidden so that everyone judges independently."));
        dtr.appendChild(dtd);
        tb.appendChild(dtr);
      }
    });
    tbl.appendChild(tb);
    body.appendChild(tbl);
    card.appendChild(body);
    card.appendChild(goalEditor("Your review goal"));
    return card;
  }

  function goalEditor(label) {
    var wrap = el("div", "disc-body");
    wrap.style.borderTop = "1px solid var(--line-soft)";
    var lab = el("label", "goal-wrap");
    lab.appendChild(document.createTextNode(label));
    var input = el("input");
    input.type = "number"; input.min = "1"; input.max = "2000"; input.id = "goal-edit";
    input.value = String(progress.goal || 20);
    input.addEventListener("change", function () {
      var goal = Math.max(1, parseInt(input.value, 10) || 20);
      api("goal/", { method: "POST", body: JSON.stringify({ goal: goal }) })
        .then(function (data) {
          progress.goal = data.goal;
          progress.goal_set = true;
          paintMeter();
          render();
        }).catch(function () { toast("Could not save that goal."); });
    });
    lab.appendChild(input);
    wrap.appendChild(lab);
    return wrap;
  }

  /* ---------- first run: set a goal ---------- */
  function renderGoalGate() {
    var g = el("div", "gate");
    g.appendChild(el("h1", "", "Judge the pathway, not the paperwork"));
    var p = el("p", "lede");
    p.textContent = "Each screen shows one career and the five courses the model picked for it. "
      + "You are judging one thing: would these five courses actually prepare someone for that job?";
    g.appendChild(p);

    var ul = el("ul", "rules");
    [["do", "Keep or drop each course. If you drop one, pick what should have been there "
      + "instead — from the courses the search actually found for that rung."],
    ["do", "If nothing in a rung would do, say so. That means the catalog is missing content, "
      + "which is a different problem from the model choosing badly."],
    ["skip", "Don’t check the rules a script already checks: five courses, valid keys, no "
      + "repeats, at most two per provider, English, in the pinned catalog."],
    ["skip", "Don’t worry about being the only reviewer. Every pathway is rated by more "
      + "than one person, and disagreements get a third look."]
    ].forEach(function (r) {
      var li = el("li");
      li.appendChild(el("span", "tick " + r[0], r[0] === "do" ? "✓" : "—"));
      li.appendChild(el("span", "", r[1]));
      ul.appendChild(li);
    });
    g.appendChild(ul);

    var f = el("div", "field");
    var lab = el("label", "goal-wrap");
    lab.appendChild(document.createTextNode("I'll review"));
    var input = el("input");
    input.type = "number"; input.min = "1"; input.max = "2000"; input.id = "rater-goal";
    input.value = String(progress.goal || 20);
    lab.appendChild(input);
    var go = el("button", "btn", "Start reviewing");
    go.addEventListener("click", function () {
      var goal = Math.max(1, parseInt(input.value, 10) || 20);
      go.disabled = true;
      api("goal/", { method: "POST", body: JSON.stringify({ goal: goal }) })
        .then(function (data) {
          progress.goal = data.goal;
          progress.goal_set = true;
          askingGoal = false;
          paintMeter();
          render();
        }).catch(function () {
          go.disabled = false;
          toast("Could not save that goal.");
        });
    });
    f.appendChild(lab); f.appendChild(go);
    g.appendChild(f);
    g.appendChild(el("p", "hint",
      "Your goal is just for you — change it any time from the leaderboard."));
    return g;
  }

  /* ---------- chrome ---------- */
  function paintMeter() {
    var goal = progress.goal || 20;
    document.getElementById("meter-txt").textContent = progress.reviewed + " / " + goal;
    document.getElementById("meter-bar").style.width =
      Math.min(100, goal ? 100 * progress.reviewed / goal : 0) + "%";
    document.getElementById("meter-label").textContent =
      progress.reviewed >= goal ? "goal reached" : "reviewed";
  }

  function render() {
    paintMeter();
    stage.innerHTML = "";
    if (mode === "leaderboard") { stage.appendChild(renderLeaderboard()); return; }
    if (askingGoal) { stage.appendChild(renderGoalGate()); return; }
    if (!ready) {
      stage.appendChild(el("div", "loading", "Loading the queue…"));
      return;
    }
    if (!current) {
      var d = el("div", "done");
      d.appendChild(el("h2", "", "That is the whole queue."));
      d.appendChild(el("p", "lede", "You rated " + progress.reviewed
        + " pathways. Nothing is left that you have not already seen."));
      var b = el("button", "btn", "See the leaderboard");
      b.addEventListener("click", function () { switchMode("leaderboard"); });
      d.appendChild(b);
      stage.appendChild(d);
      return;
    }
    stage.appendChild(renderLadder(current));
  }

  function switchMode(m) {
    mode = m;
    document.querySelectorAll(".mode").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.mode === m));
    });
    if (m === "leaderboard") {
      api("leaderboard/").then(function (data) {
        board = data;
        render();
      }).catch(function () { toast("Could not load the leaderboard."); });
    }
    render();
  }
  document.getElementById("modes").addEventListener("click", function (e) {
    var b = e.target.closest(".mode");
    if (b) { switchMode(b.dataset.mode); }
  });

  /* ---------- keyboard ---------- */
  document.addEventListener("keydown", function (e) {
    if (!current || mode !== "ladders") { return; }
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") { return; }
    var k = e.key.toLowerCase();
    if (k >= "1" && k <= "5") {
      var step = Number(k);
      var c = current.courses.filter(function (x) { return x.step === step; })[0];
      if (c) { setDrop(c, !draft.drops[step]); e.preventDefault(); }
      return;
    }
    var map = { g: "good", n: "needs_work", b: "bad" };
    if (map[k]) {
      draft.verdict = map[k];
      render();
      if (needsNote(map[k])) {
        var nt = document.getElementById("notes-" + current.id);
        if (nt) { nt.focus(); }
      }
      e.preventDefault();
      return;
    }
    if (k === "s") { draft.verdict = "skip"; submit(); e.preventDefault(); return; }
    if (e.key === "Enter" && draft.verdict) { submit(); e.preventDefault(); }
  });

  /* ---------- boot ---------- */
  function loadNext() {
    return api("next/").then(function (data) {
      progress = Object.assign(progress, data.progress);
      current = data.item;
      careerDesc = {};
      if (current) {
        (current.careers_list || []).forEach(function (c) { careerDesc[c.name] = c.desc; });
        draft = { drops: {}, swaps: {}, verdict: null, reasons: {}, notes: "" };
        openDesc = {}; openPanels = {};
        startedAt = Date.now();
      }
      ready = true;
      if (!progress.goal_set) { askingGoal = true; }
      render();
    }).catch(function () {
      ready = true;
      stage.innerHTML = "";
      stage.appendChild(el("div", "banner", "Could not reach the review service. Reload to try again."));
    });
  }

  hoverInit();
  render();
  loadNext();
})();
