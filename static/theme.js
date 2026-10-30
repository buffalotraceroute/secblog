(function () {
  const key = "secblog-theme";
  const root = document.documentElement;
  const saved = localStorage.getItem(key);
  if (saved) root.setAttribute("data-theme", saved);
  else if (window.matchMedia("(prefers-color-scheme: light)").matches) root.setAttribute("data-theme", "light");
  else root.setAttribute("data-theme", "dark");
  document.getElementById("theme-toggle")?.addEventListener("click", () => {
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem(key, next);
  });
})();
