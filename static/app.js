/* secblog UI: theme toggle, filter drawer, sidebar navigation, page filter. */
(function () {
  "use strict";
  var root = document.documentElement;
  var KEY = "secblog-theme";

  // Theme toggle (default follows prefers-color-scheme; an explicit choice is remembered)
  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var cur = root.getAttribute("data-theme") ||
        (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      var next = cur === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(KEY, next); } catch (e) {}
    });
  }

  // Mobile filter drawer
  var sidebar = document.getElementById("sidebar");
  var scrim = document.getElementById("scrim");
  var openBtn = document.getElementById("filters-open");
  function setDrawer(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", open);
    if (scrim) scrim.classList.toggle("open", open);
    document.body.classList.toggle("drawer-open", open);
    if (openBtn) openBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { var f = sidebar.querySelector("input,select,a"); if (f) f.focus({ preventScroll: true }); }
    else if (openBtn && sidebar.contains(document.activeElement)) openBtn.focus();
  }
  if (openBtn) openBtn.addEventListener("click", function () { setDrawer(true); });
  document.querySelectorAll("[data-close-drawer]").forEach(function (el) {
    el.addEventListener("click", function () { setDrawer(false); });
  });
  if (scrim) scrim.addEventListener("click", function () { setDrawer(false); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && sidebar && sidebar.classList.contains("open")) setDrawer(false);
  });

  // <select data-nav> jumps to the chosen URL
  document.querySelectorAll("select[data-nav]").forEach(function (sel) {
    sel.addEventListener("change", function () { if (sel.value) location.href = sel.value; });
  });

  // Source picker is filled lazily from blogs.json (keeps every page small)
  var blogSel = document.getElementById("sb-blog");
  if (blogSel && blogSel.dataset.src) {
    var loaded = false;
    var load = function () {
      if (loaded) return; loaded = true;
      fetch(blogSel.dataset.src).then(function (r) { return r.json(); }).then(function (rows) {
        var cur = blogSel.dataset.current || "";
        var frag = document.createDocumentFragment();
        rows.forEach(function (b) {
          if (b.id === cur) return;
          var o = document.createElement("option");
          o.value = blogSel.dataset.base + "/blog/" + b.id + "/";
          o.textContent = b.name;
          frag.appendChild(o);
        });
        blogSel.appendChild(frag);
      }).catch(function () { loaded = false; });
    };
    ["focus", "pointerdown", "touchstart", "keydown"].forEach(function (ev) { blogSel.addEventListener(ev, load, { once: false, passive: true }); });
    if ("requestIdleCallback" in window) requestIdleCallback(load); else setTimeout(load, 1500);
  }

  // "Filter this page" — quick client-side filter over the cards on the current page
  var pf = document.getElementById("page-filter");
  var list = pf && document.getElementById(pf.dataset.target || "post-list");
  if (pf && list) {
    var cards = Array.prototype.slice.call(list.querySelectorAll(pf.dataset.items || ".card"));
    var none = document.getElementById("no-results");
    var count = document.getElementById("result-count");
    var baseText = count ? count.textContent : "";
    var run = function () {
      var q = pf.value.trim().toLowerCase();
      var shown = 0;
      cards.forEach(function (c) {
        var ok = !q || c.textContent.toLowerCase().indexOf(q) !== -1;
        c.hidden = !ok; if (ok) shown++;
      });
      if (none) none.hidden = shown !== 0;
      if (count) count.textContent = q ? shown + " of " + cards.length + " match" : baseText;
    };
    pf.addEventListener("input", run);
    if (pf.value) run();
  }
})();
