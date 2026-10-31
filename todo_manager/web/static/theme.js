// Applied before the page is drawn, so a chosen theme does not flash.
try {
  const theme = localStorage.getItem("tm-theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch (e) { /* storage unavailable: follow the system */ }
